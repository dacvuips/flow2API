from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from flow2api.services import activity
from flow2api.services.auth_keys import get_api_key_by_token
from flow2api.services.request_logs import append_request_log
from flow2api.services.dashboard_events import events
from flow2api.services.result_media import prepare_params_for_manual_retry, with_base64_media
from flow2api.services.image_upsample import (
    fetch_upsample_image_bytes,
    run_upsample_image,
    upsample_resolution_label,
)
from flow2api.short_id import new_request_id
from flow2api.worker.processor import get_worker

router = APIRouter(prefix="/api/requests", tags=["requests"])


class RequestParams(BaseModel):
    prompt: Optional[str] = None
    aspect_ratio: str = "16:9"
    image_model: Optional[str] = None
    variant_count: Optional[int] = 1
    video_quality: Optional[str] = None
    image_base64s: Optional[list[str]] = None
    image_input_types: Optional[list[str]] = None
    video_mode: Optional[str] = None  # "frame" | "component" — bắt buộc với gen_image_video
    start_media_id: Optional[str] = None
    end_media_id: Optional[str] = None
    reference_media_ids: Optional[list[str]] = None


class CreateRequestBody(BaseModel):
    type: str
    params: dict[str, Any] = Field(default_factory=dict)


class UpsampleImageRequest(BaseModel):
    media_id: Optional[str] = None
    request_id: Optional[str] = None
    index: int = 0
    project_id: Optional[str] = None
    profile_id: Optional[str] = None
    target_resolution: str = "UPSAMPLE_IMAGE_RESOLUTION_4K"


_RETRY_DROP_KEYS = frozenset(
    {
        "profile_id",
        "profile_label",
        "profile_email",
        "recaptcha_retry_count",
        "get_media_404_retry_count",
        "upload_internal_retry_count",
        "extension_timeout_retry_count",
        "prominent_people_retry_count",
        "invalid_argument_retry_count",
        "trpc_401_retry_count",
        "retry_not_before",
        "running_started_at",
        "running_timeout_retry_count",
        "retry_exclude_profile_id",
        "start_media_id",
        "end_media_id",
        "reference_media_ids",
    }
)


def _params_for_retry(params: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in params.items() if k not in _RETRY_DROP_KEYS}


def _bearer(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing_bearer_token")
    return authorization.split(" ", 1)[1].strip()


def _auth_key_id(token: str = Depends(_bearer)) -> int:
    key_id = get_api_key_by_token(token)
    if not key_id:
        raise HTTPException(401, "invalid_api_key")
    return key_id


@router.post("")
async def create_request(body: CreateRequestBody, api_key_id: int = Depends(_auth_key_id)):
    params = dict(body.params)
    if body.type == "unsupported":
        raise HTTPException(400, params.get("error") or "unsupported")
    prompt = str(params.get("prompt") or "")
    model = params.get("image_model") or params.get("video_quality") or ""
    rid = new_request_id()
    image_base64s = params.get("image_base64s") or params.get("imageBase64s") or []
    if image_base64s:
        from flow2api.services.result_media import persist_input_previews

        preview_urls = persist_input_previews(rid, image_base64s)
        if preview_urls:
            params["input_preview_urls"] = preview_urls
    activity.create_request(rid, body.type, prompt, str(model), params, api_key_id=api_key_id)
    append_request_log(
        rid,
        "http",
        f"POST /api/requests type={body.type} model={model or '-'}",
        level="info",
        data={"type": body.type, "params": params},
    )
    return {"id": rid, "status": "queued"}


@router.post("/upsample-image")
async def upsample_image_external(
    body: UpsampleImageRequest,
    download: bool = Query(False, description="Trả file ảnh JPEG/PNG thay vì JSON"),
    api_key_id: int = Depends(_auth_key_id),
):
    """Upscale ảnh đã generate lên 2K/4K. Dùng media_id hoặc request_id (task gen_image done)."""
    result = await run_upsample_image(
        media_id=body.media_id,
        request_id=body.request_id,
        index=body.index,
        project_id=body.project_id,
        profile_id=body.profile_id,
        target_resolution=body.target_resolution,
    )
    append_request_log(
        body.request_id or result.get("source_media_id") or "-",
        "http",
        "POST /api/requests/upsample-image",
        level="info",
        data={
            "source_media_id": result.get("source_media_id"),
            "upsampled_media_id": result.get("media_id"),
            "download": download,
        },
    )
    if download:
        raw, mime = await fetch_upsample_image_bytes(result)
        mid = str(result.get("source_media_id") or "image")[:8]
        label = upsample_resolution_label(str(result.get("target_resolution") or ""))
        ext = "png" if "png" in mime else "jpg"
        return Response(
            content=raw,
            media_type=mime,
            headers={
                "Content-Disposition": f'attachment; filename="flow-{label}-{mid}.{ext}"'
            },
        )
    return result


@router.post("/cancel-all")
async def cancel_all_requests(_=Depends(_auth_key_id)):
    worker = get_worker()
    rows = activity.list_active_requests()
    if not rows:
        return {"canceled": 0, "ids": []}

    ids: list[str] = []
    for row in rows:
        ids.append(row.id)
        worker.request_cancel(row.id)
        activity.update_request(
            row.id,
            status="failed: canceled",
            error="canceled",
            result={"error": "canceled"},
        )
        append_request_log(
            row.id,
            "http",
            "POST /api/requests/cancel-all",
            level="warn",
        )
        events.publish("request_finished", {"id": row.id, "status": "canceled"})

    worker.cancel_running_tasks(set(ids))
    return {"canceled": len(ids), "ids": ids}


@router.post("/{request_id}/retry")
async def retry_request(request_id: str, api_key_id: int = Depends(_auth_key_id)):
    row = activity.get_request(request_id)
    if not row:
        raise HTTPException(404, "not_found")
    params = prepare_params_for_manual_retry(
        json.loads(row.params_json or "{}"),
        request_id,
    )
    activity.requeue_request(request_id, params)
    get_worker().prepare_retry(request_id)
    append_request_log(
        request_id,
        "http",
        f"POST /api/requests/{request_id}/retry",
        level="info",
        data={"type": row.type},
    )
    events.publish("request_finished", {"id": request_id, "status": "queued"})
    return {"id": request_id, "status": "queued"}


@router.get("/{request_id}")
async def get_request_status(request_id: str, _=Depends(_auth_key_id)):
    row = activity.get_request(request_id)
    if not row:
        raise HTTPException(404, "not_found")
    data = activity.record_to_public(row)
    return await with_base64_media(data, embed=False)


@router.delete("/{request_id}")
async def cancel_request(request_id: str, _=Depends(_auth_key_id)):
    row = activity.get_request(request_id)
    if not row:
        raise HTTPException(404, "not_found")
    if row.status not in ("queued", "running"):
        raise HTTPException(409, f"cannot cancel (status={row.status})")
    get_worker().request_cancel(request_id)
    append_request_log(request_id, "http", "DELETE /api/requests — cancel", level="warn")
    activity.update_request(
        request_id,
        status="failed: canceled",
        error="canceled",
        result={"error": "canceled"},
    )
    events.publish("request_finished", {"id": request_id, "status": "canceled"})
    return {"id": request_id, "status": "canceled"}
