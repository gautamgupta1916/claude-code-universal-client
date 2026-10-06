#!/usr/bin/env python3
"""Docker plumbing test — auth-independent.

Verifies the WS<->stdin/stdout bridge and session management inside the
container: session.open spawns a process and streams back; /sessions tracks it;
a turn round-trips through stdin->claude->stdout (content will be the unauth
"Not logged in" result, which is fine); close removes it; and resume across a
new connection re-spawns the session.
"""
import asyncio
import json
import sys
import urllib.request

import websockets

URL = "ws://localhost:8080/v1/agent"
KEY = sys.argv[1] if len(sys.argv) > 1 else "devkey"


def http_sessions():
    with urllib.request.urlopen("http://localhost:8080/sessions") as r:
        return json.load(r)


async def reader(ws, st):
    async for raw in ws:
        f = json.loads(raw)
        t = f.get("type")
        if t == "session.ready":
            st["session"] = f.get("session")
            st["ready"].set()
        elif t in ("assistant.delta", "assistant.text"):
            st["text"] += f.get("text", "")
        elif t == "turn.complete":
            st["turn_complete"] = True
            st["turn_done"].set()
        elif t == "error":
            print("  [error]", f.get("code"), f.get("message"))
            st["ready"].set()
            st["turn_done"].set()


def new_state():
    return {"session": None, "ready": asyncio.Event(), "turn_done": asyncio.Event(),
            "turn_complete": False, "text": ""}


async def main():
    # ---- phase 1: open, /sessions, turn round-trip, close ----
    st = new_state()
    async with websockets.connect(URL, max_size=None, ping_interval=None) as ws:
        rt = asyncio.create_task(reader(ws, st))
        await ws.send(json.dumps({"v": 1, "type": "auth", "token": KEY}))
        await ws.send(json.dumps({"v": 1, "type": "session.open", "req_id": "o1", "model": "sonnet"}))
        await asyncio.wait_for(st["ready"].wait(), timeout=60)
        sid = st["session"]
        assert sid, "no session.ready"
        print("PASS open -> session.ready:", sid)

        sess = http_sessions()
        assert any(s["session_id"] == sid for s in sess), "session missing from /sessions"
        print(f"PASS /sessions tracks it ({len(sess)} session, status={[s['status'] for s in sess]})")

        await ws.send(json.dumps({"v": 1, "type": "user.message", "req_id": "m1", "session_id": sid, "text": "hi"}))
        await asyncio.wait_for(st["turn_done"].wait(), timeout=60)
        assert st["turn_complete"], "no turn.complete — WS<->stdin/stdout bridge failed"
        print("PASS turn round-trip via stdin/stdout; agent replied:", st["text"].strip()[:60] or "(none)")

        await ws.send(json.dumps({"v": 1, "type": "session.close", "session_id": sid}))
        await asyncio.sleep(0.5)
        rt.cancel()
    assert not any(s["session_id"] == sid for s in http_sessions()), "session not removed after close"
    print("PASS close -> removed from /sessions")

    # ---- phase 2: resume across a new connection ----
    st = new_state()
    async with websockets.connect(URL, max_size=None, ping_interval=None) as ws:
        rt = asyncio.create_task(reader(ws, st))
        await ws.send(json.dumps({"v": 1, "type": "auth", "token": KEY}))
        await ws.send(json.dumps({"v": 1, "type": "session.open", "req_id": "o2", "model": "sonnet"}))
        await asyncio.wait_for(st["ready"].wait(), timeout=60)
        rsid = st["session"]
        rt.cancel()
    print("dropped connection holding", rsid)
    await asyncio.sleep(2)  # let the server suspend it

    st = new_state()
    async with websockets.connect(URL, max_size=None, ping_interval=None) as ws:
        rt = asyncio.create_task(reader(ws, st))
        await ws.send(json.dumps({"v": 1, "type": "auth", "token": KEY}))
        await ws.send(json.dumps({"v": 1, "type": "session.resume", "req_id": "r1", "session_id": rsid}))
        await asyncio.wait_for(st["ready"].wait(), timeout=60)
        assert st["session"] == rsid, "resume did not re-attach same session"
        print("PASS resume -> session.ready (re-spawned across new connection):", rsid)
        await ws.send(json.dumps({"v": 1, "type": "session.close", "session_id": rsid}))
        await asyncio.sleep(0.3)
        rt.cancel()

    print("\nDOCKER PLUMBING TEST: ALL PASS")
    return 0


sys.exit(asyncio.run(main()))
