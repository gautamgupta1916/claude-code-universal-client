#!/usr/bin/env python3
"""Minimal zero-dependency MCP stdio server (scaffold).

Speaks newline-delimited JSON-RPC 2.0 over stdin/stdout per the MCP stdio
transport. Exposes two demo tools (echo, add). Replace with your own servers.
"""

import json
import sys

TOOLS = [
    {
        "name": "echo",
        "description": "Echo back the provided text.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "add",
        "description": "Add two numbers and return the sum.",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
    },
]


def send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _result(mid, res):
    send({"jsonrpc": "2.0", "id": mid, "result": res})


def _error(mid, code, message):
    send({"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}})


def handle(req: dict) -> None:
    method = req.get("method")
    mid = req.get("id")
    if method == "initialize":
        _result(mid, {
            "protocolVersion": req.get("params", {}).get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "ccuc-util", "version": "0.1.0"},
        })
    elif method in ("notifications/initialized", "initialized"):
        pass  # notification — no response
    elif method == "ping":
        _result(mid, {})
    elif method == "tools/list":
        _result(mid, {"tools": TOOLS})
    elif method == "tools/call":
        params = req.get("params", {})
        name = params.get("name")
        args = params.get("arguments", {}) or {}
        if name == "echo":
            _result(mid, {"content": [{"type": "text", "text": str(args.get("text", ""))}]})
        elif name == "add":
            try:
                total = float(args.get("a", 0)) + float(args.get("b", 0))
                total = int(total) if float(total).is_integer() else total
                _result(mid, {"content": [{"type": "text", "text": str(total)}]})
            except (TypeError, ValueError):
                _result(mid, {"content": [{"type": "text", "text": "invalid numbers"}], "isError": True})
        else:
            _error(mid, -32601, f"unknown tool: {name}")
    elif mid is not None:
        _error(mid, -32601, f"method not found: {method}")


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            handle(req)
        except Exception as e:  # noqa: BLE001
            if isinstance(req, dict) and req.get("id") is not None:
                _error(req.get("id"), -32603, f"internal error: {e}")


if __name__ == "__main__":
    main()
