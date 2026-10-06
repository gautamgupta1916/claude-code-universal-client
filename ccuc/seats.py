from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# Env vars stripped from the child so subscription OAuth (the seat) is used,
# and gateway secrets never leak into the agent. Anything prefixed CCUC_ is
# also dropped (see child_env). A stricter allowlist is an M4 hardening item.
_ENV_DENY = {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"}


@dataclass
class Seat:
    """One OAuth subscription identity == one CLAUDE_CONFIG_DIR.

    config_dir is None for the *ambient* seat: the process inherits the machine's
    default `claude` login (local dev, e.g. macOS Keychain).

    `active` = warm processes currently running on this seat (the load metric and
    the saturation gate). A SUSPENDED session stays *pinned* to its seat (affinity,
    via Session.seat_id) but does not hold an `active` slot.
    """

    seat_id: str
    config_dir: Optional[str] = None
    active: int = 0
    max_concurrency: int = 1
    status: str = "active"            # active | logged_out | rate_limited
    cooldown_until: float = 0.0       # wall-clock ts; unhealthy until then
    last_error: Optional[str] = None

    def healthy(self) -> bool:
        return self.status == "active" and time.time() >= self.cooldown_until

    def has_capacity(self) -> bool:
        return self.active < self.max_concurrency

    def child_env(self, settings) -> dict:
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("CCUC_") and k not in _ENV_DENY
        }
        if self.config_dir:
            env["CLAUDE_CONFIG_DIR"] = self.config_dir
        prev = env.get("NODE_OPTIONS", "")
        env["NODE_OPTIONS"] = (prev + f" --max-old-space-size={settings.node_heap_mb}").strip()
        return env


class SeatPool:
    """Pool of OAuth seats with capacity gating, sticky affinity, and health.

    `acquire()` hands out a warm slot on a healthy seat (least-loaded), or the
    session's pinned seat on resume. Returns None when nothing is available so the
    caller can queue or reject.
    """

    def __init__(self, settings):
        self.settings = settings
        self.seats: list[Seat] = []
        cap = settings.seat_max_concurrency
        if settings.seat_dirs:
            for i, d in enumerate(settings.seat_dirs):
                self.seats.append(Seat(seat_id=f"seat-{i:02d}", config_dir=d, max_concurrency=cap))
        else:
            self.seats.append(Seat(seat_id="seat-00", config_dir=None, max_concurrency=cap))
        self._by_id = {s.seat_id: s for s in self.seats}

    def get(self, seat_id: str) -> Optional[Seat]:
        return self._by_id.get(seat_id)

    def acquire(self, prefer_seat_id: Optional[str] = None) -> Optional[Seat]:
        """Reserve a warm slot. With prefer_seat_id (resume affinity) only that seat
        is considered; otherwise the least-loaded healthy seat with capacity."""
        if prefer_seat_id is not None:
            seat = self._by_id.get(prefer_seat_id)
            if seat and seat.healthy() and seat.has_capacity():
                seat.active += 1
                return seat
            return None
        candidates = [s for s in self.seats if s.healthy() and s.has_capacity()]
        if not candidates:
            return None
        seat = min(candidates, key=lambda s: s.active)
        seat.active += 1
        return seat

    def release(self, seat_id: str) -> None:
        seat = self._by_id.get(seat_id)
        if seat:
            seat.active = max(0, seat.active - 1)

    def mark_unhealthy(self, seat_id: str, reason: str, cooldown_s: float = 300.0) -> None:
        seat = self._by_id.get(seat_id)
        if not seat:
            return
        seat.status = "rate_limited" if "rate" in reason or "limit" in reason else "logged_out"
        seat.cooldown_until = time.time() + cooldown_s
        seat.last_error = reason
        logger.warning("seat %s marked %s (%.0fs cooldown): %s", seat_id, seat.status, cooldown_s, reason)

    def mark_healthy(self, seat_id: str) -> None:
        seat = self._by_id.get(seat_id)
        if seat:
            seat.status = "active"
            seat.cooldown_until = 0.0
            seat.last_error = None

    def capacity(self) -> tuple[int, int]:
        """(active_slots, total_slots) across healthy seats."""
        total = sum(s.max_concurrency for s in self.seats if s.healthy())
        active = sum(s.active for s in self.seats)
        return active, total
