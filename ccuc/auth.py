"""Identity types: Scope (per-key policy) and Principal (an authenticated caller).

These are kept dependency-light and free of the KeyStore so both `keys.py` and
`gateway.py` can import them without cycles.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from pydantic import BaseModel


class Scope(BaseModel):
    """What an API key is allowed to do. Stored as JSON in the KeyStore."""

    allowed_tools: Optional[list[str]] = None   # None = all tools; else an allowlist
    disallowed_tools: list[str] = []
    working_dir_root: Optional[str] = None       # None = ephemeral per-session scratch dir
    allow_client_subdir: bool = True             # may the client pick a subdir under the root?
    model: Optional[str] = None                  # default model for this key
    allowed_models: Optional[list[str]] = None   # if set, restrict which models may be requested
    max_concurrent_sessions: int = 4
    max_turns_per_day: Optional[int] = None
    expires_at: Optional[str] = None             # ISO 8601; None = never


@dataclass
class Principal:
    """An authenticated caller resolved from a validated API key."""

    key_id: str
    scope: Scope
