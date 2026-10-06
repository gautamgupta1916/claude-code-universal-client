#!/usr/bin/env python3
"""M2 interrupt check: a long turn is cancelled fast and the process survives."""
import asyncio
import json
import sys
import time

import websockets

URL = "ws://localhost:8080/v1/agent"
KEY = sys.argv[1] if len(sys.argv) > 1 else "devkey"


async def main():
    st = {"session": None, "ready": asyncio.Event(), "turn_done": asyncio.Event(),
          "deltas": 0, "buf": "", "interrupt_at": None, "complete_at": None}

    async with websockets.connect(URL, max_size=None, ping_interval=None) as ws:
        async def reader():
            async for raw in ws:
                f = json.loads(raw)
                t = f.get("type")
                if t == "assistant.delta":
                    st["deltas"] += 1
                    st["buf"] += f.get("text", "")
                elif t == "session.ready":
                    st["session"] = f.get("session")
                    st["ready"].set()
                elif t == "turn.complete":
                    st["complete_at"] = time.monotonic()
                    st["turn_done"].set()
                elif t == "error":
                    print("  [error]", f.get("code"), f.get("message"))
                    st["ready"].set(); st["turn_done"].set()
        rt = asyncio.create_task(reader())

        await ws.send(json.dumps({"v": 1, "type": "auth", "token": KEY}))
        await ws.send(json.dumps({"v": 1, "type": "session.open", "req_id": "o1", "model": "sonnet"}))
        await asyncio.wait_for(st["ready"].wait(), timeout=120)
        sid = st["session"]
        print("opened:", sid)

        # long turn
        st["turn_done"].clear(); st["deltas"] = 0; st["buf"] = ""
        await ws.send(json.dumps({"v": 1, "type": "user.message", "req_id": "m1", "session_id": sid,
                                  "text": "Write a detailed 600-word essay about the history of computing. Be verbose."}))
        # wait for streaming to start
        for _ in range(200):
            if st["deltas"] > 5:
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(1.0)
        st["interrupt_at"] = time.monotonic()
        print(f"interrupting after {st['deltas']} deltas...")
        await ws.send(json.dumps({"v": 1, "type": "interrupt", "session_id": sid}))
        await asyncio.wait_for(st["turn_done"].wait(), timeout=30)
        dt = st["complete_at"] - st["interrupt_at"]
        print(f"turn.complete arrived {dt:.2f}s after interrupt (essay was ~{len(st['buf'])} chars)")

        # process must still be alive for a new turn
        st["turn_done"].clear(); st["buf"] = ""
        await ws.send(json.dumps({"v": 1, "type": "user.message", "req_id": "m2", "session_id": sid,
                                  "text": "Reply with exactly this token and nothing else: STILL_ALIVE"}))
        await asyncio.wait_for(st["turn_done"].wait(), timeout=120)
        print("post-interrupt reply:", st["buf"].strip()[:80])

        await ws.send(json.dumps({"v": 1, "type": "session.close", "session_id": sid}))
        await asyncio.sleep(0.2)
        rt.cancel()

    fast = dt < 8.0
    alive = "STILL_ALIVE" in st["buf"]
    print("\nINTERRUPT TEST:", "PASS" if (fast and alive) else "FAIL",
          f"(fast_cancel={fast}, process_alive={alive})")
    return 0 if (fast and alive) else 1


sys.exit(asyncio.run(main()))
