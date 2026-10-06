#!/usr/bin/env python3
"""Increment 2 verification: skill packs, global MCP, default pack, hard-kill."""
import asyncio
import json
import os
import re
import subprocess
import sys

import websockets

URL = "ws://localhost:8080/v1/agent"
KEY = sys.argv[1] if len(sys.argv) > 1 else "devkey"


class Conn:
    def __init__(self, ws):
        self.ws = ws
        self.session = None
        self.cwd = None
        self.skills = []
        self.text = ""
        self.tools = []
        self.results = []
        self.ready = asyncio.Event()
        self.turn_done = asyncio.Event()

    async def reader(self):
        async for raw in self.ws:
            f = json.loads(raw)
            t = f.get("type")
            if t in ("assistant.delta", "assistant.text"):
                self.text += f.get("text", "")
            elif t == "tool.use":
                self.tools.append(f.get("name"))
            elif t == "tool.result":
                self.results.append(str(f.get("content", "")))
            elif t == "session.ready":
                self.session, self.cwd, self.skills = f.get("session"), f.get("cwd"), f.get("skills", [])
                self.ready.set()
            elif t == "turn.complete":
                self.turn_done.set()
            elif t == "error":
                print("  [error]", f.get("code"), f.get("message"))
                self.ready.set()
                self.turn_done.set()

    async def open(self, skills=None):
        await self.ws.send(json.dumps({"v": 1, "type": "auth", "token": KEY}))
        m = {"v": 1, "type": "session.open", "req_id": "o", "model": "sonnet"}
        if skills is not None:
            m["skills"] = skills
        await self.ws.send(json.dumps(m))
        await asyncio.wait_for(self.ready.wait(), 120)

    async def turn(self, text):
        self.turn_done.clear()
        self.text = ""
        self.tools = []
        self.results = []
        await self.ws.send(json.dumps({"v": 1, "type": "user.message", "req_id": "m",
                                       "session_id": self.session, "text": text}))
        await asyncio.wait_for(self.turn_done.wait(), 300)

    async def close(self):
        await self.ws.send(json.dumps({"v": 1, "type": "session.close", "session_id": self.session}))
        await asyncio.sleep(0.4)


async def main():
    ok = True

    # ---- Phase 1: skills + global MCP + hard-kill ----
    async with websockets.connect(URL, max_size=None, ping_interval=None) as ws:
        c = Conn(ws)
        rt = asyncio.create_task(c.reader())
        await c.open(skills=["pgs-skills"])
        print("ready.skills =", c.skills)
        assert "pgs-skills__greet" in c.skills, "greet skill not composed"
        assert os.path.isfile(os.path.join(c.cwd, ".claude", "skills", "pgs-skills__greet", "SKILL.md")), "SKILL.md missing in cwd"
        print("PASS  skills composed into session cwd")

        await c.turn("Please greet me — say hello.")
        gp = os.path.join(c.cwd, "greeting.txt")
        if os.path.isfile(gp) and "hello from pgs-skills" in open(gp).read():
            print("PASS  greet skill executed ->", open(gp).read().strip())
        else:
            print("FAIL  greet skill did not produce greeting.txt")
            ok = False

        await c.turn("Use the ccuc-util echo tool with text HELLO_MCP, then tell me exactly what it returned.")
        used_echo = any("echo" in (n or "") for n in c.tools)
        got = any("HELLO_MCP" in r for r in c.results) or "HELLO_MCP" in c.text
        print(f"{'PASS' if used_echo and got else 'FAIL'}  global MCP echo (tools={c.tools})")
        ok = ok and used_echo and got

        sid = c.session
        pre = subprocess.run(["pgrep", "-f", sid], capture_output=True, text=True).stdout.split()
        print(f"claude procs for session before close: {len(pre)}")
        await c.turn("Run exactly this bash command and report the number it prints: sleep 300 & echo CHILD=$!")
        m = re.search(r"CHILD=(\d+)", " ".join(c.results) + " " + c.text)
        child = int(m.group(1)) if m else None
        print("child pid =", child)
        await c.close()
        rt.cancel()
        await asyncio.sleep(1.5)
        post = subprocess.run(["pgrep", "-f", sid], capture_output=True, text=True).stdout.split()
        if not post:
            print("PASS  hard-kill: claude terminal process gone after close")
        else:
            print("FAIL  hard-kill: claude process still running:", post)
            ok = False
        if child:
            if subprocess.run(["ps", "-p", str(child)], capture_output=True).returncode == 0:
                print(f"WARN  backgrounded child {child} survived (fully-detached; needs container isolation)")
                subprocess.run(["kill", str(child)], capture_output=True)
            else:
                print("PASS  hard-kill: backgrounded child reaped too")

    # ---- Phase 2: default pack when none named ----
    async with websockets.connect(URL, max_size=None, ping_interval=None) as ws:
        c = Conn(ws)
        rt = asyncio.create_task(c.reader())
        await c.open(skills=None)
        print("default ready.skills =", c.skills)
        if any(s.startswith("default__") for s in c.skills):
            print("PASS  default pack applied when none named")
        else:
            print("FAIL  default pack not applied")
            ok = False
        await c.close()
        rt.cancel()

    print("\nINCREMENT TEST:", "ALL PASS" if ok else "SOME CHECKS FAILED")
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
