"""Transport-agnostic session provisioning, shared by the WS gateway and the
one-shot HTTP endpoint.

`provision_session` holds the whole "validate -> resolve work dir -> acquire seat
-> resolve mode/tools/skills/MCP -> build Session -> spawn driver" policy that
used to live inside `Connection._open`. It raises `ProvisionError` instead of
writing frames, so both front doors enforce exactly the same rules and each one
decides how to render a failure (WS error frame vs HTTP status).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ccuc.auth import Scope
from ccuc.bundles import compose_skills
from ccuc.driver import StreamingDriver
from ccuc.models import Session, SessionStatus

logger = logging.getLogger(__name__)


class ProvisionError(Exception):
    """A session could not be provisioned. `code` matches the wire error codes."""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass
class ProvisionResult:
    session: Session
    driver: StreamingDriver
    seat: object
    skills_composed: list[str] = field(default_factory=list)
    skill_warnings: list[str] = field(default_factory=list)


def resolve_tools(scope: Scope, req_allowed, req_disallowed):
    """Effective tool policy = key scope intersected with the request."""
    if scope.allowed_tools is not None and req_allowed is not None:
        allowed = [t for t in req_allowed if t in scope.allowed_tools]
    elif scope.allowed_tools is not None:
        allowed = list(scope.allowed_tools)
    elif req_allowed is not None:
        allowed = list(req_allowed)
    else:
        allowed = None
    disallowed = list(dict.fromkeys(list(scope.disallowed_tools) + list(req_disallowed or [])))
    return allowed, disallowed


def resolve_work_dir(settings, scope: Scope, session_id: str, req_cwd):
    """Return (path, error). Enforces the working_dir_root jail when set;
    otherwise allocates an ephemeral per-session scratch dir."""
    if scope.working_dir_root:
        base = Path(scope.working_dir_root).expanduser().resolve()
        if req_cwd and scope.allow_client_subdir:
            target = (base / req_cwd).resolve()
            if target != base and base not in target.parents:
                return None, "cwd escapes working_dir_root"
        else:
            target = base
        target.mkdir(parents=True, exist_ok=True)
        return target, None
    target = settings.sessions_dir / session_id / "work"
    target.mkdir(parents=True, exist_ok=True)
    return target, None


async def acquire_seat(seat_pool, settings, prefer_seat_id=None):
    """Reserve a warm slot, waiting (bounded) if the pool is saturated."""
    deadline = time.monotonic() + settings.max_queue_wait_s
    while True:
        seat = seat_pool.acquire(prefer_seat_id)
        if seat is not None:
            return seat
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.25)


def check_seat_health(seat_pool, session: Session, driver) -> None:
    """Cool a seat down if the CLI reported a genuine rate-limit rejection."""
    rl = getattr(driver, "last_rate_limit", None)
    if not isinstance(rl, dict):
        return
    status = (rl.get("status") or "").lower()
    # "allowed" and "allowed_warning" (approaching limits) are healthy; only
    # a genuine rejection cools the seat down.
    if status in {"rejected", "blocked", "exhausted", "exceeded", "throttled"}:
        cooldown = 300.0
        resets = rl.get("resetsAt")
        if isinstance(resets, (int, float)):
            cooldown = max(60.0, min(3600.0, resets - time.time()))
        seat_pool.mark_unhealthy(session.seat_id, f"rate_limit:{status}", cooldown_s=cooldown)


async def provision_session(
    *,
    settings,
    scope: Scope,
    key_id: str,
    seat_pool,
    session_manager,
    shared_mcp_path,
    on_frame_factory,
    req,
    driver_settings=None,
) -> ProvisionResult:
    """Open a new session and spawn its `claude` process.

    `req` is duck-typed: any object exposing cwd / model / mode / skills /
    allowed_tools / disallowed_tools / append_system_prompt (the WS
    `SessionOpen` frame and the HTTP `CompletionRequest` both qualify).

    `on_frame_factory(session_id)` returns the driver's frame sink — a factory
    because the session id is minted here.

    On success the session is registered with the SessionManager (metadata +
    driver) and the seat slot is held by the caller, who owns teardown. On
    failure the seat is released and `ProvisionError` is raised.
    """
    if session_manager.count_active_for_key(key_id) >= scope.max_concurrent_sessions:
        raise ProvisionError(
            "too_many_sessions", f"key limit {scope.max_concurrent_sessions} reached"
        )

    model = req.model or scope.model or settings.default_model
    if scope.allowed_models and model not in scope.allowed_models:
        raise ProvisionError("model_not_allowed", f"model {model} not permitted for this key")

    session_id = str(uuid.uuid4())
    work_dir, err = resolve_work_dir(settings, scope, session_id, req.cwd)
    if err:
        raise ProvisionError("bad_cwd", err)

    seat = await acquire_seat(seat_pool, settings)
    if seat is None:
        raise ProvisionError(
            "seat_pool_saturated", "no free seat slot; retry shortly", retryable=True
        )

    mode = (req.mode or settings.default_mode).lower()
    if mode not in ("chat", "agent"):
        mode = settings.default_mode
    client_append = req.append_system_prompt or ""

    if mode == "agent":
        # full-agentic: skill packs + global MCP + tool allow/deny policy
        allowed_tools, disallowed_tools = resolve_tools(scope, req.allowed_tools, req.disallowed_tools)
        packs = req.skills if req.skills is not None else list(settings.default_skill_packs)
        composed, skill_errors = compose_skills(work_dir, packs, settings.bundles_path)
        mcp_path = shared_mcp_path if settings.shared_mcp_enabled else None
        append_system_prompt = client_append
    else:
        # chat: pure completion — no tools, no MCP, no skills; steer to inline answers
        allowed_tools, disallowed_tools = None, []
        packs, composed, skill_errors = [], [], []
        mcp_path = None
        append_system_prompt = "\n\n".join(p for p in (settings.chat_system_prompt, client_append) if p)

    session = Session(
        session_id=session_id, key_id=key_id, seat_id=seat.seat_id,
        work_dir=str(work_dir), model=model, mode=mode, append_system_prompt=append_system_prompt,
        allowed_tools=allowed_tools, disallowed_tools=disallowed_tools,
        mcp_config_path=mcp_path, skill_packs=packs,
    )
    driver = StreamingDriver(driver_settings or settings, session, seat)
    try:
        await driver.start(on_frame_factory(session_id))
    except Exception as e:  # noqa: BLE001
        logger.exception("failed to start session %s", session_id)
        seat_pool.release(seat.seat_id)
        raise ProvisionError("session_start_failed", str(e)) from e

    session.status = SessionStatus.IDLE
    session_manager.register(session)
    session_manager.register_driver(session_id, driver)
    return ProvisionResult(
        session=session, driver=driver, seat=seat,
        skills_composed=composed, skill_warnings=list(skill_errors),
    )
