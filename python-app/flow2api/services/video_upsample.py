"""Upscale generated videos to 1080p for external API callers."""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

import httpx
from fastapi import HTTPException

from flow2api.config import HTTP_HANDLER_TIMEOUT_S, PUBLIC_BASE_URL
from flow2api.services import activity
from flow2api.services.flow_client import get_flow_client_for_profile
from flow2api.services.flow_sdk import (
    FlowApiError,
    VIDEO_RESOLUTION_1080P,
    extract_workflow_id_from_result,
    normalize_source_video_media_id,
    upsample_video,
)

logger = logging.getLogger(__name__)

_VIDEO_REQUEST_TYPES = frozenset(
    {
        "gen_text_video",
        "gen_image_video",
        "gen_video",
        "gen_video_start_end",
        "gen_multi_image_video",
    }
)


def _absolute_url(path: str) -> str:
    p = str(path or "").strip()
    if not p:
        return ""
    if p.startswith(("http://", "https://")):
        return p
    base = str(PUBLIC_BASE_URL or "").rstrip("/")
    if base:
        return f"{base}{p if p.startswith('/') else '/' + p}"
    return p


def _is_video_request_type(req_type: str) -> bool:
    raw = str(req_type or "").strip().lower()
    if raw in _VIDEO_REQUEST_TYPES:
        return True
    return "video" in raw and "upsample" not in raw


def _invalid_argument_hint() -> str:
    return (
        "missing_profile_id — upscale video phải dùng đúng Chrome profile đã tạo video "
        "(hoặc gửi request_id từ task done)"
    )


def resolve_upsample_video_inputs(
    *,
    media_id: Optional[str] = None,
    request_id: Optional[str] = None,
    index: int = 0,
    project_id: Optional[str] = None,
    profile_id: Optional[str] = None,
    aspect_ratio: Optional[str] = None,
    workflow_id: Optional[str] = None,
) -> tuple[str, str, str, str, str]:
    """Resolve media/project/profile/aspect/workflow from explicit ids or a video task."""
    mid = normalize_source_video_media_id(str(media_id or "").strip())
    pid = str(project_id or "").strip()
    prof = str(profile_id or "").strip()
    aspect = str(aspect_ratio or "").strip() or "16:9"
    wid = str(workflow_id or "").strip()

    rid = str(request_id or "").strip()
    if rid:
        row = activity.get_request(rid)
        if not row:
            raise HTTPException(404, "request_not_found")
        if row.status != "done":
            raise HTTPException(409, f"request_not_done (status={row.status})")
        if not _is_video_request_type(row.type):
            raise HTTPException(400, "request_not_video")
        result = json.loads(row.result_json or "{}")
        params = json.loads(row.params_json or "{}")
        if not pid:
            pid = str(result.get("project_id") or "").strip()
        if not prof:
            prof = str(result.get("profile_id") or params.get("profile_id") or "").strip()
        if not aspect_ratio:
            aspect = str(params.get("aspect_ratio") or "16:9").strip() or "16:9"
        if not wid:
            wid = extract_workflow_id_from_result(result)
        if not mid:
            media_ids = [
                normalize_source_video_media_id(str(m))
                for m in (result.get("media_ids") or [])
                if str(m).strip()
            ]
            if not media_ids:
                entries = result.get("media_entries") or []
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    m = normalize_source_video_media_id(
                        str(entry.get("media_id") or entry.get("mediaId") or "")
                    )
                    if m:
                        media_ids.append(m)
            if not media_ids:
                raise HTTPException(400, "request_has_no_media_id")
            idx = max(0, min(int(index or 0), len(media_ids) - 1))
            mid = media_ids[idx]

    if not mid:
        raise HTTPException(400, "missing_media_id")
    if not pid:
        raise HTTPException(
            400,
            "missing_project_id — truyền request_id hoặc project_id từ task video done",
        )
    if not prof:
        raise HTTPException(
            400,
            "missing_profile_id — upscale video phải dùng đúng Chrome profile đã tạo video",
        )
    return mid, pid, prof, aspect, wid


