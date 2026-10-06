#!/usr/bin/env python3
"""M2 resume check: fact set on conn1 survives a disconnect + reconnect+resume."""
import asyncio
import json
import sys

import websockets

URL = "ws://localhost:8080/v1/agent"
KEY = sys.argv[1] if len(sys.argv) > 1 else "devkey"


async def drive(ws, state):
    async for raw in ws:
        f = json.loads(raw)
        t = f.get("type")
        if t == "assistant.delta":
            state["buf"] += f.get("text", "")
        elif t == "assistant.text":
            state["buf"] += f.get("text", "")
        elif t == "session.ready":
            state["session"] = f.get("session")
            state["ready"].set()
        elif t == "turn.complete":
            state["turn_done"].set()
        elif t == "error":
            print("  [error]", f.get("code"), f.get("message"))
            state["ready"].set()
            state["turn_done"].set()


async def turn(ws, state, sid, text):
    state["turn_done"].clear()
    state["buf"] = ""
    await ws.send(json.dumps({"v": 1, "type": "user.message", "req_id": "m", "session_id": sid, "text": text}))
    await asyncio.wait_for(state["turn_done"].wait(), timeout=600)
    return state["buf"]


async def main():
    # ---- connection 1: set a fact, then drop the socket without closing the session ----
    state = {"session": None, "ready": asyncio.Event(), "turn_done": asyncio.Event(), "buf": ""}
    async with websockets.connect(URL, max_size=None, ping_interval=None) as ws:
        t = asyncio.create_task(drive(ws, state))
        await ws.send(json.dumps({"v": 1, "type": "auth", "token": KEY}))
        await ws.send(json.dumps({"v": 1, "type": "session.open", "req_id": "o1", "model": "sonnet"}))
        await asyncio.wait_for(state["ready"].wait(), timeout=120)
        sid = state["session"]
        print("opened session:", sid)
        r1 = await turn(ws, state, sid, "Remember this secret codeword: BANANA-77. Just acknowledge in a few words.")
        print("turn1:", r1.strip()[:120])
        t.cancel()
    print("dropped connection 1 (no session.close)")

    await asyncio.sleep(2)  # let the server suspend the session

    # ---- connection 2: resume by id, ask for the fact ----
    state2 = {"session": None, "ready": asyncio.Event(), "turn_done": asyncio.Event(), "buf": ""}
    async with websockets.connect(URL, max_size=None, ping_interval=None) as ws:
        t = asyncio.create_task(drive(ws, state2))
        await ws.send(json.dumps({"v": 1, "type": "auth", "token": KEY}))
        await ws.send(json.dumps({"v": 1, "type": "session.resume", "req_id": "r1", "session_id": sid}))
        await asyncio.wait_for(state2["ready"].wait(), timeout=120)
        print("resumed session:", state2["session"])
        r2 = await turn(ws, state2, sid, "What exact codeword did I ask you to remember? Reply with only the codeword.")
        print("turn2:", r2.strip()[:200])
        await ws.send(json.dumps({"v": 1, "type": "session.close", "session_id": sid}))
        await asyncio.sleep(0.2)
        t.cancel()

    ok = "BANANA-77" in r2 or "BANANA" in r2.upper()
    print("\nRESUME TEST:", "PASS — context retained across reconnect" if ok else f"FAIL — got: {r2!r}")
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
