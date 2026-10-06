#!/usr/bin/env python3
"""Open a session (pgs-skills), hold it long enough for the CLI `init` (MCP load)
to log, then session.close. Prints ONLY the session id on stdout (for the caller
to pgrep). Auth-independent."""
import asyncio
import json
import sys

import websockets

URL = "ws://localhost:8080/v1/agent"
KEY = sys.argv[1] if len(sys.argv) > 1 else "devkey"


async def main():
    async with websockets.connect(URL, max_size=None, ping_interval=None) as ws:
        info = {}
        ready = asyncio.Event()

        async def r():
            async for raw in ws:
                f = json.loads(raw)
                if f.get("type") == "session.ready":
                    info.update(f)
                    ready.set()

        t = asyncio.create_task(r())
        await ws.send(json.dumps({"v": 1, "type": "auth", "token": KEY}))
        await ws.send(json.dumps({"v": 1, "type": "session.open", "req_id": "o",
                                  "model": "sonnet", "skills": ["pgs-skills"]}))
        await asyncio.wait_for(ready.wait(), 60)
        print(info["session"], flush=True)   # only line on stdout
        # a turn triggers `system/init` (emitted on first stdin message); unauth is fine
        await ws.send(json.dumps({"v": 1, "type": "user.message", "req_id": "m",
                                  "session_id": info["session"], "text": "hi"}))
        await asyncio.sleep(6)
        await ws.send(json.dumps({"v": 1, "type": "session.close", "session_id": info["session"]}))
        await asyncio.sleep(0.5)
        t.cancel()


asyncio.run(main())
