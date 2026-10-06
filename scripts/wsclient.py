#!/usr/bin/env python3
"""Reference / test WebSocket client for CCUC.

Usage:
  # one-shot: run N turns then exit (great for smoke tests)
  python scripts/wsclient.py --key devkey \
      --message "Create hello.txt containing 'hi' then cat it" \
      --message "What did the file say?"

  # interactive: type turns on stdin, /quit to exit
  python scripts/wsclient.py --key devkey --interactive
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import websockets


async def run(url: str, key: str, messages: list[str], cwd, model, interactive: bool, skills=None, mode=None) -> int:
    state = {"session": None, "ready": asyncio.Event(), "turn_done": asyncio.Event(), "rc": 0}

    async with websockets.connect(url, max_size=None, ping_interval=None) as ws:

        async def reader():
            async for raw in ws:
                f = json.loads(raw)
                t = f.get("type")
                if t == "assistant.delta":
                    state["saw_delta"] = True
                    sys.stdout.write(f.get("text", ""))
                    sys.stdout.flush()
                elif t == "assistant.text":
                    if not state.get("saw_delta"):  # deltas already showed it live
                        print(f.get("text", ""))
                elif t == "assistant.thinking":
                    pass
                elif t == "tool.use":
                    print(f"\n  ⚙ {f.get('name')}({json.dumps(f.get('input', {}))[:160]})")
                elif t == "tool.result":
                    tag = "ERR " if f.get("is_error") else ""
                    print(f"  ↳ {tag}{str(f.get('content'))[:160]}")
                elif t == "session.ready":
                    state["session"] = f.get("session")
                    print(f"[ready session={f.get('session')} seat={f.get('seat')} "
                          f"mode={f.get('mode')} cwd={f.get('cwd')} tools={len(f.get('tools', []))} "
                          f"skills={f.get('skills', [])}]")
                    state["ready"].set()
                elif t == "turn.complete":
                    print(f"\n[turn done: stop={f.get('stop_reason')} cost=${f.get('cost_usd')} "
                          f"dur={f.get('duration_ms')}ms turns={f.get('num_turns')}]")
                    state["turn_done"].set()
                elif t == "error":
                    print(f"\n[ERROR {f.get('code')}: {f.get('message')}]")
                    state["rc"] = 1
                    state["ready"].set()
                    state["turn_done"].set()
                elif t == "ready":
                    print("[connected]")
                elif t == "session.closed":
                    print("[session closed]")
                else:
                    print(f"\n[{t}] {json.dumps(f)[:200]}")

        rtask = asyncio.create_task(reader())

        # first-frame auth (avoids websockets header-API version differences)
        await ws.send(json.dumps({"v": 1, "type": "auth", "token": key}))
        open_msg = {"v": 1, "type": "session.open", "req_id": "o1", "cwd": cwd, "model": model}
        if skills:
            open_msg["skills"] = skills
        if mode:
            open_msg["mode"] = mode
        await ws.send(json.dumps(open_msg))
        await asyncio.wait_for(state["ready"].wait(), timeout=120)
        sid = state["session"]
        if sid is None:
            rtask.cancel()
            return state["rc"] or 1

        async def one_turn(text: str):
            state["turn_done"].clear()
            state["saw_delta"] = False
            print(f"\n>>> {text}")
            await ws.send(json.dumps({"v": 1, "type": "user.message", "req_id": "m", "session_id": sid, "text": text}))
            await asyncio.wait_for(state["turn_done"].wait(), timeout=600)

        for m in messages:
            await one_turn(m)

        if interactive:
            loop = asyncio.get_event_loop()
            print("\n(interactive — type a message, /quit to exit)")
            while True:
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                if line in ("/quit", "/exit"):
                    break
                await one_turn(line)

        await ws.send(json.dumps({"v": 1, "type": "session.close", "session_id": sid}))
        await asyncio.sleep(0.2)
        rtask.cancel()
    return state["rc"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://localhost:8080/v1/agent")
    ap.add_argument("--key", default="devkey")
    ap.add_argument("--message", action="append", default=[], help="a turn to send (repeatable)")
    ap.add_argument("--cwd", default=None, help="working subdir under the key's root")
    ap.add_argument("--model", default=None)
    ap.add_argument("--skills", action="append", default=None, help="skill pack to load (repeatable, agent mode)")
    ap.add_argument("--mode", default=None, choices=["chat", "agent"], help="session mode: chat (no tools, inline answers) | agent (full-agentic)")
    ap.add_argument("--interactive", action="store_true")
    args = ap.parse_args()
    return asyncio.run(run(args.url, args.key, args.message, args.cwd, args.model, args.interactive, args.skills, args.mode))


if __name__ == "__main__":
    raise SystemExit(main())
