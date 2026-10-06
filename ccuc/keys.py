"""SQLite-backed API key store.

Key string format: ``ccuc_<key_id>_<secret>`` where ``key_id`` is 10 hex chars
(stored in plaintext, the O(1) lookup handle and the safe-to-log identifier) and
``secret`` is a high-entropy token. We store only ``HMAC-SHA256(pepper, secret)``;
the pepper lives outside the DB (env or a 0600 file under the data dir), so a DB
leak alone cannot verify keys. Verification is constant-time.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from ccuc.auth import Principal, Scope

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class KeyStore:
    def __init__(self, settings):
        self.settings = settings
        self.db_path = settings.db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._pepper = self._load_or_create_pepper()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS api_keys(
                key_id       TEXT PRIMARY KEY,
                name         TEXT,
                key_hash     TEXT NOT NULL,
                scopes_json  TEXT NOT NULL,
                created_at   TEXT,
                last_used_at TEXT,
                revoked_at   TEXT
            )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS usage_counters(
                key_id TEXT, day TEXT, turns INTEGER,
                PRIMARY KEY (key_id, day)
            )"""
        )
        self._conn.commit()

    def _load_or_create_pepper(self) -> bytes:
        if self.settings.key_pepper:
            return self.settings.key_pepper.encode()
        p = self.settings.data_path / "pepper"
        if p.exists():
            return p.read_bytes()
        self.settings.data_path.mkdir(parents=True, exist_ok=True)
        val = secrets.token_bytes(32)
        p.write_bytes(val)
        try:
            p.chmod(0o600)
        except OSError:
            pass
        logger.warning("generated a persistent key pepper at %s (set CCUC_KEY_PEPPER to override)", p)
        return val

    def _hash(self, secret: str) -> str:
        return hmac.new(self._pepper, secret.encode(), hashlib.sha256).hexdigest()

    # ---- admin operations ----

    def create(self, name: str, scope: Scope) -> tuple[str, str]:
        """Mint a key. Returns (full_key_shown_once, key_id)."""
        key_id = secrets.token_hex(5)
        secret = secrets.token_urlsafe(24)
        full = f"ccuc_{key_id}_{secret}"
        self._conn.execute(
            "INSERT INTO api_keys(key_id,name,key_hash,scopes_json,created_at) VALUES(?,?,?,?,?)",
            (key_id, name, self._hash(secret), scope.model_dump_json(), _now_iso()),
        )
        self._conn.commit()
        return full, key_id

    def list(self) -> list[dict]:
        cur = self._conn.execute(
            "SELECT key_id,name,created_at,last_used_at,revoked_at,scopes_json FROM api_keys ORDER BY created_at"
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def get(self, key_id: str) -> Optional[dict]:
        cur = self._conn.execute(
            "SELECT key_id,name,created_at,last_used_at,revoked_at,scopes_json FROM api_keys WHERE key_id=?",
            (key_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return dict(zip([c[0] for c in cur.description], row))

    def revoke(self, key_id: str) -> bool:
        cur = self._conn.execute(
            "UPDATE api_keys SET revoked_at=? WHERE key_id=? AND revoked_at IS NULL",
            (_now_iso(), key_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    # ---- usage / quota ----

    def turns_today(self, key_id: str) -> int:
        day = datetime.now(timezone.utc).date().isoformat()
        cur = self._conn.execute(
            "SELECT turns FROM usage_counters WHERE key_id=? AND day=?", (key_id, day)
        )
        row = cur.fetchone()
        return row[0] if row else 0

    def record_turn(self, key_id: str) -> None:
        day = datetime.now(timezone.utc).date().isoformat()
        self._conn.execute(
            "INSERT INTO usage_counters(key_id, day, turns) VALUES(?,?,1) "
            "ON CONFLICT(key_id, day) DO UPDATE SET turns = turns + 1",
            (key_id, day),
        )
        self._conn.commit()

    # ---- verification ----

    def validate(self, token: Optional[str]) -> Optional[Principal]:
        if not token:
            return None
        # dev fallback (local only): permissive scope, ephemeral working dir
        if self.settings.dev_api_key and hmac.compare_digest(token, self.settings.dev_api_key):
            return Principal(key_id="dev", scope=Scope())
        if not token.startswith("ccuc_"):
            return None
        parts = token.split("_", 2)  # key_id is hex (no underscore); secret may contain _
        if len(parts) != 3:
            return None
        _, key_id, secret = parts
        cur = self._conn.execute(
            "SELECT key_hash,scopes_json,revoked_at FROM api_keys WHERE key_id=?", (key_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        key_hash, scopes_json, revoked_at = row
        if revoked_at:
            return None
        if not hmac.compare_digest(key_hash, self._hash(secret)):
            return None
        scope = Scope.model_validate_json(scopes_json)
        if scope.expires_at:
            try:
                if datetime.fromisoformat(scope.expires_at) < datetime.now(timezone.utc):
                    return None
            except ValueError:
                pass
        self._conn.execute("UPDATE api_keys SET last_used_at=? WHERE key_id=?", (_now_iso(), key_id))
        self._conn.commit()
        return Principal(key_id=key_id, scope=scope)
