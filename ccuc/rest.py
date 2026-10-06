"""One-shot HTTP endpoint: POST /v1/agent/completions.

One request = one session. The server opens a session, runs a single turn, then
kills the process and frees the seat before responding with a single blocking
JSON body. Strictly stateless: no resume, no continuation, no streaming — the
returned `session_id` is a correlation handle for logs and the audit trail only.

Anything long-running, multi-turn, or needing live progress belongs on the
WebSocket gateway (`ccuc/gateway.py`), which this endpoint deliberately does not
try to replace. Both share the same seat pool, session registry, quotas and
audit log, so capacity and observability stay unified.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ccuc.auth import Principal
from ccuc.models import SessionStatus
from ccuc.provision import ProvisionError, check_seat_health, provision_session

logger = logging.getLogger(__name__)
router = APIRouter()

# wire error code -> HTTP status
_STATUS = {
    "unauthenticated": 401,
    "forbidden": 403,
    "model_not_allowed": 403,
    "bad_cwd": 400,
    "bad_request": 400,
    "http_mode_disabled": 404,
    "too_many_sessions": 429,
    "quota_exceeded": 429,
    "seat_pool_saturated": 503,
    "turn_timeout": 504,
    "turn_failed": 502,
    "agent_error": 502,
    "session_start_failed": 500,
    "client_disconnected": 499,
}

_DISCONNECT_POLL_S = 1.0


class HttpError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False, detail: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.detail = detail

    def response(self) -> JSONResponse:
        status = _STATUS.get(self.code, 500)
        headers = {"Retry-After": "5"} if status == 503 else None
        return JSONResponse(
            status_code=status,
            headers=headers,
            content={"error": {
                "code": self.code, "message": self.message,
                "retryable": self.retryable, "detail": self.detail,
            }},
        )


# ---------------- request / response models ----------------

class CompletionRequest(BaseModel):
    prompt: str = Field(min_length=1)
    mode: Optional[Literal["chat", "agent"]] = None   # None -> server default_mode
    model: Optional[str] = None
    cwd: Optional[str] = None
    skills: Optional[list[str]] = None                # agent mode only; None -> default packs
    allowed_tools: Optional[list[str]] = None
    disallowed_tools: Optional[list[str]] = None
    append_system_prompt: Optional[str] = None
    timeout_s: Optional[float] = Field(default=None, gt=0)


class ToolCall(BaseModel):
    id: Optional[str] = None
    name: Optional[str] = None
    input: Optional[dict] = None
    result: Optional[str] = None
    is_error: Optional[bool] = None


class CompletionResponse(BaseModel):
    session_id: str            # correlation handle only — NOT resumable
    text: str
    messages: list[str]
    tool_calls: list[ToolCall]
    stop_reason: Optional[str] = None
    cost_usd: Optional[float] = None
    duration_ms: Optional[int] = None
    num_turns: Optional[int] = None
    usage: Optional[dict] = None
    is_error: bool = False
    model: Optional[str] = None
    mode: Optional[str] = None
    seat: Optional[str] = None
    skills: list[str] = Field(default_factory=list)


# ---------------- auth ----------------

def bearer_principal(request: Request) -> Principal:
    auth = request.headers.get("authorization") or ""
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else None
    principal = request.app.state.keystore.validate(token)
    if not principal:
        raise HttpError("unauthenticated", "invalid or missing API key")
    return principal


# ---------------- frame sink ----------------

class FrameCollector:
    """Driver frame sink that accumulates a turn instead of streaming it.

    Mirrors `Connection._on_frame_for` in gateway.py — same audit records, but
    the frames land in memory and become the JSON body.
    """

    def __init__(self, audit, key_id: str):
        self.audit = audit
        self.key_id = key_id
        self.messages: list[str] = []
        self.tool_calls: list[dict] = []
        self._by_id: dict[str, dict] = {}
        self.agent_error: Optional[str] = None

    def __call__(self, session_id: str):
        async def on_frame(fr: dict) -> None:
            t = fr.get("type")
            if t == "assistant.text":
                text = fr.get("text") or ""
                if text:
                    self.messages.append(text)
            elif t == "tool.use":
                inp = fr.get("input") or {}
                call = {"id": fr.get("id"), "name": fr.get("name"), "input": inp,
                        "result": None, "is_error": None}
                self.tool_calls.append(call)
                if call["id"]:
                    self._by_id[call["id"]] = call
                blob = json.dumps(inp, default=str)
                self.audit.record("tool_use", self.key_id, session_id, tool_name=fr.get("name"),
                                  detail=f"sha256={hashlib.sha256(blob.encode()).hexdigest()[:16]} {blob[:180]}")
            elif t == "tool.result":
                call = self._by_id.get(fr.get("tool_use_id") or "")
                if call is not None:
                    call["result"] = fr.get("content")
                    call["is_error"] = fr.get("is_error")
                self.audit.record("tool_result", self.key_id, session_id,
                                  detail=f"is_error={fr.get('is_error')}")
            elif t == "error" and self.agent_error is None:
                self.agent_error = fr.get("message")
        return on_frame


async def _await_disconnect(request: Request) -> None:
    """Resolve once the HTTP client hangs up.

    uvicorn does not cancel the endpoint task on disconnect, so without this a
    turn nobody is listening to would keep a seat pinned for up to
    http_turn_timeout_s.
    """
    while not await request.is_disconnected():
        await asyncio.sleep(_DISCONNECT_POLL_S)


# ---------------- teardown ----------------

async def _teardown(settings, seat_pool, sm, audit, key_id, session, driver) -> None:
    """Always run, including on client disconnect: kill the process, free the
    seat, drop the session, and remove the ephemeral work dir."""
    session.status = SessionStatus.CLOSED
    sm.unregister_driver(session.session_id)
    sm.remove(session.session_id)
    try:
        await driver.stop()
    except Exception:  # noqa: BLE001
        logger.exception("one-shot driver stop failed | session=%s", session.session_id)
    seat_pool.release(session.seat_id)
    audit.record("session_close", key_id, session.session_id, detail="one-shot")

    # SessionManager.cleanup_disk only sweeps sessions still in the registry and
    # we just removed ours, so the ephemeral dir is ours to delete.
    wd = Path(session.work_dir)
    base = settings.sessions_dir
    try:
        inside = wd.is_relative_to(base)
    except (AttributeError, ValueError):
        inside = str(wd).startswith(str(base))
    if inside:
        target = wd.parent if wd.name == "work" else wd
        try:
            shutil.rmtree(target, ignore_errors=True)
        except OSError as e:
            logger.warning("one-shot dir cleanup failed for %s: %s", session.session_id, e)


# ---------------- endpoint ----------------

@router.post("/v1/agent/completions")
async def completions(request: Request, body: CompletionRequest):
    try:
        return await _completions(request, body)
    except HttpError as e:
        return e.response()


async def _completions(request: Request, body: CompletionRequest):
    st = request.app.state
    settings = st.settings
    if not settings.http_mode_enabled:
        raise HttpError("http_mode_disabled", "one-shot HTTP mode is disabled on this server")

    principal = bearer_principal(request)
    scope = principal.scope
    keystore, sm, seat_pool, audit = st.keystore, st.session_manager, st.seat_pool, st.audit

    if scope.max_turns_per_day is not None and keystore.turns_today(principal.key_id) >= scope.max_turns_per_day:
        raise HttpError("quota_exceeded", f"daily turn limit {scope.max_turns_per_day} reached")

    collector = FrameCollector(audit, principal.key_id)
    # nothing streams to the client, so partial-message deltas are pure overhead
    driver_settings = settings.model_copy(update={"include_partial_messages": False})

    try:
        res = await provision_session(
            settings=settings,
            scope=scope,
            key_id=principal.key_id,
            seat_pool=seat_pool,
            session_manager=sm,
            shared_mcp_path=getattr(st, "shared_mcp_path", None),
            on_frame_factory=collector,
            req=body,
            driver_settings=driver_settings,
        )
    except ProvisionError as e:
        raise HttpError(e.code, e.message, retryable=e.retryable) from e

    session, driver = res.session, res.driver
    session.attached = True
    audit.record("session_open", principal.key_id, session.session_id,
                 detail=f"one-shot mode={session.mode} model={session.model} skills={res.skills_composed}")

    timeout = min(body.timeout_s or settings.http_turn_timeout_s, settings.http_turn_timeout_s)
    try:
        session.status = SessionStatus.RUNNING
        session.turn_count += 1
        session.touch()
        keystore.record_turn(principal.key_id)
        # race the turn against the client hanging up — whoever finishes first wins
        turn = asyncio.ensure_future(driver.send_turn(body.prompt, timeout=timeout))
        gone = asyncio.ensure_future(_await_disconnect(request))
        try:
            await asyncio.wait({turn, gone}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            gone.cancel()
            if not turn.done():
                turn.cancel()
        if not turn.done() or turn.cancelled():
            logger.info("client disconnected mid-turn | session=%s — aborting", session.session_id)
            raise HttpError("client_disconnected", "client hung up before the turn finished")
        try:
            result = turn.result()
        except RuntimeError as e:
            raise HttpError("turn_failed", str(e)) from e
        check_seat_health(seat_pool, session, driver)
        if result is None:
            raise HttpError("turn_timeout", f"turn exceeded {timeout:g}s", retryable=True)
        # recorded before teardown so the audit reads open -> tools -> complete -> close
        cost = result.get("total_cost_usd") or result.get("cost_usd")
        audit.record("turn_complete", principal.key_id, session.session_id, cost_usd=cost,
                     detail=f"one-shot stop={result.get('stop_reason')} turns={result.get('num_turns')}")
    finally:
        # Shielded so a client that hangs up mid-request can't leave a `claude`
        # process alive or a seat slot leaked: if this task is already being
        # cancelled the shield re-raises immediately but the teardown itself
        # runs to completion in the background.
        await asyncio.shield(
            _teardown(settings, seat_pool, sm, audit, principal.key_id, session, driver)
        )

    # the CLI's own `result` field is the authoritative final text; the collected
    # assistant blocks are the fallback (and the full per-block breakdown)
    final = result.get("result")
    text = final if isinstance(final, str) and final else (collector.messages[-1] if collector.messages else "")
    return CompletionResponse(
        session_id=session.session_id,
        text=text,
        messages=collector.messages,
        tool_calls=[ToolCall(**c) for c in collector.tool_calls],
        stop_reason=result.get("stop_reason"),
        cost_usd=cost,
        duration_ms=result.get("duration_ms"),
        num_turns=result.get("num_turns"),
        usage=result.get("usage"),
        is_error=bool(result.get("is_error", False)) or collector.agent_error is not None,
        model=driver.model,
        mode=session.mode,
        seat=session.seat_id,
        skills=res.skills_composed,
    )
