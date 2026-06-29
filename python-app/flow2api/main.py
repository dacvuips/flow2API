"""Flow2API — entry point (HTTP + WS + worker)."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from flow2api.config import (
    ACTIVITY_LIST_LIMIT,
    ACTIVITY_META_LIMIT,
    FRONTEND_DIR,
    HTTP_HOST,
    HTTP_PORT,
    PURGE_INTERVAL_S,
    RELOAD,
    WORKER_NUDGE_INTERVAL_S,
    learn_public_base_url_from_headers,
)
from flow2api.db import init_db
from flow2api.routes.activity import router as activity_router
from flow2api.routes.admin import router as admin_router
from flow2api.routes.auth import router as auth_router
from flow2api.routes.requests import router as requests_router
from flow2api.routes.settings import router as settings_router
from flow2api.routes.system import router as system_router
from flow2api.routes.worker import router as worker_router
from flow2api.services.ws_server import run_ws_server
from flow2api.services.task_counters import bootstrap_from_requests_if_empty
from flow2api.services.task_retention import purge_storage
from flow2api.worker.processor import get_worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


class _SuppressAsyncioClientDisconnect(logging.Filter):
    """Windows: browser closes video range requests → harmless asyncio noise."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "asyncio":
            return True
        if "_call_connection_lost" in (record.getMessage() or ""):
            return False
        exc = record.exc_info[1] if record.exc_info else None
        if isinstance(exc, ConnectionResetError) and getattr(exc, "winerror", None) == 10054:
            return False
        return True


logging.getLogger("asyncio").addFilter(_SuppressAsyncioClientDisconnect())


class _SuppressNoisyAccessLog(logging.Filter):
    """Hide dashboard polling from CMD (health, auth scan, task list, …)."""

    _QUIET_PATHS = (
        "/api/health",
        "/api/auth/scan",
        "/api/auth/me",
        "/api/auth/accounts",
        "/api/auth/key",
        "/api/events",
        "/api/worker/settings",
        "/api/settings",
        "/api/activity",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage() or ""
        for path in self._QUIET_PATHS:
            if path in msg:
                return False
        if '"GET /api/requests/' in msg or '"GET /api/activity/' in msg:
            return False
        if '"GET /api/requests HTTP/1.1"' in msg:
            return False
        return True


logging.getLogger("uvicorn.access").addFilter(_SuppressNoisyAccessLog())

logger = logging.getLogger(__name__)


async def _retention_loop() -> None:
    interval = max(60, int(PURGE_INTERVAL_S or 300))
    while True:
        await asyncio.sleep(interval)
        try:
            stripped, deleted, expired = await asyncio.to_thread(
                purge_storage, ACTIVITY_LIST_LIMIT, ACTIVITY_META_LIMIT
            )
            if stripped:
                logger.info(
                    "background retention stripped media from %s task(s)", stripped
                )
            if deleted:
                logger.info("background retention purge removed %s task(s)", deleted)
            if expired:
                logger.info("background media purge removed %s output folder(s)", expired)
        except Exception as exc:
            logger.warning("background retention purge failed: %s", exc)


async def _worker_watchdog() -> None:
    worker = get_worker()
    interval = max(30, int(WORKER_NUDGE_INTERVAL_S or 120))
    while True:
        await asyncio.sleep(interval)
        try:
            result = await worker.nudge()
            if result.get("actions"):
                logger.info(
                    "worker nudge (%ss): %s queued=%s running=%s",
                    interval,
                    result.get("actions"),
                    result.get("queued"),
                    result.get("running_slots"),
                )
        except Exception as exc:
            logger.warning("worker nudge failed: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    try:
        bootstrap_from_requests_if_empty()
    except Exception as exc:
        logger.warning("task counter bootstrap failed: %s", exc)
    try:
        stripped, purged, expired = await asyncio.to_thread(
            purge_storage, ACTIVITY_LIST_LIMIT, ACTIVITY_META_LIMIT
        )
        if stripped:
            logger.info("startup retention stripped media from %s task(s)", stripped)
        if purged:
            logger.info("startup retention purge removed %s old task(s)", purged)
        if expired:
            logger.info("startup media purge removed %s expired output folder(s)", expired)
    except Exception as exc:
        logger.warning("startup retention purge failed: %s", exc)
    ws_task = asyncio.create_task(run_ws_server())
    worker = get_worker()
    worker_task = asyncio.create_task(worker.start())
    watchdog_task = asyncio.create_task(_worker_watchdog())
    retention_task = asyncio.create_task(_retention_loop())
    from flow2api.services.system_ops import telegram_poll_loop

    telegram_task = asyncio.create_task(telegram_poll_loop())
    logger.info(
        "flow2api agent started (http:%s + ws:1609 + worker + nudge %ss)",
        HTTP_PORT,
        WORKER_NUDGE_INTERVAL_S,
    )
    yield
    await worker.stop()
    ws_task.cancel()
    worker_task.cancel()
    watchdog_task.cancel()
    retention_task.cancel()
    telegram_task.cancel()
    try:
        await asyncio.gather(
            ws_task, worker_task, watchdog_task, retention_task, telegram_task, return_exceptions=True
        )
    except Exception:
        pass


def create_app() -> FastAPI:
    app = FastAPI(title="Flow2API", lifespan=lifespan)

    @app.middleware("http")
    async def _learn_public_base_url(request, call_next):
        learn_public_base_url_from_headers(request.headers)
        return await call_next(request)

    app.include_router(requests_router)
    app.include_router(auth_router)
    app.include_router(activity_router)
    app.include_router(admin_router)
    app.include_router(system_router)
    app.include_router(settings_router)
    app.include_router(worker_router)

    dashboard = FRONTEND_DIR / "dashboard.html"
    if dashboard.is_file():

        @app.get("/")
        async def dashboard_page():
            return FileResponse(dashboard)

        if FRONTEND_DIR.is_dir():
            app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    return app


app = create_app()


def main() -> None:
    pkg_dir = Path(__file__).resolve().parent
    kwargs: dict = {
        "host": HTTP_HOST,
        "port": HTTP_PORT,
        "log_level": "info",
    }
    if RELOAD:
        reload_dirs = [str(pkg_dir)]
        if FRONTEND_DIR.is_dir():
            reload_dirs.append(str(FRONTEND_DIR))
        kwargs["reload"] = True
        kwargs["reload_dirs"] = reload_dirs
        logger.info("Auto-reload ON — sửa code sẽ tự restart, không cần tắt CMD")
    else:
        kwargs["reload"] = False
    uvicorn.run("flow2api.main:app", **kwargs)


if __name__ == "__main__":
    main()
