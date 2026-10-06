#!/usr/bin/env python3
"""Docker (auth-independent) check: skill packs bundled + composed, default pack.
(Skill composition + session.ready.skills don't need Claude auth.)"""
import asyncio
import json
import sys

import websockets

URL = "ws://localhost:8080/v1/agent"
KEY = sys.argv[1] if len(sys.argv) > 1 else "devkey"


async def open_and_report(skills):
    async with websockets.connect(URL, max_size=None, ping_interval=None) as ws:
        ready = asyncio.Event()
        info = {}

        async def r():
            async for raw in ws:
                f = json.loads(raw)
                if f.get("type") == "session.ready":
                    info.update(f)
                    ready.set()
                elif f.get("type") == "error":
                    print("  err", f.get("code"), f.get("message"))
                    ready.set()

        t = asyncio.create_task(r())
        await ws.send(json.dumps({"v": 1, "type": "auth", "token": KEY}))
        m = {"v": 1, "type": "session.open", "req_id": "o", "model": "sonnet"}
        if skills is not None:
            m["skills"] = skills
        await ws.send(json.dumps(m))
        await asyncio.wait_for(ready.wait(), 60)
        if info.get("session"):
            await ws.send(json.dumps({"v": 1, "type": "session.close", "session_id": info["session"]}))
            await asyncio.sleep(0.3)
        t.cancel()
        return info.get("skills", [])


async def main():
    s1 = await open_and_report(["pgs-skills"])
    print("skills[pgs-skills] ->", s1)
    assert "pgs-skills__greet" in s1, "pgs-skills not composed in container"
    s2 = await open_and_report(None)
    print("skills[default]   ->", s2)
    assert any(x.startswith("default__") for x in s2), "default pack not applied in container"
    print("PASS  docker: skill packs bundled + composed + default pack")


asyncio.run(main())
