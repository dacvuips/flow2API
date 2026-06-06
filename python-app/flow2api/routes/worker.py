from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from flow2api.services import activity
from flow2api.services.auth_keys import get_api_key_by_token
from flow2api.services.extension_pool import get_extension_pool
from flow2api.services.worker_settings import (
    get_worker_settings,
    save_profile_limit,
    save_worker_settings,
)
from flow2api.worker.processor import get_worker

router = APIRouter(prefix="/api/worker", tags=["worker"])


class WorkerSettingsBody(BaseModel):
    max_concurrent: int | None = Field(None, ge=1, le=32)
    task_stagger_s: float | None = Field(None, ge=0.0, le=300.0)
    profile_default_max_concurrent: int | None = Field(None, ge=1, le=8)


class ProfileLimitBody(BaseModel):
    profile_id: str
    max_concurrent: int = Field(1, ge=1, le=8)


class ProfileLimitsBulkBody(BaseModel):
    profile_limits: dict[str, int] = Field(default_factory=dict)
    profile_default_max_concurrent: int | None = Field(None, ge=1, le=8)


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
    return {**saved.to_dict(), "ok": True}


@router.put("/profile-limits")
async def update_profile_limits(body: ProfileLimitsBulkBody, _=Depends(_auth_key_id)):
    fields: dict[str, Any] = {"profile_limits": body.profile_limits}
    if body.profile_default_max_concurrent is not None:
        fields["profile_default_max_concurrent"] = body.profile_default_max_concurrent
    saved = save_worker_settings(**fields)
    return {**saved.to_dict(), "profiles": get_extension_pool().list_public(), "ok": True}


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

