from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse

import asyncio

from flow2api.config import HTTP_HANDLER_TIMEOUT_S, VIDEOS_DIR
from flow2api.services.dashboard_events import events
from flow2api.services.extension_pool import get_extension_pool
from flow2api.services.health_cache import get_health_payload

router = APIRouter(tags=["system"])


@router.get("/api/health")
async def health():
    timeout = max(3.0, float(HTTP_HANDLER_TIMEOUT_S or 25))
    try:
        return await asyncio.wait_for(get_health_payload(), timeout=timeout)
    except asyncio.TimeoutError:
        pool = get_extension_pool()
        return {
            "ok": False,
            "degraded": True,
            "error": "health_timeout",
            "extension_connected": pool.any_connected(),
            "profiles_online": pool.online_count(),
            "profiles_ready": pool.ready_count(),
            "debug_version": 4,
        }


@router.post("/api/ext/callback")
async def ext_callback(
    request: Request,
    x_callback_secret: str | None = Header(default=None, alias="X-Callback-Secret"),
):
    pool = get_extension_pool()
    if not x_callback_secret or x_callback_secret != pool.callback_secret:
        raise HTTPException(403, "invalid callback secret")
    payload = await request.json()
    pool.resolve_callback(payload)
    return {"ok": True}


@router.get("/media/{media_id}")
async def serve_local_video(media_id: str):
    path = VIDEOS_DIR / f"{media_id}.mp4"
    if not path.is_file():
        raise HTTPException(404, "not_found")
    return FileResponse(path, media_type="video/mp4")


@router.get("/api/events")
async def sse_events():
    from fastapi.responses import StreamingResponse

    return StreamingResponse(events.stream(), media_type="text/event-stream")
