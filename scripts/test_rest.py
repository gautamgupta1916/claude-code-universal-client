#!/usr/bin/env python3
"""One-shot HTTP mode verification: POST /v1/agent/completions.

Start the gateway with a live seat (e.g. `ccuc`) on localhost:8080, then:
    python scripts/test_rest.py [key]

Checks:
  chat       -> a direct answer, zero tool calls, a cost is reported.
  agent      -> at least one tool call, each with a populated result.
  no leak    -> seat slots / warm processes / session count return to baseline,
                and no ephemeral session dir is left behind on disk.
  auth       -> a bogus key gets 401 in the {"error": {...}} envelope.
  timeout    -> timeout_s=1 on a non-trivial prompt gets 504.
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("CCUC_BASE", "http://localhost:8080")
KEY = sys.argv[1] if len(sys.argv) > 1 else "devkey"
SESSIONS_DIR = os.path.join(".ccuc", "sessions")

failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")
    if not ok:
        failures.append(name)


def get(path):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=30) as r:
        return json.load(r)


def post(body, key=KEY, timeout=600):
    req = urllib.request.Request(
        f"{BASE}/v1/agent/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw}


def snapshot():
    h = get("/health")
    dirs = set(os.listdir(SESSIONS_DIR)) if os.path.isdir(SESSIONS_DIR) else set()
    return h["seat_slots"]["active"], h["warm_processes"], len(get("/sessions")), dirs


def main():
    before = snapshot()
    print(f"baseline: active_slots={before[0]} warm={before[1]} sessions={before[2]}")

    print("\n[1] chat mode")
    status, body = post({"prompt": "What is 17*23? Reply with just the number.", "mode": "chat"})
    check("200", status == 200, str(body)[:200])
    if status == 200:
        check("answer contains 391", "391" in body.get("text", ""), repr(body.get("text", ""))[:120])
        check("no tool calls", body.get("tool_calls") == [], str(body.get("tool_calls"))[:120])
        check("cost reported", body.get("cost_usd") is not None)
        check("session_id present", bool(body.get("session_id")))
        check("mode echoed", body.get("mode") == "chat")

    print("\n[2] agent mode")
    status, body = post({
        "prompt": "Create a file called hello.txt containing the word hi, then tell me it is done.",
        "mode": "agent",
    })
    check("200", status == 200, str(body)[:200])
    if status == 200:
        calls = body.get("tool_calls") or []
        check("a tool fired", len(calls) > 0, f"{len(calls)} call(s)")
        check("tool results filled", all(c.get("result") is not None for c in calls),
              str([c.get("name") for c in calls])[:120])
        check("mode echoed", body.get("mode") == "agent")

    print("\n[3] no leaks after the turns")
    after = snapshot()
    check("seat slots restored", after[0] == before[0], f"{before[0]} -> {after[0]}")
    check("no warm processes left", after[1] == before[1], f"{before[1]} -> {after[1]}")
    check("session registry clean", after[2] == before[2], f"{before[2]} -> {after[2]}")
    check("no session dirs left", after[3] == before[3], str(after[3] - before[3])[:160])

    print("\n[4] auth")
    status, body = post({"prompt": "hi", "mode": "chat"}, key="ccuc_deadbeef00_nope")
    check("401", status == 401, str(body)[:160])
    check("error envelope", (body.get("error") or {}).get("code") == "unauthenticated", str(body)[:160])

    print("\n[5] timeout")
    status, body = post({
        "prompt": "Count slowly from 1 to 500, writing out every number in words.",
        "mode": "chat", "timeout_s": 1,
    })
    check("504", status == 504, str(body)[:160])
    check("retryable", (body.get("error") or {}).get("retryable") is True, str(body)[:160])

    print("\n[6] no leaks after the timeout")
    after = snapshot()
    check("seat slots restored", after[0] == before[0], f"{before[0]} -> {after[0]}")
    check("no warm processes left", after[1] == before[1], f"{before[1]} -> {after[1]}")

    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILURE(S): {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
