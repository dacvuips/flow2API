from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response

import asyncio
import logging

import httpx

from flow2api.config import HTTP_HANDLER_TIMEOUT_S, INPUTS_DIR, VIDEOS_DIR
from flow2api.services.dashboard_events import events
from flow2api.services.extension_pool import get_extension_pool
from flow2api.services.flow_client import get_flow_client
from flow2api.services.flow_sdk import get_media_http_status, parse_get_media_image
from flow2api.services.health_cache import get_health_payload

logger = logging.getLogger(__name__)

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


@router.get("/inputs/{request_id}/{filename}")
async def serve_input_image(request_id: str, filename: str):
    if ".." in request_id or ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(400, "invalid path")
    path = INPUTS_DIR / request_id / filename
    if not path.is_file():
        raise HTTPException(404, "not_found")
    ext = path.suffix.lower()
    mime = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(ext, "application/octet-stream")
    return FileResponse(path, media_type=mime)


@router.get("/media/{media_id:path}")
async def serve_media(media_id: str):
    path = VIDEOS_DIR / f"{media_id}.mp4"
    if path.is_file():
        return FileResponse(path, media_type="video/mp4")

    client = get_flow_client()
    if not client.connected:
        raise HTTPException(503, "extension_not_connected")
    try:
        resp = await asyncio.wait_for(client.get_media(media_id), timeout=60)
    except asyncio.TimeoutError:
        raise HTTPException(504, "get_media_timeout") from None
    except Exception as exc:
        logger.warning("serve_media get_media failed %s: %s", media_id[:12], exc)
        raise HTTPException(502, "get_media_failed") from exc

    status = get_media_http_status(resp if isinstance(resp, dict) else {})
    if status == 404:
        raise HTTPException(404, "not_found")
    if status >= 400:
        raise HTTPException(status, "get_media_error")

    url, raw, mime = parse_get_media_image(resp if isinstance(resp, dict) else {})
    if raw:
        return Response(content=raw, media_type=mime)
    if url:
        try:
            cap = max(10.0, min(60.0, float(HTTP_HANDLER_TIMEOUT_S or 25) * 2))
            async with httpx.AsyncClient(timeout=cap, follow_redirects=True) as http:
                fetched = await http.get(url)
            if fetched.status_code == 200 and fetched.content:
                media_type = fetched.headers.get("content-type") or mime
                return Response(content=fetched.content, media_type=media_type)
        except Exception as exc:
            logger.warning("serve_media fetch url failed %s: %s", media_id[:12], exc)
    raise HTTPException(404, "not_found")


@router.get("/api/events")
async def sse_events():
    from fastapi.responses import StreamingResponse

    return StreamingResponse(events.stream(), media_type="text/event-stream")
