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
from flow2api.services.result_media import (
    prepare_params_for_manual_retry,
    prepare_params_for_worker_requeue,
    with_base64_media,
)
from flow2api.services.request_params import get_video_quality, normalize_request_params
from flow2api.services.image_upsample import (
    build_upsample_image_job_params,
    fetch_upsample_image_bytes,
    run_upsample_image,
    upsample_resolution_label,
)
from flow2api.services.video_upsample import (
    build_upsample_video_job_params,
    execute_upsample_video_on_client,
    fetch_upsample_video_bytes,
    run_upsample_video,
)
from flow2api.short_id import new_request_id
from flow2api.services.flow_client import apply_user_profile_assignment
from flow2api.services.extension_pool import get_extension_pool
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
    video_media_id: Optional[str] = None
    media_generation_id: Optional[str] = None
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


class UpsampleVideoRequest(BaseModel):
    media_id: Optional[str] = None
    request_id: Optional[str] = None
    index: int = 0
    project_id: Optional[str] = None
    profile_id: Optional[str] = None
    aspect_ratio: Optional[str] = None
    workflow_id: Optional[str] = None


class AssignRequestProfileBody(BaseModel):
    profile_id: Optional[str] = None


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
    params = normalize_request_params(dict(body.params))
    if body.type == "unsupported":
        raise HTTPException(400, params.get("error") or "unsupported")
    # Mọi request video → ép Veo 3.1 Lite [Lower Priority]
    if "video" in str(body.type or "").lower():
        from flow2api.services.request_params import FORCED_VIDEO_QUALITY

        params["video_quality"] = FORCED_VIDEO_QUALITY
    pid = str(params.get("profile_id") or "").strip()
    if pid:
        try:
            params = apply_user_profile_assignment(params, pid)
        except ValueError as exc:
            raise HTTPException(400, "profile_not_found") from exc
    prompt = str(params.get("prompt") or "")
    model = params.get("image_model") or get_video_quality(params, "lite_relaxed" if "video" in str(body.type or "").lower() else "") or ""
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
    download: bool = Query(
        False,
        description="Tải file khi job done (dùng với sync=true hoặc GET /api/requests/{id}?download=true)",
    ),
    sync: bool = Query(
        False,
        description="Chờ xong trong 1 request (chỉ local; qua Cloudflare dùng async mặc định)",
    ),
    api_key_id: int = Depends(_auth_key_id),
):
    """Upscale ảnh đã generate lên 2K/4K. Mặc định enqueue worker (tránh Cloudflare 504)."""
    params = build_upsample_image_job_params(
        media_id=body.media_id,
        request_id=body.request_id,
        index=body.index,
        project_id=body.project_id,
        profile_id=body.profile_id,
        target_resolution=body.target_resolution,
    )
    if sync:
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
            "POST /api/requests/upsample-image (sync)",
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

    label = upsample_resolution_label(str(params.get("target_resolution") or ""))
    rid = new_request_id()
    activity.create_request(
        rid,
        "upsample_image",
        f"upsample {label}",
        label,
        params,
        api_key_id=api_key_id,
    )
    append_request_log(
        rid,
        "http",
        "POST /api/requests/upsample-image",
        level="info",
        data={"source_request_id": params.get("source_request_id"), "async": True},
    )
    if download:
        return {
            "id": rid,
            "status": "queued",
            "poll": f"/api/requests/{rid}",
            "download_when_done": f"/api/requests/{rid}?download=true",
            "hint": "Poll đến status=done rồi GET download_when_done (upscale có thể >100s qua CF)",
        }
    return {"id": rid, "status": "queued"}


@router.post("/upsample-video")
async def upsample_video_external(
    body: UpsampleVideoRequest,
    download: bool = Query(False, description="Tải file MP4 khi job done (dùng với async=false)"),
    sync: bool = Query(
        False,
        description="Chờ xong trong 1 request (chỉ local; qua Cloudflare dùng async mặc định)",
    ),
    api_key_id: int = Depends(_auth_key_id),
):
    """Upscale video đã generate lên 1080p. Mặc định enqueue worker (tránh Cloudflare 502)."""
    params = build_upsample_video_job_params(
        media_id=body.media_id,
        request_id=body.request_id,
        index=body.index,
        project_id=body.project_id,
        profile_id=body.profile_id,
        aspect_ratio=body.aspect_ratio,
        workflow_id=body.workflow_id,
    )
    if sync:
        result = await run_upsample_video(
            media_id=body.media_id,
            request_id=body.request_id,
            index=body.index,
            project_id=body.project_id,
            profile_id=body.profile_id,
            aspect_ratio=body.aspect_ratio,
            workflow_id=body.workflow_id,
        )
        append_request_log(
            body.request_id or result.get("source_media_id") or "-",
            "http",
            "POST /api/requests/upsample-video (sync)",
            level="info",
            data={"upsampled_media_id": result.get("media_id"), "download": download},
        )
        if download:
            raw, mime = await fetch_upsample_video_bytes(result)
            mid = str(result.get("source_media_id") or "video")[:8]
            return Response(
                content=raw,
                media_type=mime,
                headers={
                    "Content-Disposition": f'attachment; filename="flow-1080p-{mid}.mp4"'
                },
            )
        return result

    rid = new_request_id()
    activity.create_request(
        rid,
        "upsample_video",
        "upsample 1080p",
        "1080p",
        params,
        api_key_id=api_key_id,
    )
    append_request_log(
        rid,
        "http",
        "POST /api/requests/upsample-video",
        level="info",
        data={"source_request_id": params.get("source_request_id"), "async": True},
    )
    if download:
        return {
            "id": rid,
            "status": "queued",
            "poll": f"/api/requests/{rid}",
            "download_when_done": f"/api/requests/{rid}?download=true",
            "hint": "Poll đến status=done rồi GET download_when_done (upscale mất vài phút)",
        }
    return {"id": rid, "status": "queued"}


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


