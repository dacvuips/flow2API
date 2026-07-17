"""Upscale generated images to 2K/4K for external API callers."""
from __future__ import annotations

import base64
import json
import logging
from typing import Any, Optional

import httpx
from fastapi import HTTPException

from flow2api.config import HTTP_HANDLER_TIMEOUT_S, get_public_base_url
from flow2api.services import activity
from flow2api.services.flow_client import get_flow_client_for_profile
from flow2api.services.flow_sdk import FlowApiError, ensure_project, upsample_image

logger = logging.getLogger(__name__)

UPSAMPLE_RESOLUTION_2K = "UPSAMPLE_IMAGE_RESOLUTION_2K"
UPSAMPLE_RESOLUTION_4K = "UPSAMPLE_IMAGE_RESOLUTION_4K"


def normalize_target_resolution(value: str) -> str:
    """Accept 2k/4k shorthand or full UPSAMPLE_IMAGE_RESOLUTION_* enum."""
    raw = str(value or "").strip()
    if not raw:
        return UPSAMPLE_RESOLUTION_4K
    upper = raw.upper()
    if upper in ("2K", UPSAMPLE_RESOLUTION_2K):
        return UPSAMPLE_RESOLUTION_2K
    if upper in ("4K", UPSAMPLE_RESOLUTION_4K):
        return UPSAMPLE_RESOLUTION_4K
    raise HTTPException(400, "invalid_target_resolution")


def upsample_resolution_label(value: str) -> str:
    return "2k" if normalize_target_resolution(value) == UPSAMPLE_RESOLUTION_2K else "4k"


def _absolute_url(path: str) -> str:
    p = str(path or "").strip()
    if not p:
        return ""
    if p.startswith(("http://", "https://")):
        return p
    base = str(get_public_base_url() or "").rstrip("/")
    if base:
        return f"{base}{p if p.startswith('/') else '/' + p}"
    return p


def resolve_upsample_inputs(
    *,
    media_id: Optional[str] = None,
    request_id: Optional[str] = None,
    index: int = 0,
    project_id: Optional[str] = None,
    profile_id: Optional[str] = None,
) -> tuple[str, str, str]:
    """Resolve media_id + project_id + profile_id from explicit ids or a gen_image task."""
    mid = str(media_id or "").strip()
    pid = str(project_id or "").strip()
    prof = str(profile_id or "").strip()

    rid = str(request_id or "").strip()
    if rid:
        row = activity.get_request(rid)
        if not row:
            raise HTTPException(404, "request_not_found")
        if row.status != "done":
            raise HTTPException(409, f"request_not_done (status={row.status})")
        if row.type != "gen_image":
            raise HTTPException(400, "request_not_image")
        result = json.loads(row.result_json or "{}")
        params = json.loads(row.params_json or "{}")
        if not pid:
            pid = str(result.get("project_id") or "").strip()
        if not prof:
            prof = str(result.get("profile_id") or params.get("profile_id") or "").strip()
        if not mid:
            media_ids = [str(m).strip() for m in (result.get("media_ids") or []) if str(m).strip()]
            if not media_ids:
                entries = result.get("media_entries") or []
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    m = str(entry.get("media_id") or entry.get("mediaId") or "").strip()
                    if m:
                        media_ids.append(m)
            if not media_ids:
                raise HTTPException(400, "request_has_no_media_id")
            idx = max(0, min(int(index or 0), len(media_ids) - 1))
            mid = media_ids[idx]

    if not mid:
        raise HTTPException(400, "missing_media_id")
    return mid, pid, prof


def build_upsample_image_job_params(
    *,
    media_id: Optional[str] = None,
    request_id: Optional[str] = None,
    index: int = 0,
    project_id: Optional[str] = None,
    profile_id: Optional[str] = None,
    target_resolution: str = UPSAMPLE_RESOLUTION_4K,
) -> dict[str, Any]:
    """Resolved worker params — validates source image task / ids."""
    resolution = normalize_target_resolution(target_resolution)
    mid, pid, prof = resolve_upsample_inputs(
        media_id=media_id,
        request_id=request_id,
        index=index,
        project_id=project_id,
        profile_id=profile_id,
    )
    out: dict[str, Any] = {
        "media_id": mid,
        "project_id": pid,
        "profile_id": prof,
        "target_resolution": resolution,
    }
    if request_id:
        out["source_request_id"] = str(request_id).strip()
    return out


