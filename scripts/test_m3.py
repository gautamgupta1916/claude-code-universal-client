#!/usr/bin/env python3
"""M3 verification: idle-suspend + transparent re-warm, per-key concurrency,
daily turn quota, seat saturation.

Usage: test_m3.py <dev_key> <conc_key(max_concurrent=1)> <quota_key(max_turns=1)>
Server must run with a short idle TTL + low queue wait + seat_max_concurrency=1.
"""
import asyncio
import json
import sys
import time
import urllib.request

import websockets

URL = "ws://localhost:8080/v1/agent"
DEV, CONC, QUOTA = sys.argv[1], sys.argv[2], sys.argv[3]


def _get(path):
    with urllib.request.urlopen("http://localhost:8080" + path) as r:
        return json.load(r)


class Conn:
    def __init__(self, ws, key):
        self.ws = ws
        self.key = key
        self.session = None
        self.text = ""
        self._q = asyncio.Queue()

    async def start(self):
        self._rt = asyncio.create_task(self._reader())
        await self.ws.send(json.dumps({"v": 1, "type": "auth", "token": self.key}))

    async def _reader(self):
        async for raw in self.ws:
            f = json.loads(raw)
            if f.get("type") in ("assistant.delta", "assistant.text"):
                self.text += f.get("text", "")
            await self._q.put(f)

    async def _wait(self, pred, timeout=120):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                f = await asyncio.wait_for(self._q.get(), timeout=max(0.01, end - time.monotonic()))
            except asyncio.TimeoutError:
                return None
            if pred(f):
                return f
        return None

    async def open(self, req="o", skills=None):
        m = {"v": 1, "type": "session.open", "req_id": req, "model": "sonnet"}
        if skills is not None:
            m["skills"] = skills
        await self.ws.send(json.dumps(m))
        f = await self._wait(lambda x: x.get("req_id") == req and x.get("type") in ("session.ready", "error"))
        if f and f.get("type") == "session.ready":
            self.session = f.get("session")
        return f

    async def turn(self, text, req="m", timeout=180):
        if not self.session:
            return {"type": "error", "code": "no_session"}
        self.text = ""
        await self.ws.send(json.dumps({"v": 1, "type": "user.message", "req_id": req,
                                       "session_id": self.session, "text": text}))
        return await self._wait(lambda x: x.get("req_id") == req and x.get("type") in ("turn.complete", "error"), timeout)

    async def close(self):
        if self.session:
            await self.ws.send(json.dumps({"v": 1, "type": "session.close", "session_id": self.session}))
            await asyncio.sleep(0.3)
        self._rt.cancel()


def conn():
    return websockets.connect(URL, max_size=None, ping_interval=None)


async def main():
    results = {}

    # ---- A: idle-suspend + transparent re-warm + context ----
    async with conn() as ws:
        c = Conn(ws, DEV)
        await c.start()
        await c.open()
        await c.turn("Remember this codeword: ZEBRA-9. Acknowledge in a few words.")
        suspended = False
        warm = None
        for _ in range(25):
            await asyncio.sleep(1)
            warm = _get("/health")["warm_processes"]
            if warm == 0:
                suspended = True
                break
        st = [s["status"] for s in _get("/sessions") if s["session_id"] == c.session]
        print(f"[A] warm_processes after idle={warm}  session status={st}")
        await c.turn("What codeword did I ask you to remember? Reply with only the codeword.")
        results["A idle-suspend+rewarm+context"] = suspended and st == ["suspended"] and "ZEBRA-9" in c.text
        print("   rewarmed reply:", c.text.strip()[:50])
        await c.close()

    # ---- B: per-key concurrency (conc key max_concurrent=1) ----
    async with conn() as ws:
        c = Conn(ws, CONC)
        await c.start()
        f1 = await c.open(req="b1")
        f2 = await c.open(req="b2")
        results["B per-key concurrency"] = (
            f1.get("type") == "session.ready" and f2.get("type") == "error" and f2.get("code") == "too_many_sessions"
        )
        print("[B] 2nd open code:", f2.get("code"))
        await c.close()

    # ---- C: daily turn quota (quota key max_turns_per_day=1) ----
    async with conn() as ws:
        c = Conn(ws, QUOTA)
        await c.start()
        await c.open()
        t1 = await c.turn("hi", req="c1")
        t2 = await c.turn("hi again", req="c2")
        results["C daily turn quota"] = (
            t1.get("type") == "turn.complete" and t2.get("type") == "error" and t2.get("code") == "quota_exceeded"
        )
        print("[C] turn1:", t1.get("type"), "| turn2:", t2.get("code") or t2.get("type"))
        await c.close()

    # ---- D: seat saturation (seat_max_concurrency=1) ----
    async with conn() as ws:
        c = Conn(ws, DEV)
        await c.start()
        fa = await c.open(req="d1")
        fb = await c.open(req="d2")
        results["D seat saturation"] = (
            fa.get("type") == "session.ready" and fb.get("type") == "error" and fb.get("code") == "seat_pool_saturated"
        )
        print("[D] 2nd concurrent open code:", fb.get("code"))
        # close both
        for sid in {fa.get("session"), fb.get("session")} - {None}:
            await ws.send(json.dumps({"v": 1, "type": "session.close", "session_id": sid}))
        await asyncio.sleep(0.3)
        c._rt.cancel()

    print("\n=== M3 RESULTS ===")
    ok = True
    for k, v in results.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
        ok = ok and v
    print("M3:", "ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
