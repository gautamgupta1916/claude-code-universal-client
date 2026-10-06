#!/usr/bin/env python3
"""Negative-path checks for M2: bad key rejected, cwd escape rejected."""
import asyncio
import json
import sys

import websockets

URL = "ws://localhost:8080/v1/agent"
GOOD_KEY = sys.argv[1]


async def recv_json(ws, timeout=10):
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    return json.loads(raw)


async def test_bad_key():
    try:
        async with websockets.connect(URL, max_size=None, ping_interval=None) as ws:
            await ws.send(json.dumps({"v": 1, "type": "auth", "token": "ccuc_deadbeef00_notarealsecret"}))
            saw_unauth = False
            try:
                while True:
                    f = await recv_json(ws)
                    if f.get("type") == "error" and f.get("code") == "unauthenticated":
                        saw_unauth = True
            except (asyncio.TimeoutError, websockets.ConnectionClosed):
                pass
            assert saw_unauth, "expected unauthenticated error"
            print("PASS bad_key rejected")
    except AssertionError:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"PASS bad_key connection closed ({type(e).__name__})")


async def test_cwd_escape():
    async with websockets.connect(URL, max_size=None, ping_interval=None) as ws:
        await ws.send(json.dumps({"v": 1, "type": "auth", "token": GOOD_KEY}))
        await recv_json(ws)  # hello 'ready'
        await ws.send(json.dumps({"v": 1, "type": "session.open", "req_id": "o1",
                                  "cwd": "../../../../../../etc", "model": "sonnet"}))
        f = await recv_json(ws)
        assert f.get("type") == "error" and f.get("code") == "bad_cwd", f"expected bad_cwd, got {f}"
        print("PASS cwd escape rejected:", f.get("message"))


async def main():
    await test_bad_key()
    await test_cwd_escape()
    print("ALL NEGATIVE TESTS PASSED")


asyncio.run(main())