@router.put("/{request_id}/profile")
async def assign_request_profile(
    request_id: str,
    body: AssignRequestProfileBody,
    _=Depends(_auth_key_id),
):
    row = activity.get_request(request_id)
    if not row:
        raise HTTPException(404, "not_found")
    status = str(row.status or "")
    if status not in ("queued", "running"):
        raise HTTPException(409, f"cannot_assign_profile (status={status})")
    params = json.loads(row.params_json or "{}")
    was_running = status == "running"
    if was_running:
        params = prepare_params_for_worker_requeue(
            params,
            request_id,
            prompt=str(row.prompt or ""),
        )
    try:
        params = apply_user_profile_assignment(params, body.profile_id)
    except ValueError as exc:
        raise HTTPException(400, "profile_not_found") from exc
    if was_running:
        activity.requeue_request(request_id, params)
        get_worker().prepare_retry(request_id)
        log_msg = (
            f"PUT /api/requests/{request_id}/profile → {body.profile_id or 'auto'} "
            "(requeued from running)"
        )
    else:
        activity.update_request(request_id, params=params)
        log_msg = f"PUT /api/requests/{request_id}/profile → {body.profile_id or 'auto'}"
    append_request_log(
        request_id,
        "http",
        log_msg,
        level="info",
    )
    events.publish(
        "queue_changed",
        {"id": request_id, "profile_id": body.profile_id, "requeued": was_running},
    )
    return {
        "id": request_id,
        "status": "queued" if was_running else status,
        "profile_id": params.get("profile_id"),
        "profile_assigned_by_user": bool(params.get("profile_assigned_by_user")),
        "profiles": get_extension_pool().list_public(),
        "requeued": was_running,
        "ok": True,
    }


@router.get("/{request_id}")
async def get_request_status(
    request_id: str,
    download: bool = Query(
        False,
        description="Tải file kết quả (ảnh/video) khi status=done",
    ),
    index: int = Query(0, ge=0, description="Chỉ số ảnh/video khi download=true"),
    _=Depends(_auth_key_id),
):
    row = activity.get_request(request_id)
    if not row:
        raise HTTPException(404, "not_found")
    if download:
        if row.status != "done":
            raise HTTPException(409, f"not_ready (status={row.status})")
        result = json.loads(row.result_json or "{}")
        if row.type == "upsample_image":
            raw, mime = await fetch_upsample_image_bytes(result)
            mid = str(result.get("source_media_id") or request_id)[:8]
            label = upsample_resolution_label(
                str(result.get("target_resolution") or "")
            )
            ext = "png" if "png" in mime else "jpg"
            return Response(
                content=raw,
                media_type=mime,
                headers={
                    "Content-Disposition": (
                        f'attachment; filename="flow-{label}-{mid}.{ext}"'
                    )
                },
            )
        if row.type == "upsample_video":
            raw, mime = await fetch_upsample_video_bytes(result)
            mid = str(result.get("source_media_id") or request_id)[:8]
            return Response(
                content=raw,
                media_type=mime,
                headers={
                    "Content-Disposition": f'attachment; filename="flow-1080p-{mid}.mp4"'
                },
            )

        from flow2api.services.stored_media import (
            materialize_request_video,
            resolve_stored_image_path,
            resolve_stored_video_path,
        )

        is_video = "video" in str(row.type or "").lower()
        if is_video:
            path = resolve_stored_video_path(request_id, index)
            if not path:
                path = await materialize_request_video(request_id, index, result)
            if not path:
                raise HTTPException(404, "result_file_not_found")
            raw = path.read_bytes()
            return Response(
                content=raw,
                media_type="video/mp4",
                headers={
                    "Content-Disposition": (
                        f'attachment; filename="flow-{request_id[:8]}-{index}.mp4"'
                    )
                },
            )

        path = resolve_stored_image_path(request_id, index)
        if not path:
            raise HTTPException(404, "result_file_not_found")
        ext = path.suffix.lower().lstrip(".") or "jpg"
        if ext == "jpeg":
            ext = "jpg"
        mime = {
            "jpg": "image/jpeg",
            "png": "image/png",
            "webp": "image/webp",
        }.get(ext, "image/jpeg")
        return Response(
            content=path.read_bytes(),
            media_type=mime,
            headers={
                "Content-Disposition": (
                    f'attachment; filename="flow-{request_id[:8]}-{index}.{ext}"'
                )
            },
        )
    data = activity.record_to_public(row)
    return await with_base64_media(data, embed=False)


@router.delete("/{request_id}")
async def cancel_request(request_id: str, _=Depends(_auth_key_id)):
    row = activity.get_request(request_id)
    if not row:
        raise HTTPException(404, "not_found")
    if row.status not in ("queued", "running"):
        raise HTTPException(409, f"cannot cancel (status={row.status})")
    worker = get_worker()
    worker.request_cancel(request_id)
    worker.cancel_running_tasks({request_id})
    append_request_log(request_id, "http", "DELETE /api/requests — cancel", level="warn")
    activity.update_request(
        request_id,
        status="failed: canceled",
        error="canceled",
        result={"error": "canceled"},
    )
    events.publish("request_finished", {"id": request_id, "status": "canceled"})
    return {"id": request_id, "status": "canceled"}
