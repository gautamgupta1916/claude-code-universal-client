"""SQLite audit log: a durable record of session lifecycle + tool use.

Every tool the agent invokes, every turn's cost, and open/close/error events are
written here (same DB file as the KeyStore, own connection). Powers /stats and
/metrics and gives an after-the-fact trail of what each key's sessions did.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class AuditLog:
    def __init__(self, settings):
        self.settings = settings
        p = settings.db_path
        p.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(p), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS audit_log(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, key_id TEXT, session_id TEXT,
                event_type TEXT, tool_name TEXT, detail TEXT, cost_usd REAL
            )"""
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_log(session_id)")
        self._conn.commit()

    def record(self, event_type: str, key_id: Optional[str] = None, session_id: Optional[str] = None,
               tool_name: Optional[str] = None, detail: Optional[str] = None,
               cost_usd: Optional[float] = None) -> None:
        try:
            self._conn.execute(
                "INSERT INTO audit_log(ts,key_id,session_id,event_type,tool_name,detail,cost_usd) VALUES(?,?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), key_id, session_id, event_type, tool_name, detail, cost_usd),
            )
            self._conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.debug("audit write failed: %s", e)

    def total_cost(self) -> float:
        cur = self._conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM audit_log WHERE event_type='turn_complete'")
        return float(cur.fetchone()[0] or 0.0)

    def event_counts(self) -> dict:
        cur = self._conn.execute("SELECT event_type, COUNT(*) FROM audit_log GROUP BY event_type")
        return {row[0]: row[1] for row in cur.fetchall()}

    def tool_use_counts(self) -> dict:
        cur = self._conn.execute(
            "SELECT tool_name, COUNT(*) FROM audit_log WHERE event_type='tool_use' GROUP BY tool_name ORDER BY 2 DESC"
        )
        return {row[0]: row[1] for row in cur.fetchall()}

    def recent(self, limit: int = 50, session_id: Optional[str] = None) -> list[dict]:
        if session_id:
            cur = self._conn.execute(
                "SELECT ts,key_id,session_id,event_type,tool_name,detail,cost_usd FROM audit_log "
                "WHERE session_id=? ORDER BY id DESC LIMIT ?", (session_id, limit))
        else:
            cur = self._conn.execute(
                "SELECT ts,key_id,session_id,event_type,tool_name,detail,cost_usd FROM audit_log "
                "ORDER BY id DESC LIMIT ?", (limit,))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
