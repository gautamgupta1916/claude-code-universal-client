"""ccuc-keys — admin CLI for minting and managing API keys."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ccuc.auth import Scope
from ccuc.config import Settings
from ccuc.keys import KeyStore


def main() -> int:
    ap = argparse.ArgumentParser(prog="ccuc-keys", description="Manage CCUC API keys")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="mint a new key (plaintext shown once)")
    c.add_argument("--name", required=True)
    c.add_argument("--root", help="working_dir_root the key is confined to (default: ephemeral scratch)")
    c.add_argument("--model", help="default model for this key")
    c.add_argument("--allow-tool", action="append", default=None, help="allowlist a tool (repeatable)")
    c.add_argument("--deny-tool", action="append", default=[], help="denylist a tool (repeatable)")
    c.add_argument("--max-concurrent", type=int, default=4)
    c.add_argument("--max-turns-per-day", type=int, default=None)
    c.add_argument("--no-subdir", action="store_true", help="forbid client-chosen subdirs under root")

    sub.add_parser("list", help="list keys")
    s = sub.add_parser("show", help="show one key")
    s.add_argument("key_id")
    r = sub.add_parser("revoke", help="revoke a key")
    r.add_argument("key_id")

    args = ap.parse_args()
    ks = KeyStore(Settings())

    if args.cmd == "create":
        scope = Scope(
            allowed_tools=args.allow_tool,
            disallowed_tools=args.deny_tool,
            working_dir_root=str(Path(args.root).expanduser().resolve()) if args.root else None,
            allow_client_subdir=not args.no_subdir,
            model=args.model,
            max_concurrent_sessions=args.max_concurrent,
            max_turns_per_day=args.max_turns_per_day,
        )
        full, kid = ks.create(args.name, scope)
        print("Created key (shown ONCE — store it now):")
        print("  ", full)
        print("   key_id:", kid)
    elif args.cmd == "list":
        rows = ks.list()
        if not rows:
            print("(no keys)")
        for row in rows:
            state = "REVOKED" if row["revoked_at"] else "active"
            print(f"{row['key_id']}  {row['name'] or '':24}  {state:8}  created={row['created_at']}")
    elif args.cmd == "show":
        row = ks.get(args.key_id)
        print(json.dumps(row, indent=2) if row else "not found")
    elif args.cmd == "revoke":
        print("revoked" if ks.revoke(args.key_id) else "not found / already revoked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
