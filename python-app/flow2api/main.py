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

from flow2api.config import FRONTEND_DIR, HTTP_HOST, HTTP_PORT, RELOAD
from flow2api.db import init_db
from flow2api.routes.activity import router as activity_router
from flow2api.routes.admin import router as admin_router
from flow2api.routes.auth import router as auth_router
from flow2api.routes.requests import router as requests_router
from flow2api.routes.system import router as system_router
from flow2api.routes.worker import router as worker_router
from flow2api.services.ws_server import run_ws_server
from flow2api.worker.processor import get_worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    ws_task = asyncio.create_task(run_ws_server())
    worker = get_worker()
    worker_task = asyncio.create_task(worker.start())
    logger.info("flow2api agent started (http:%s + ws:1609 + worker)", HTTP_PORT)
    yield
    await worker.stop()
    ws_task.cancel()
    worker_task.cancel()
    try:
        await asyncio.gather(ws_task, worker_task, return_exceptions=True)
    except Exception:
        pass


def create_app() -> FastAPI:
    app = FastAPI(title="Flow2API", lifespan=lifespan)
    app.include_router(requests_router)
    app.include_router(auth_router)
    app.include_router(activity_router)
    app.include_router(admin_router)
    app.include_router(system_router)
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
