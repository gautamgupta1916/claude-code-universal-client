from __future__ import annotations

import asyncio
import logging
from collections import Counter
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from ccuc.audit import AuditLog
from ccuc.bundles import list_skill_packs, render_shared_mcp
from ccuc.config import Settings
from ccuc.gateway import router as ws_router
from ccuc.keys import KeyStore
from ccuc.logging_config import UVICORN_LOG_CONFIG, setup_logging
from ccuc.rest import router as rest_router
from ccuc.seats import SeatPool
from ccuc.sessions import SessionManager

setup_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    settings.sessions_dir.mkdir(parents=True, exist_ok=True)
    seat_pool = SeatPool(settings)
    keystore = KeyStore(settings)
    session_manager = SessionManager(settings)
    audit = AuditLog(settings)
    shared_mcp_path = (
        render_shared_mcp(settings.bundles_path, settings.rendered_mcp_path)
        if settings.shared_mcp_enabled else None
    )

    app.state.settings = settings
    app.state.seat_pool = seat_pool
    app.state.keystore = keystore
    app.state.session_manager = session_manager
    app.state.audit = audit
    app.state.shared_mcp_path = shared_mcp_path

    logger.info(
        "CCUC starting on %s:%s | seats=%d | model=%s | data=%s",
        settings.host, settings.port, len(seat_pool.seats), settings.default_model, settings.data_path,
    )
    for s in seat_pool.seats:
        logger.info("seat %s | config_dir=%s", s.seat_id, s.config_dir or "(ambient login)")
    logger.info(
        "bundles=%s | skill packs=%s | shared MCP=%s | default packs=%s",
        settings.bundles_path, list_skill_packs(settings.bundles_path),
        shared_mcp_path or "none", settings.default_skill_packs,
    )
    logger.info(
        "front doors: WS /v1/agent | one-shot HTTP POST /v1/agent/completions=%s (timeout %.0fs)",
        "on" if settings.http_mode_enabled else "off", settings.http_turn_timeout_s,
    )
    if settings.dev_api_key:
        logger.warning("DEV AUTH ENABLED — connect with Bearer '%s' (local dev only)", settings.dev_api_key)

    sweeper = asyncio.create_task(_periodic_sweep(settings, seat_pool, session_manager))
    yield

    logger.info("CCUC shutting down")
    sweeper.cancel()
    for sid, driver in list(session_manager._drivers.items()):
        try:
            await driver.stop()
        except Exception:  # noqa: BLE001
            pass


async def _periodic_sweep(settings, seat_pool, session_manager) -> None:
    while True:
        await asyncio.sleep(settings.sweep_interval_s)
        try:
            n = await session_manager.sweep_idle(seat_pool)
            if n:
                logger.info("idle sweep suspended %d session(s)", n)
            reaped = session_manager.sweep_stale()
            if reaped:
                logger.info("stale sweep reaped %d session(s)", reaped)
            await session_manager.cleanup_disk(settings.disk_cleanup_hours)
        except Exception:  # noqa: BLE001
            logger.exception("periodic sweep error")


app = FastAPI(title="claude-code-universal-client", lifespan=lifespan)
app.include_router(ws_router)
app.include_router(rest_router)


@app.get("/health")
async def health(request: Request):
    sp: SeatPool = request.app.state.seat_pool
    sm = request.app.state.session_manager
    active_slots, total_slots = sp.capacity()
    return {
        "status": "ok",
        "seat_slots": {"active": active_slots, "total": total_slots},
        "sessions": len(sm.list()),
        "warm_processes": len(sm._drivers),
        "seats": [
            {
                "seat_id": s.seat_id,
                "config_dir": s.config_dir,
                "status": s.status,
                "healthy": s.healthy(),
                "active": s.active,
                "max_concurrency": s.max_concurrency,
            }
            for s in sp.seats
        ],
    }


@app.get("/sessions")
async def sessions(request: Request):
    sm: SessionManager = request.app.state.session_manager
    return [
        {
            "session_id": s.session_id,
            "key_id": s.key_id,
            "seat_id": s.seat_id,
            "status": s.status.value,
            "attached": s.attached,
            "model": s.model,
            "turn_count": s.turn_count,
            "work_dir": s.work_dir,
            "last_activity_at": s.last_activity_at.isoformat(),
        }
        for s in sm.list()
    ]


@app.get("/stats")
async def stats(request: Request):
    sm = request.app.state.session_manager
    sp: SeatPool = request.app.state.seat_pool
    audit = request.app.state.audit
    by_status = Counter(s.status.value for s in sm.list())
    active, total = sp.capacity()
    return {
        "sessions": {"total": len(sm.list()), "warm_processes": len(sm._drivers), "by_status": dict(by_status)},
        "seats": {
            "active_slots": active,
            "total_slots": total,
            "detail": [
                {"seat_id": s.seat_id, "status": s.status, "healthy": s.healthy(),
                 "active": s.active, "max_concurrency": s.max_concurrency}
                for s in sp.seats
            ],
        },
        "usage": {
            "total_cost_usd": round(audit.total_cost(), 4),
            "event_counts": audit.event_counts(),
            "tool_use_counts": audit.tool_use_counts(),
        },
    }


@app.get("/audit")
async def audit_tail(request: Request, limit: int = 50, session: str | None = None):
    return request.app.state.audit.recent(limit=min(limit, 500), session_id=session)


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics(request: Request):
    sm = request.app.state.session_manager
    sp: SeatPool = request.app.state.seat_pool
    audit = request.app.state.audit
    by_status = Counter(s.status.value for s in sm.list())
    active, total = sp.capacity()
    lines = ["# HELP ccuc_sessions Sessions by status", "# TYPE ccuc_sessions gauge"]
    for st in ("starting", "running", "idle", "suspended", "interrupted", "closed", "failed"):
        lines.append(f'ccuc_sessions{{status="{st}"}} {by_status.get(st, 0)}')
    lines += [
        "# TYPE ccuc_warm_processes gauge", f"ccuc_warm_processes {len(sm._drivers)}",
        "# TYPE ccuc_seat_active_slots gauge", f"ccuc_seat_active_slots {active}",
        "# TYPE ccuc_seat_total_slots gauge", f"ccuc_seat_total_slots {total}",
        "# TYPE ccuc_cost_usd_total counter", f"ccuc_cost_usd_total {audit.total_cost()}",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    settings = Settings()
    uvicorn.run(
        "ccuc.app:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_config=UVICORN_LOG_CONFIG,
    )


if __name__ == "__main__":
    main()
