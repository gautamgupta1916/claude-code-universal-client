#!/usr/bin/env python3
"""M4 verification: audit log, /stats, /metrics, stale-session reaping.
Server should run with a short stale TTL + sweep interval."""
import asyncio
import json
import sys
import urllib.request

import websockets

URL = "ws://localhost:8080/v1/agent"
KEY = sys.argv[1] if len(sys.argv) > 1 else "devkey"


def get_json(path):
    with urllib.request.urlopen("http://localhost:8080" + path) as r:
        return json.load(r)


def get_text(path):
    with urllib.request.urlopen("http://localhost:8080" + path) as r:
        return r.read().decode()


async def session_with_tool():
    """Open a pgs-skills session, run a tool-using turn, close. Returns session id."""
    async with websockets.connect(URL, max_size=None, ping_interval=None) as ws:
        sid = None
        done = asyncio.Event()
        ready = asyncio.Event()

        async def r():
            nonlocal sid
            async for raw in ws:
                f = json.loads(raw)
                if f.get("type") == "session.ready":
                    sid = f.get("session")
                    ready.set()
                elif f.get("type") == "turn.complete":
                    done.set()
                elif f.get("type") == "error" and f.get("code") != "skill_pack_warning":
                    ready.set()
                    done.set()
        t = asyncio.create_task(r())
        await ws.send(json.dumps({"v": 1, "type": "auth", "token": KEY}))
        await ws.send(json.dumps({"v": 1, "type": "session.open", "req_id": "o", "model": "sonnet", "skills": []}))
        await asyncio.wait_for(ready.wait(), 60)
        await ws.send(json.dumps({"v": 1, "type": "user.message", "req_id": "m", "session_id": sid,
                                  "text": "Create a file called m4test.txt containing the word ok, then run: cat m4test.txt"}))
        await asyncio.wait_for(done.wait(), 300)
        await ws.send(json.dumps({"v": 1, "type": "session.close", "session_id": sid}))
        await asyncio.sleep(0.3)
        t.cancel()
        return sid


async def open_and_drop():
    """Open a session then drop the socket (server suspends it). Returns session id."""
    async with websockets.connect(URL, max_size=None, ping_interval=None) as ws:
        sid = None
        ready = asyncio.Event()

        async def r():
            nonlocal sid
            async for raw in ws:
                f = json.loads(raw)
                if f.get("type") == "session.ready":
                    sid = f.get("session")
                    ready.set()
        t = asyncio.create_task(r())
        await ws.send(json.dumps({"v": 1, "type": "auth", "token": KEY}))
        await ws.send(json.dumps({"v": 1, "type": "session.open", "req_id": "o", "model": "sonnet"}))
        await asyncio.wait_for(ready.wait(), 60)
        t.cancel()
        return sid


async def main():
    results = {}

    # --- audit + stats + metrics ---
    sid = await session_with_tool()
    stats = get_json("/stats")
    ec = stats["usage"]["event_counts"]
    print("[stats.usage]", json.dumps(stats["usage"]))
    results["audit records tool_use + turn_complete"] = ec.get("tool_use", 0) > 0 and ec.get("turn_complete", 0) > 0 and ec.get("session_open", 0) > 0
    results["stats reports cost"] = stats["usage"]["total_cost_usd"] > 0

    audit = get_json(f"/audit?session={sid}")
    tools = [a["tool_name"] for a in audit if a["event_type"] == "tool_use"]
    print("[audit tools for session]", tools)
    results["audit tail scoped to session"] = len(audit) > 0 and len(tools) > 0

    metrics = get_text("/metrics")
    print("[metrics sample]", " | ".join(l for l in metrics.splitlines() if l and not l.startswith("#"))[:160])
    results["metrics exposes prometheus"] = "ccuc_sessions{status=" in metrics and "ccuc_cost_usd_total" in metrics

    # --- stale reaping ---
    sid2 = await open_and_drop()
    st = None
    reaped = False
    for _ in range(15):
        await asyncio.sleep(1)
        st = [s["status"] for s in get_json("/sessions") if s["session_id"] == sid2]
        if st == ["failed"]:
            reaped = True
            break
    print("[stale] suspended session status ->", st)
    results["stale SUSPENDED session reaped"] = reaped

    print("\n=== M4 RESULTS ===")
    ok = True
    for k, v in results.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
        ok = ok and v
    print("M4:", "ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
