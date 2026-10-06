from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SessionStatus(str, Enum):
    STARTING = "starting"       # process spawning
    RUNNING = "running"         # a turn is in flight
    IDLE = "idle"               # process warm, between turns
    SUSPENDED = "suspended"     # process torn down (idle timeout); resumable via --resume
    INTERRUPTED = "interrupted"  # process died mid-turn; resumable
    CLOSED = "closed"           # closed cleanly
    FAILED = "failed"           # terminal error


@dataclass
class Session:
    session_id: str            # our UUID; passed to the CLI as --session-id (== resume key)
    key_id: str                # owning API key/principal
    seat_id: str               # sticky OAuth-seat binding (required for resume)
    work_dir: str              # per-session working directory (required for resume)
    model: str
    mode: str = "chat"         # "chat" (no tools) | "agent" (full-agentic); fixed for the session's life
    append_system_prompt: str = ""
    allowed_tools: Optional[list[str]] = None
    disallowed_tools: list[str] = field(default_factory=list)
    mcp_config_path: Optional[str] = None
    skill_packs: list[str] = field(default_factory=list)
    status: SessionStatus = SessionStatus.STARTING
    turn_count: int = 0
    native_session_id: Optional[str] = None  # reported by the CLI's system/init event
    created_at: datetime = field(default_factory=_now)
    last_activity_at: datetime = field(default_factory=_now)
    error: Optional[str] = None
    attached: bool = False  # transient: currently bound to a live WS connection + process
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)

    def touch(self) -> None:
        self.last_activity_at = _now()
