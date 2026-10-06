"""WebSocket gateway: the /v1/agent endpoint and per-connection handler.

Bridges a client WebSocket to a persistent Claude Code process (client
`user.message` -> stdin turn; each stdout event -> a WS frame). Drivers are owned
by the SessionManager so a global sweeper can idle-suspend them; this connection
holds the on_frame sink for its attached sessions and transparently re-warms a
suspended session on the next turn.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ccuc import protocol
from ccuc.auth import Principal
from ccuc.driver import StreamingDriver
from ccuc.models import Session, SessionStatus
from ccuc.provision import (  # re-exported: shared with the one-shot HTTP path
    ProvisionError,
    acquire_seat,
    check_seat_health,
    provision_session,
    resolve_tools,
    resolve_work_dir,
)

logger = logging.getLogger(__name__)
router = APIRouter()

__all__ = ["router", "Connection", "resolve_tools", "resolve_work_dir"]


@router.websocket("/v1/agent")
async def agent_ws(ws: WebSocket):
    await ws.accept()
    conn = Connection(
        ws, ws.app.state.settings, ws.app.state.seat_pool,
        ws.app.state.keystore, ws.app.state.session_manager, ws.app.state.audit,
    )
    await conn.run()


class Connection:
    def __init__(self, ws: WebSocket, settings, seat_pool, keystore, session_manager, audit):
        self.ws = ws
        self.settings = settings
        self.seat_pool = seat_pool
        self.keystore = keystore
        self.sm = session_manager
        self.audit = audit
        self.principal: Principal | None = None
        self.sessions: dict[str, Session] = {}   # sessions attached to THIS connection
        self._busy: set[str] = set()
        self._turn_tasks: dict[str, asyncio.Task] = {}
        self._send_lock = asyncio.Lock()

    async def send(self, frame: dict) -> None:
        async with self._send_lock:
            try:
                await self.ws.send_text(json.dumps(frame))
            except Exception as e:  # noqa: BLE001
                logger.debug("ws send failed: %s", e)

    async def run(self) -> None:
        try:
            if not await self._authenticate():
                return
            active, total = self.seat_pool.capacity()
            await self.send(protocol.hello(limits={
                "seat_max_concurrency": self.settings.seat_max_concurrency,
                "seats_total_slots": total,
            }))
            while True:
                raw = await self.ws.receive_text()
                await self._dispatch(raw)
        except WebSocketDisconnect:
            logger.info("ws disconnect | key=%s sessions=%d", getattr(self.principal, "key_id", "?"), len(self.sessions))
        except RuntimeError as e:
            logger.info("ws closed | %s", e)
        except Exception as e:  # noqa: BLE001
            logger.warning("connection error: %s", e)
        finally:
            await self._cleanup()

    async def _authenticate(self) -> bool:
        token = None
        auth = self.ws.headers.get("authorization")
        if auth and auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        if not token:
            try:
                raw = await asyncio.wait_for(self.ws.receive_text(), timeout=self.settings.auth_timeout_s)
                frame = protocol.parse_inbound(raw)
                if getattr(frame, "type", None) == "auth":
                    token = frame.token
            except Exception:
                pass
        principal = self.keystore.validate(token)
        if not principal:
            await self.send(protocol.error("unauthenticated", "invalid or missing API key"))
            await self.ws.close(code=4401)
            return False
        self.principal = principal
        logger.info("authenticated | key=%s", principal.key_id)
        return True

    async def _dispatch(self, raw: str) -> None:
        try:
            frame = protocol.parse_inbound(raw)
        except Exception as e:  # noqa: BLE001
            await self.send(protocol.error("bad_frame", f"invalid frame: {e}"))
            return
        t = frame.type
        if t == "ping":
            await self.send(protocol.pong())
        elif t == "session.open":
            await self._open(frame)
        elif t == "user.message":
            await self._user_message(frame)
        elif t == "interrupt":
            await self._interrupt(frame)
        elif t == "session.close":
            await self._close(frame)
        elif t == "session.resume":
            await self._resume(frame)
        elif t == "auth":
            pass
        else:
            await self.send(protocol.error("unsupported_type", f"unknown type {t}"))

    async def _acquire_seat(self, prefer_seat_id=None):
        """Reserve a warm slot, waiting (bounded) if the pool is saturated."""
        return await acquire_seat(self.seat_pool, self.settings, prefer_seat_id)

    def _on_frame_for(self, session_id: str):
        """Build the driver's frame sink: forward to the WS + audit tool activity."""
        async def on_frame(fr: dict) -> None:
            t = fr.get("type")
            if t == "tool.use":
                inp = json.dumps(fr.get("input", {}), default=str)
                self.audit.record("tool_use", self.principal.key_id, session_id, tool_name=fr.get("name"),
                                  detail=f"sha256={hashlib.sha256(inp.encode()).hexdigest()[:16]} {inp[:180]}")
            elif t == "tool.result":
                self.audit.record("tool_result", self.principal.key_id, session_id,
                                  detail=f"is_error={fr.get('is_error')}")
            await self.send(fr)
        return on_frame

    async def _open(self, f) -> None:
        # session_id is minted inside provision_session, so a failure before the
        # process exists has no session to name — matching the previous behaviour
        # for the pre-spawn error codes.
        try:
            res = await provision_session(
                settings=self.settings,
                scope=self.principal.scope,
                key_id=self.principal.key_id,
                seat_pool=self.seat_pool,
                session_manager=self.sm,
                shared_mcp_path=getattr(self.ws.app.state, "shared_mcp_path", None),
                on_frame_factory=self._on_frame_for,
                req=f,
            )
        except ProvisionError as e:
            await self.send(protocol.error(e.code, e.message, retryable=e.retryable, req_id=f.req_id))
            return

        session, driver = res.session, res.driver
        session_id = session.session_id
        for msg in res.skill_warnings:
            await self.send(protocol.error("skill_pack_warning", msg, session=session_id, req_id=f.req_id))

        session.attached = True
        self.sessions[session_id] = session
        self.audit.record("session_open", self.principal.key_id, session_id,
                          detail=f"mode={session.mode} model={session.model} skills={res.skills_composed}")

        await self.send(protocol.session_ready(
            session_id, session.seat_id, session.work_dir, session.model,
            tools=driver.tools, mcp_servers=driver.mcp_servers, req_id=f.req_id,
            skills=res.skills_composed, mode=session.mode,
        ))

    async def _resume(self, f) -> None:
        sess = self.sm.get(f.session_id)
        if not sess:
            await self.send(protocol.error("no_such_session", "unknown session", session=f.session_id, req_id=f.req_id))
            return
        if sess.key_id != self.principal.key_id:
            await self.send(protocol.error("forbidden", "session belongs to another key", session=f.session_id, req_id=f.req_id))
            return
        if sess.attached or self.sm.get_driver(f.session_id) is not None:
            await self.send(protocol.error("session_busy", "session is attached to another connection", session=f.session_id, req_id=f.req_id))
            return
        seat = await self._acquire_seat(prefer_seat_id=sess.seat_id)
        if seat is None:
            await self.send(protocol.error("seat_pool_saturated", "no free slot on the session's seat; retry", retryable=True, session=f.session_id, req_id=f.req_id))
            return

        driver = StreamingDriver(self.settings, sess, seat)
        try:
            await driver.start(self._on_frame_for(f.session_id), resume=True)
        except Exception as e:  # noqa: BLE001
            logger.exception("failed to resume session %s", f.session_id)
            self.seat_pool.release(seat.seat_id)
            await self.send(protocol.error("resume_failed", str(e), session=f.session_id, req_id=f.req_id))
            return
        sess.status = SessionStatus.IDLE
        sess.attached = True
        sess.touch()
        self.sm.register_driver(f.session_id, driver)
        self.sessions[f.session_id] = sess
        logger.info("resumed session=%s seat=%s", f.session_id, seat.seat_id)
        await self.send(protocol.session_ready(
            f.session_id, seat.seat_id, sess.work_dir, sess.model,
            tools=driver.tools, mcp_servers=driver.mcp_servers, req_id=f.req_id, skills=[], mode=sess.mode,
        ))

    async def _user_message(self, f) -> None:
        session = self.sessions.get(f.session_id)
        if session is None:
            await self.send(protocol.error("no_such_session", "unknown session (open or resume it first)", session=f.session_id, req_id=f.req_id))
            return
        if f.session_id in self._busy:
            await self.send(protocol.error("turn_in_progress", "a turn is already running for this session", session=f.session_id, req_id=f.req_id))
            return
        self._busy.add(f.session_id)
        self._turn_tasks[f.session_id] = asyncio.create_task(self._run_turn(session, f))

    async def _rewarm(self, session: Session, req_id):
        """Re-spawn a suspended session's process on its pinned seat (transparent resume)."""
        seat = await self._acquire_seat(prefer_seat_id=session.seat_id)
        if seat is None:
            await self.send(protocol.error("seat_pool_saturated", "no free slot on session's seat; retry", retryable=True, session=session.session_id, req_id=req_id))
            return None
        driver = StreamingDriver(self.settings, session, seat)
        try:
            await driver.start(self._on_frame_for(session.session_id), resume=True)
        except Exception as e:  # noqa: BLE001
            self.seat_pool.release(seat.seat_id)
            await self.send(protocol.error("rewarm_failed", str(e), session=session.session_id, req_id=req_id))
            return None
        self.sm.register_driver(session.session_id, driver)
        logger.info("rewarmed session=%s seat=%s", session.session_id, seat.seat_id)
        return driver

    async def _run_turn(self, session: Session, f) -> None:
        scope = self.principal.scope
        if scope.max_turns_per_day is not None and self.keystore.turns_today(self.principal.key_id) >= scope.max_turns_per_day:
            self._busy.discard(f.session_id)
            self._turn_tasks.pop(f.session_id, None)
            await self.send(protocol.error("quota_exceeded", f"daily turn limit {scope.max_turns_per_day} reached", session=f.session_id, req_id=f.req_id))
            return

        # ensure a warm process (transparent re-warm if idle-suspended)
        async with session._lock:
            driver = self.sm.get_driver(session.session_id)
            if driver is None or not driver.is_alive:
                driver = await self._rewarm(session, f.req_id)
                if driver is None:
                    self._busy.discard(f.session_id)
                    self._turn_tasks.pop(f.session_id, None)
                    return
            session.status = SessionStatus.RUNNING
            session.turn_count += 1
            session.touch()

        self.keystore.record_turn(self.principal.key_id)
        result = None
        errored = False
        try:
            result = await driver.send_turn(f.text)
        except RuntimeError as e:
            errored = True
            await self.send(protocol.error("turn_failed", str(e), session=f.session_id, req_id=f.req_id))
        finally:
            self._busy.discard(f.session_id)
            self._turn_tasks.pop(f.session_id, None)
            session.status = SessionStatus.IDLE
            session.touch()

        self._check_seat_health(session, driver)
        if errored:
            return
        if result is None:
            await self.send(protocol.error("turn_timeout", "turn timed out", session=f.session_id, retryable=True, req_id=f.req_id))
            return
        await self.send(protocol.turn_complete(
            f.session_id, req_id=f.req_id,
            stop_reason=result.get("stop_reason"),
            cost_usd=result.get("total_cost_usd") or result.get("cost_usd"),
            duration_ms=result.get("duration_ms"),
            num_turns=result.get("num_turns"),
            usage=result.get("usage"),
            is_error=result.get("is_error", False),
        ))
        self.audit.record("turn_complete", self.principal.key_id, f.session_id,
                          cost_usd=(result.get("total_cost_usd") or result.get("cost_usd")),
                          detail=f"stop={result.get('stop_reason')} turns={result.get('num_turns')}")

    def _check_seat_health(self, session: Session, driver) -> None:
        check_seat_health(self.seat_pool, session, driver)

    async def _interrupt(self, f) -> None:
        driver = self.sm.get_driver(f.session_id)
        if driver is not None:
            asyncio.create_task(driver.interrupt())

    async def _close(self, f) -> None:
        session = self.sessions.pop(f.session_id, None) or self.sm.get(f.session_id)
        if session is None or session.key_id != self.principal.key_id:
            return
        task = self._turn_tasks.pop(f.session_id, None)
        if task:
            task.cancel()
        self._busy.discard(f.session_id)
        driver = self.sm.get_driver(f.session_id)
        session.attached = False
        session.status = SessionStatus.CLOSED
        if driver is not None:  # a warm session holds a seat slot
            self.seat_pool.release(session.seat_id)
        self.sm.remove(f.session_id)
        self.audit.record("session_close", self.principal.key_id, f.session_id)
        await self.send(protocol.session_closed(f.session_id))
        if driver is not None:
            await driver.stop()

    async def _cleanup(self) -> None:
        for task in list(self._turn_tasks.values()):
            task.cancel()
        self._turn_tasks.clear()
        self._busy.clear()
        # On disconnect, SUSPEND (don't destroy) so the client can reconnect/resume.
        for sid, session in list(self.sessions.items()):
            driver = self.sm.get_driver(sid)
            session.attached = False
            if session.status not in (SessionStatus.CLOSED, SessionStatus.FAILED):
                session.status = SessionStatus.SUSPENDED
            if driver is not None:
                try:
                    await driver.stop()
                except Exception:  # noqa: BLE001
                    pass
                self.seat_pool.release(session.seat_id)
                self.sm.unregister_driver(sid)
        self.sessions.clear()