async def execute_upsample_image_on_client(
    client,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Run image upsample on a bound Flow client (worker or sync HTTP)."""
    import asyncio

    mid = str(params.get("media_id") or "").strip()
    pid = str(params.get("project_id") or "").strip()
    prof = str(params.get("profile_id") or "").strip()
    resolution = normalize_target_resolution(
        str(params.get("target_resolution") or UPSAMPLE_RESOLUTION_4K)
    )

    if not client.paygate_tier:
        await client.fetch_paygate_tier()

    if not pid:
        try:
            pid = await ensure_project(client)
        except Exception as exc:
            logger.warning("upsample ensure_project failed: %s", exc)
            raise RuntimeError("project_unavailable") from exc

    raw = await asyncio.wait_for(
        upsample_image(
            client,
            media_id=mid,
            project_id=pid,
            target_resolution=resolution,
        ),
        timeout=300,
    )
    return format_upsample_response(
        raw,
        source_media_id=mid,
        project_id=pid,
        profile_id=prof or getattr(client, "profile_id", ""),
        target_resolution=resolution,
    )


async def run_upsample_image(
    *,
    media_id: Optional[str] = None,
    request_id: Optional[str] = None,
    index: int = 0,
    project_id: Optional[str] = None,
    profile_id: Optional[str] = None,
    target_resolution: str = UPSAMPLE_RESOLUTION_4K,
) -> dict[str, Any]:
    params = build_upsample_image_job_params(
        media_id=media_id,
        request_id=request_id,
        index=index,
        project_id=project_id,
        profile_id=profile_id,
        target_resolution=target_resolution,
    )
    mid = str(params.get("media_id") or "")
    prof = str(params.get("profile_id") or "")

    try:
        client = get_flow_client_for_profile(prof or None)
    except RuntimeError as exc:
        if str(exc) == "profile_not_ready":
            raise HTTPException(503, "profile_not_ready") from exc
        raise
    if not client.connected and not client.has_direct_lane():
        raise HTTPException(503, "extension_not_connected")
    if not client.flow_key:
        raise HTTPException(503, "no_flow_token")

    import asyncio

    try:
        return await execute_upsample_image_on_client(client, params)
    except asyncio.TimeoutError:
        raise HTTPException(504, "upsample_timeout") from None
    except FlowApiError as exc:
        raise HTTPException(502, str(exc)) from exc
    except RuntimeError as exc:
        if str(exc) == "project_unavailable":
            raise HTTPException(503, "project_unavailable") from exc
        raise
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("upsample_image failed %s: %s", mid[:12], exc)
        raise HTTPException(502, "upsample_failed") from exc


def format_upsample_response(
    raw: dict[str, Any],
    *,
    source_media_id: str,
    project_id: str,
    profile_id: str = "",
    target_resolution: str = UPSAMPLE_RESOLUTION_4K,
) -> dict[str, Any]:
    """Normalize upsample result for external JSON API."""
    resolution = normalize_target_resolution(target_resolution)
    out: dict[str, Any] = {
        "source_media_id": source_media_id,
        "project_id": project_id,
        "target_resolution": resolution,
    }
    if profile_id:
        out["profile_id"] = profile_id

    url = str(raw.get("url") or "").strip()
    upsampled_id = str(raw.get("media_id") or "").strip()
    encoded = str(raw.get("encoded_image") or "").strip()

    if upsampled_id:
        out["media_id"] = upsampled_id
        out["upsampled_media_id"] = upsampled_id
    if url:
        out["url"] = _absolute_url(url)
        out["image_url"] = out["url"]
    elif upsampled_id:
        local = f"/media/{upsampled_id}"
        out["url"] = _absolute_url(local)
        out["image_url"] = out["url"]

    if encoded:
        if encoded.startswith("data:"):
            out["data_url"] = encoded
        else:
            out["data_url"] = f"data:image/jpeg;base64,{encoded}"
        out["has_base64"] = True

    if not out.get("url") and not out.get("data_url"):
        if raw.get("raw"):
            out["raw"] = raw.get("raw")
        else:
            raise HTTPException(502, "upsample_no_image")

    return out


async def fetch_upsample_image_bytes(payload: dict[str, Any]) -> tuple[bytes, str]:
    """Download upscaled image bytes from API payload."""
    data_url = str(payload.get("data_url") or "")
    if data_url.startswith("data:image/"):
        try:
            header, _, body = data_url.partition(",")
            raw = base64.b64decode(body)
            mime = "image/jpeg"
            if "png" in header.lower():
                mime = "image/png"
            elif "webp" in header.lower():
                mime = "image/webp"
            return raw, mime
        except Exception as exc:
            raise HTTPException(502, "invalid_base64_image") from exc

    url = str(payload.get("url") or payload.get("image_url") or "").strip()
    if not url:
        raise HTTPException(502, "upsample_no_image")

    cap = max(10.0, min(120.0, float(HTTP_HANDLER_TIMEOUT_S or 25) * 4))
    try:
        async with httpx.AsyncClient(timeout=cap, follow_redirects=True) as http:
            resp = await http.get(url)
        if resp.status_code != 200 or not resp.content:
            raise HTTPException(502, "upsample_download_failed")
        mime = resp.headers.get("content-type") or "image/jpeg"
        return resp.content, mime
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("fetch upsample image failed: %s", exc)
        raise HTTPException(502, "upsample_download_failed") from exc
