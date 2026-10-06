#!/usr/bin/env python3
"""Increment 3 verification: chat vs agent session mode.

Start the gateway with a live seat (e.g. `ccuc`) on localhost:8080, then:
    python scripts/test_mode.py [key]

Checks:
  chat (default)  -> session.ready.mode == "chat", zero tool.use, no files created,
                     the BFS answer comes back inline as code in assistant text.
  agent (opt-in)  -> session.ready.mode == "agent", a tool fires, greeting.txt written.
  resume          -> a resumed chat session still reports mode == "chat".
"""
import asyncio
import json
import os
import sys

import websockets

URL = "ws://localhost:8080/v1/agent"
KEY = sys.argv[1] if len(sys.argv) > 1 else "devkey"
WORK_ROOT = os.path.join(".ccuc", "sessions")


def work_files(sid):
    """Non-hidden files created in the session work dir (skills live under .claude/)."""
    wd = os.path.join(WORK_ROOT, sid or "", "work")
    found = []
    for root, dirs, files in os.walk(wd):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if not f.startswith("."):
                found.append(os.path.relpath(os.path.join(root, f), wd))
    return found


async def drive(open_frame, text, *, resume_sid=None):
    """Open (or resume) a session and optionally run one turn. Returns collected state."""
    state = {"sid": resume_sid, "ready": None, "texts": [], "delta": [], "tools": []}
    async with websockets.connect(URL, max_size=None, ping_interval=None) as ws:
        ready = asyncio.Event()
        done = asyncio.Event()

        async def reader():
            async for raw in ws:
                fr = json.loads(raw)
                t = fr.get("type")
                if t == "session.ready":
                    state["sid"] = fr.get("session")
                    state["ready"] = fr
                    ready.set()
                elif t == "assistant.text":
                    state["texts"].append(fr.get("text", ""))
                elif t == "assistant.delta":
                    state["delta"].append(fr.get("text", ""))
                elif t == "tool.use":
                    state["tools"].append(fr.get("name"))
                elif t == "turn.complete":
                    done.set()
                elif t == "error" and fr.get("code") != "skill_pack_warning":
                    print("  [error]", fr.get("code"), fr.get("message"))
                    ready.set()
                    done.set()

        rt = asyncio.create_task(reader())
        await ws.send(json.dumps({"v": 1, "type": "auth", "token": KEY}))
        await ws.send(json.dumps(open_frame))
        await asyncio.wait_for(ready.wait(), 60)
        if text and state["sid"]:
            await ws.send(json.dumps({"v": 1, "type": "user.message", "req_id": "m",
                                      "session_id": state["sid"], "text": text}))
            await asyncio.wait_for(done.wait(), 300)
        rt.cancel()
    return state


async def main():
    results = {}

    # 1) chat (default: no mode field) — pure completion, code inline
    s1 = await drive(
        {"v": 1, "type": "session.open", "req_id": "o", "model": "sonnet"},
        "Give me C++ code for a breadth-first search (BFS) on an adjacency list. Reply with the code only.",
    )
    ready1 = s1["ready"] or {}
    text1 = "\n".join(s1["texts"]) or "".join(s1["delta"])
    files1 = work_files(s1["sid"])
    has_code = ("#include" in text1) or ("```" in text1) or ("queue" in text1.lower())
    print(f"[chat] sid={s1['sid']} mode={ready1.get('mode')} tools_reported={len(ready1.get('tools', []))} "
          f"tool_uses={s1['tools']} files={files1}")
    print(f"[chat] answer head: {text1[:160]!r}")
    results["chat: session.ready.mode == chat"] = ready1.get("mode") == "chat"
    results["chat: zero tool.use"] = len(s1["tools"]) == 0
    results["chat: no files created in work dir"] = len(files1) == 0
    results["chat: answered with code inline"] = has_code

    # 2) agent (opt-in) — full-agentic, writes a file via the greet skill
    s2 = await drive(
        {"v": 1, "type": "session.open", "req_id": "o", "model": "sonnet", "mode": "agent", "skills": ["pgs-skills"]},
        "greet me",
    )
    ready2 = s2["ready"] or {}
    files2 = work_files(s2["sid"])
    print(f"[agent] sid={s2['sid']} mode={ready2.get('mode')} tool_uses={s2['tools']} files={files2}")
    results["agent: session.ready.mode == agent"] = ready2.get("mode") == "agent"
    results["agent: used a tool"] = len(s2["tools"]) > 0
    results["agent: created greeting.txt"] = any(f.endswith("greeting.txt") for f in files2)

    # 3) resume preserves the mode
    s3 = await drive({"v": 1, "type": "session.open", "req_id": "o", "model": "sonnet"}, None)
    sid3 = s3["sid"]
    await asyncio.sleep(2)  # let the server suspend/detach the session on disconnect
    s3r = await drive({"v": 1, "type": "session.resume", "req_id": "o", "session_id": sid3}, None, resume_sid=sid3)
    ready3 = s3r["ready"] or {}
    print(f"[resume] sid={sid3} mode={ready3.get('mode')}")
    results["resume: mode preserved (chat)"] = ready3.get("mode") == "chat"

    print("\n=== INCREMENT 3 (mode) RESULTS ===")
    ok = True
    for k, v in results.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
        ok = ok and v
    print("MODE:", "ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
