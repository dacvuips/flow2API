from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

import asyncio
import logging

import httpx

from flow2api.config import HTTP_HANDLER_TIMEOUT_S, INPUTS_DIR, VIDEOS_DIR
from flow2api.services.auth_keys import get_api_key_by_token
from flow2api.services.stored_media import (
    materialize_request_video,
    resolve_stored_image_path,
    resolve_stored_video_path,
)
from flow2api.services.dashboard_events import events
from flow2api.services.extension_pool import get_extension_pool
from flow2api.services.flow_client import get_flow_client
from flow2api.services.flow_sdk import get_media_http_status, parse_get_media_image
from flow2api.services.health_cache import get_health_payload
from flow2api.services.image_upsample import run_upsample_image
from flow2api.services.video_upsample import run_upsample_video

logger = logging.getLogger(__name__)

router = APIRouter(tags=["system"])


class UpsampleImageBody(BaseModel):
    media_id: str | None = None
    request_id: str | None = None
    index: int = 0
    project_id: str | None = None
    profile_id: str | None = None
    target_resolution: str = "UPSAMPLE_IMAGE_RESOLUTION_4K"


class UpsampleVideoBody(BaseModel):
    media_id: str | None = None
    request_id: str | None = None
    index: int = 0
    project_id: str | None = None
    profile_id: str | None = None
    aspect_ratio: str | None = None
    workflow_id: str | None = None


def _bearer_token(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing_bearer_token")
    token = authorization.split(" ", 1)[1].strip()
    if not get_api_key_by_token(token):
        raise HTTPException(401, "invalid_api_key")
    return token


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


@router.get("/video/{request_id}")
@router.get("/video/{request_id}/{index}")
async def serve_stored_video(request_id: str, index: int = 0):
    path = resolve_stored_video_path(request_id, index)
    if not path:
        try:
            path = await materialize_request_video(request_id, index)
        except Exception as exc:
            logger.warning(
                "serve_stored_video materialize failed %s: %s",
                str(request_id)[:12],
                exc,
            )
            path = None
    if not path:
        raise HTTPException(404, "not_found")
    return FileResponse(path, media_type="video/mp4")


@router.get("/image/{request_id}")
@router.get("/image/{request_id}/{index}")
async def serve_stored_image(request_id: str, index: int = 0):
    path = resolve_stored_image_path(request_id, index)
    if not path:
        raise HTTPException(404, "not_found")
    ext = path.suffix.lower()
    mime = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(ext, "image/jpeg")
    return FileResponse(path, media_type=mime)


@router.get("/outputs/{request_id}/{filename}")
async def serve_stored_output(request_id: str, filename: str):
    if ".." in request_id or ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(400, "invalid path")
    from flow2api.config import OUTPUTS_DIR

    path = OUTPUTS_DIR / request_id / filename
    if not path.is_file():
        raise HTTPException(404, "not_found")
    ext = path.suffix.lower()
    if ext == ".mp4":
        return FileResponse(path, media_type="video/mp4")
    mime = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(ext, "application/octet-stream")
    return FileResponse(path, media_type=mime)


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
    media_id = str(media_id or "").strip().split("/")[0]
    if not media_id:
        raise HTTPException(400, "invalid media id")

    try:
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
            raise HTTPException(404, "not_found") from None

        status = get_media_http_status(resp if isinstance(resp, dict) else {})
        if status == 404 or status >= 400:
            if status >= 400 and status != 404:
                logger.warning("serve_media upstream status=%s id=%s", status, media_id[:12])
            raise HTTPException(404, "not_found")

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

        from flow2api.services.flow_sdk import try_fetch_media_video_url

        try:
            video_ref = await try_fetch_media_video_url(client, media_id)
        except Exception as exc:
            logger.warning("serve_media video fetch failed %s: %s", media_id[:12], exc)
            video_ref = None

        local = VIDEOS_DIR / f"{media_id}.mp4"
        if local.is_file():
            return FileResponse(local, media_type="video/mp4")

        if video_ref and str(video_ref).startswith(("http://", "https://")):
            try:
                cap = max(10.0, min(60.0, float(HTTP_HANDLER_TIMEOUT_S or 25) * 2))
                async with httpx.AsyncClient(timeout=cap, follow_redirects=True) as http:
                    fetched = await http.get(video_ref)
                if fetched.status_code == 200 and fetched.content:
                    media_type = fetched.headers.get("content-type") or "video/mp4"
                    return Response(content=fetched.content, media_type=media_type)
            except Exception as exc:
                logger.warning("serve_media fetch video url failed %s: %s", media_id[:12], exc)

        raise HTTPException(404, "not_found")
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("serve_media unexpected %s: %s", media_id[:12], exc)
        raise HTTPException(404, "not_found") from None


@router.post("/api/flow/upsample-image")
async def flow_upsample_image(
    body: UpsampleImageBody,
    _: str = Depends(_bearer_token),
):
    return await run_upsample_image(
        media_id=body.media_id,
        request_id=body.request_id,
        index=body.index,
        project_id=body.project_id,
        profile_id=body.profile_id,
        target_resolution=body.target_resolution,
    )


@router.post("/api/flow/upsample-video")
async def flow_upsample_video(
    body: UpsampleVideoBody,
    _: str = Depends(_bearer_token),
):
    return await run_upsample_video(
        media_id=body.media_id,
        request_id=body.request_id,
        index=body.index,
        project_id=body.project_id,
        profile_id=body.profile_id,
        aspect_ratio=body.aspect_ratio,
        workflow_id=body.workflow_id,
    )


@router.get("/api/events")
async def sse_events():
    from fastapi.responses import StreamingResponse

    return StreamingResponse(events.stream(), media_type="text/event-stream")
