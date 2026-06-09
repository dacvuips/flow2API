from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse

from flow2api.config import VIDEOS_DIR
from flow2api.services import activity
from flow2api.services.dashboard_events import events
from flow2api.services.extension_pool import get_extension_pool
from flow2api.services.worker_settings import get_worker_settings
from flow2api.worker.processor import get_worker

router = APIRouter(tags=["system"])


@router.get("/api/health")
async def health():
    pool = get_extension_pool()
    stats = activity.summary_stats()
    worker_cfg = get_worker_settings()
    worker = get_worker()
    profiles = pool.list_public()
    first_ready = pool.first_ready()
    return {
        "ok": True,
        "worker": {
            **worker_cfg.to_dict(),
            "running_slots": worker.running_count(),
            "queued": activity.count_queued(),
            "scheduler_alive": worker.scheduler_alive(),
            "profiles_online": pool.online_count(),
            "profiles_ready": pool.ready_count(),
        },
        "profiles": profiles,
        "extension": {
            "connected": pool.any_connected(),
            "flow_key_present": bool(first_ready and first_ready.flow_key),
            "token_age_s": first_ready.to_public_dict().get("token_age_s") if first_ready else None,
            "profiles_online": pool.online_count(),
            "profiles_ready": pool.ready_count(),
        },
        "extension_connected": pool.any_connected(),
        "ws_stats": {
            "connected": pool.any_connected(),
            "profiles_online": pool.online_count(),
            "profiles_ready": pool.ready_count(),
            "accounts": profiles,
        },
        "queue": stats,
        "debug_version": 3,
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
