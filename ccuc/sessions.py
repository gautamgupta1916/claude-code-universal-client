"""Cross-connection session registry + process (driver) ownership.

Holds Session metadata AND the live driver for each warm session so that a global
sweeper can idle-suspend sessions independently of the WebSocket connection that
opened them. In-memory for now; SQLite persistence (survive a server restart) is
an M4 item.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ccuc.models import Session, SessionStatus

logger = logging.getLogger(__name__)

_TERMINAL = (SessionStatus.CLOSED, SessionStatus.FAILED)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SessionManager:
    def __init__(self, settings):
        self.settings = settings
        self._sessions: dict[str, Session] = {}
        self._drivers: dict[str, object] = {}  # sid -> live StreamingDriver (warm sessions only)

    # ---- session registry ----

    def register(self, session: Session) -> None:
        self._sessions[session.session_id] = session

    def get(self, session_id: str) -> Optional[Session]:
        return self._sessions.get(session_id)

    def remove(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        self._drivers.pop(session_id, None)

    def list(self) -> list[Session]:
        return list(self._sessions.values())

    def count_active_for_key(self, key_id: str) -> int:
        """Open (non-terminal) sessions owned by a key — the per-key concurrency count."""
        return sum(1 for s in self._sessions.values() if s.key_id == key_id and s.status not in _TERMINAL)

    # ---- driver registry (warm sessions) ----

    def register_driver(self, session_id: str, driver: object) -> None:
        self._drivers[session_id] = driver

    def unregister_driver(self, session_id: str) -> None:
        self._drivers.pop(session_id, None)

    def get_driver(self, session_id: str):
        return self._drivers.get(session_id)

    # ---- sweepers (run periodically from app lifespan) ----

    async def sweep_idle(self, seat_pool) -> int:
        """Suspend warm IDLE sessions idle longer than idle_ttl_s: kill the process,
        free the seat slot, keep the (resumable) session record. Returns count suspended."""
        ttl = self.settings.idle_ttl_s
        suspended = 0
        for sid, session in list(self._sessions.items()):
            driver = self._drivers.get(sid)
            if driver is None or session.status != SessionStatus.IDLE:
                continue
            if (_now() - session.last_activity_at).total_seconds() < ttl:
                continue
            async with session._lock:
                if session.status != SessionStatus.IDLE:
                    continue
                idle = (_now() - session.last_activity_at).total_seconds()
                if idle < ttl:
                    continue
                session.status = SessionStatus.SUSPENDED
                self._drivers.pop(sid, None)
                try:
                    await driver.stop()
                except Exception:  # noqa: BLE001
                    pass
                seat_pool.release(session.seat_id)
            suspended += 1
            logger.info("idle-suspended session=%s (idle %.0fs) seat=%s freed", sid, idle, session.seat_id)
        return suspended

    def sweep_stale(self) -> int:
        """Reap SUSPENDED sessions idle beyond stale_ttl_s: mark FAILED (terminal, so
        they stop counting toward per-key concurrency). Their disks are cleaned by
        cleanup_disk. Returns count reaped."""
        ttl = self.settings.stale_ttl_s
        now = _now()
        reaped = 0
        for sid, session in list(self._sessions.items()):
            if session.status != SessionStatus.SUSPENDED:
                continue
            if (now - session.last_activity_at).total_seconds() < ttl:
                continue
            session.status = SessionStatus.FAILED
            session.error = "stale (idle beyond stale_ttl_s)"
            self._drivers.pop(sid, None)
            reaped += 1
            logger.info("stale-reaped session=%s", sid)
        return reaped

    async def cleanup_disk(self, max_age_hours: int = 72) -> None:
        """Delete ephemeral per-session working dirs (under sessions_dir) for terminal
        sessions older than max_age_hours. Never touches scope working_dir_root dirs."""
        base = self.settings.sessions_dir
        now = _now()
        for session in list(self._sessions.values()):
            if session.status not in _TERMINAL:
                continue
            age_h = (now - session.last_activity_at).total_seconds() / 3600
            if age_h <= max_age_hours:
                continue
            wd = Path(session.work_dir)
            # only remove if it's the ephemeral per-session dir we created
            try:
                inside = wd.is_relative_to(base) if hasattr(wd, "is_relative_to") else str(wd).startswith(str(base))
            except Exception:  # noqa: BLE001
                inside = False
            if inside and wd.exists():
                try:
                    shutil.rmtree(wd.parent if wd.name == "work" else wd)
                    logger.info("cleaned session dir for %s (%.0fh old)", session.session_id, age_h)
                except OSError as e:
                    logger.warning("dir cleanup failed for %s: %s", session.session_id, e)
            self._sessions.pop(session.session_id, None)
