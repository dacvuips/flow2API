from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from flow2api.services import activity
from flow2api.services.auth_keys import get_api_key_by_token
from flow2api.services.dashboard_events import events
from flow2api.services.extension_pool import get_extension_pool
from flow2api.services.worker_settings import (
    get_worker_settings,
    save_profile_limit,
    save_worker_settings,
    set_profile_credit_allowed,
    set_profile_dispatch_enabled,
    set_profile_media_allowed,
)
from flow2api.worker.processor import get_worker

router = APIRouter(prefix="/api/worker", tags=["worker"])


class WorkerSettingsBody(BaseModel):
    max_concurrent: int | None = Field(None, ge=1, le=100)
    task_stagger_s: float | None = Field(None, ge=0.0, le=300.0)
    profile_default_max_concurrent: int | None = Field(None, ge=1, le=8)


class ProfileLimitBody(BaseModel):
    profile_id: str
    max_concurrent: int = Field(1, ge=1, le=8)


class ProfileLimitsBulkBody(BaseModel):
    profile_limits: dict[str, int] = Field(default_factory=dict)
    profile_default_max_concurrent: int | None = Field(None, ge=1, le=8)


class ProfileDispatchBody(BaseModel):
    enabled: bool = True


class ProfileCreditBody(BaseModel):
    allowed: bool = True


class ProfileProxyBody(BaseModel):
    enabled: bool = True


class ProfileMediaBody(BaseModel):
    image: bool | None = None
    video: bool | None = None


def _bearer(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing_bearer_token")
    return authorization.split(" ", 1)[1].strip()


def _auth_key_id(token: str = Depends(_bearer)) -> int:
    key_id = get_api_key_by_token(token)
    if not key_id:
        raise HTTPException(401, "invalid_api_key")
    return key_id


def _settings_payload() -> dict[str, Any]:
    settings = get_worker_settings()
    worker = get_worker()
    pool = get_extension_pool()
    return {
        **settings.to_dict(),
        "running_slots": worker.running_count(),
        "queued": activity.count_queued(),
        "profiles": pool.list_public(),
    }


@router.get("/settings")
async def read_worker_settings():
    return _settings_payload()


@router.put("/settings")
async def update_worker_settings(body: WorkerSettingsBody, _=Depends(_auth_key_id)):
    fields: dict[str, Any] = {}
    if body.max_concurrent is not None:
        fields["max_concurrent"] = body.max_concurrent
    if body.task_stagger_s is not None:
        fields["task_stagger_s"] = body.task_stagger_s
    if body.profile_default_max_concurrent is not None:
        fields["profile_default_max_concurrent"] = body.profile_default_max_concurrent
    saved = save_worker_settings(**fields)
    return {**saved.to_dict(), "profiles": get_extension_pool().list_public(), "ok": True}


@router.put("/profile-limits")
async def update_profile_limits(body: ProfileLimitsBulkBody, _=Depends(_auth_key_id)):
    fields: dict[str, Any] = {"profile_limits": body.profile_limits}
    if body.profile_default_max_concurrent is not None:
        fields["profile_default_max_concurrent"] = body.profile_default_max_concurrent
    saved = save_worker_settings(**fields)
    return {**saved.to_dict(), "profiles": get_extension_pool().list_public(), "ok": True}


@router.post("/nudge")
async def nudge_worker(_=Depends(_auth_key_id)):
    worker = get_worker()
    return await worker.nudge()


@router.put("/profile-limits/{profile_id}")
async def update_one_profile_limit(
    profile_id: str,
    body: ProfileLimitBody,
    _=Depends(_auth_key_id),
):
    if body.profile_id and body.profile_id != profile_id:
        profile_id = body.profile_id
    saved = save_profile_limit(profile_id, body.max_concurrent)
    return {**saved.to_dict(), "ok": True}


@router.put("/profiles/{profile_id}/dispatch")
async def update_profile_dispatch(
    profile_id: str,
    body: ProfileDispatchBody,
    _=Depends(_auth_key_id),
):
    pool = get_extension_pool()
    if not pool.get(profile_id):
        raise HTTPException(404, "profile_not_found")
    try:
        saved = set_profile_dispatch_enabled(profile_id, body.enabled)
    except ValueError as exc:
        raise HTTPException(400, "invalid_profile_id") from exc
    from flow2api.services import system_ops

    await system_ops.push_proxy_to_extensions()
    events.publish(
        "profile_dispatch_changed",
        {"profile_id": profile_id, "enabled": body.enabled},
    )
    events.publish(
        "profile_proxy_changed",
        {"profile_id": profile_id, "dispatch": body.enabled},
    )
    return {
        **saved.to_dict(),
        "profiles": pool.list_public(),
        "ok": True,
    }


@router.put("/profiles/{profile_id}/credit")
async def update_profile_credit(
    profile_id: str,
    body: ProfileCreditBody,
    _=Depends(_auth_key_id),
):
    pool = get_extension_pool()
    if not pool.get(profile_id):
        raise HTTPException(404, "profile_not_found")
    try:
        saved = set_profile_credit_allowed(profile_id, body.allowed)
    except ValueError as exc:
        raise HTTPException(400, "invalid_profile_id") from exc
    events.publish(
        "profile_credit_changed",
        {"profile_id": profile_id, "allowed": body.allowed},
    )
    return {
        **saved.to_dict(),
        "profiles": pool.list_public(),
        "ok": True,
    }


@router.put("/profiles/{profile_id}/proxy")
async def update_profile_proxy(
    profile_id: str,
    body: ProfileProxyBody,
    _=Depends(_auth_key_id),
):
    from flow2api.services import system_ops

    pool = get_extension_pool()
    if not pool.get(profile_id):
        raise HTTPException(404, "profile_not_found")
    if not system_ops.is_proxy_pool_enabled():
        raise HTTPException(409, "proxy_pool_disabled")
    try:
        result = await system_ops.set_profile_proxy_attach_enabled(profile_id, body.enabled)
    except ValueError as exc:
        raise HTTPException(400, "invalid_profile_id") from exc
    events.publish(
        "profile_proxy_changed",
        {"profile_id": profile_id, "enabled": body.enabled},
    )
    state = "gắn IP" if body.enabled else "ngừng gắn IP"
    return {
        **result,
        "profiles": pool.list_public(),
        "message": f"Đã {state} cho profile",
        "ok": True,
    }


@router.put("/profiles/{profile_id}/media")
async def update_profile_media(
    profile_id: str,
    body: ProfileMediaBody,
    _=Depends(_auth_key_id),
):
    pool = get_extension_pool()
    if not pool.get(profile_id):
        raise HTTPException(404, "profile_not_found")
    if body.image is None and body.video is None:
        raise HTTPException(400, "missing_media_flags")
    try:
        saved = set_profile_media_allowed(
            profile_id,
            image=body.image,
            video=body.video,
        )
    except ValueError as exc:
        raise HTTPException(400, "invalid_profile_id") from exc
    events.publish(
        "profile_media_changed",
        {
            "profile_id": profile_id,
            "image": body.image,
            "video": body.video,
        },
    )
    return {
        **saved.to_dict(),
        "profiles": pool.list_public(),
        "ok": True,
    }