async def run_upsample_video(
    *,
    media_id: Optional[str] = None,
    request_id: Optional[str] = None,
    index: int = 0,
    project_id: Optional[str] = None,
    profile_id: Optional[str] = None,
    aspect_ratio: Optional[str] = None,
    workflow_id: Optional[str] = None,
) -> dict[str, Any]:
    mid, pid, prof, aspect, wid = resolve_upsample_video_inputs(
        media_id=media_id,
        request_id=request_id,
        index=index,
        project_id=project_id,
        profile_id=profile_id,
        aspect_ratio=aspect_ratio,
        workflow_id=workflow_id,
    )

    try:
        client = get_flow_client_for_profile(prof)
    except RuntimeError as exc:
        if str(exc) == "profile_not_ready":
            raise HTTPException(
                503,
                "profile_not_ready — mở đúng Chrome profile đã tạo video và extension Flow",
            ) from exc
        raise

    if not client.connected:
        raise HTTPException(503, "extension_not_connected")
    if not client.flow_key:
        raise HTTPException(503, "no_flow_token")

    import asyncio

    await client.fetch_paygate_tier()
    if not client.paygate_tier:
        await asyncio.sleep(0.8)
        await client.fetch_paygate_tier()
    try:
        await client.refresh_flow_token()
    except Exception:
        pass

    try:
        raw = await asyncio.wait_for(
            upsample_video(
                client,
                media_id=mid,
                project_id=pid,
                aspect_ratio=aspect,
                workflow_id=wid,
            ),
            timeout=900,
        )
    except asyncio.TimeoutError:
        raise HTTPException(504, "upsample_video_timeout") from None
    except FlowApiError as exc:
        raise HTTPException(502, str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("upsample_video failed %s profile=%s: %s", mid[:12], prof[:8], exc)
        raise HTTPException(502, "upsample_video_failed") from exc

    return format_upsample_video_response(
        raw,
        source_media_id=mid,
        project_id=pid,
        profile_id=prof or client.profile_id,
        aspect_ratio=aspect,
        workflow_id=wid,
    )


def format_upsample_video_response(
    raw: dict[str, Any],
    *,
    source_media_id: str,
    project_id: str,
    profile_id: str = "",
    aspect_ratio: str = "16:9",
    workflow_id: str = "",
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "source_media_id": source_media_id,
        "project_id": project_id,
        "target_resolution": VIDEO_RESOLUTION_1080P,
        "aspect_ratio": aspect_ratio,
    }
    if profile_id:
        out["profile_id"] = profile_id
    if workflow_id:
        out["workflow_id"] = workflow_id

    urls = [str(u).strip() for u in (raw.get("video_urls") or []) if str(u).strip()]
    media_ids = [str(m).strip() for m in (raw.get("media_ids") or []) if str(m).strip()]
    upsampled_id = media_ids[0] if media_ids else ""

    if upsampled_id:
        out["media_id"] = upsampled_id
        out["upsampled_media_id"] = upsampled_id
    if urls:
        out["video_url"] = _absolute_url(urls[0])
        out["video_urls"] = [_absolute_url(u) for u in urls]
    elif upsampled_id:
        local = f"/media/{upsampled_id}"
        out["video_url"] = _absolute_url(local)
        out["video_urls"] = [out["video_url"]]

    if not out.get("video_url") and not out.get("video_urls"):
        if raw.get("raw"):
            out["raw"] = raw.get("raw")
        else:
            raise HTTPException(502, "upsample_no_video")

    return out


async def fetch_upsample_video_bytes(payload: dict[str, Any]) -> tuple[bytes, str]:
    """Download upscaled video bytes from API payload."""
    url = str(payload.get("video_url") or "").strip()
    if not url:
        urls = payload.get("video_urls") or []
        url = str(urls[0] or "").strip() if urls else ""
    if not url:
        raise HTTPException(502, "upsample_no_video")

    cap = max(30.0, min(300.0, float(HTTP_HANDLER_TIMEOUT_S or 25) * 8))
    try:
        async with httpx.AsyncClient(timeout=cap, follow_redirects=True) as http:
            resp = await http.get(url)
        if resp.status_code != 200 or not resp.content:
            raise HTTPException(502, "upsample_video_download_failed")
        mime = resp.headers.get("content-type") or "video/mp4"
        return resp.content, mime
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("fetch upsample video failed: %s", exc)
        raise HTTPException(502, "upsample_video_download_failed") from exc
