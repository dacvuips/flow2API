from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from flow2api.services import activity
from flow2api.services.auth_keys import get_api_key_by_token
from flow2api.services.dashboard_events import events
from flow2api.services.result_media import with_base64_media
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
    activity.create_request(rid, body.type, prompt, str(model), params, api_key_id=api_key_id)
    return {"id": rid, "status": "queued"}


@router.get("/{request_id}")
async def get_request_status(request_id: str, _=Depends(_auth_key_id)):
    row = activity.get_request(request_id)
    if not row:
        raise HTTPException(404, "not_found")
    data = activity.record_to_public(row)
    return await with_base64_media(data, embed=True)


@router.delete("/{request_id}")
async def cancel_request(request_id: str, _=Depends(_auth_key_id)):
    row = activity.get_request(request_id)
    if not row:
        raise HTTPException(404, "not_found")
    if row.status not in ("queued", "running"):
        raise HTTPException(409, f"cannot cancel (status={row.status})")
    get_worker().request_cancel(request_id)
    activity.update_request(
        request_id,
        status="failed: canceled",
        error="canceled",
        result={"error": "canceled"},
    )
    events.publish("request_finished", {"id": request_id, "status": "canceled"})
    return {"id": request_id, "status": "canceled"}
