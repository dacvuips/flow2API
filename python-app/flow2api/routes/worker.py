from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request
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


@router.get("/profiles/{profile_id}/auth-status")
async def profile_auth_status(profile_id: str, _=Depends(_auth_key_id)):
    from flow2api.services.flow_profile_service import profile_auth_status as auth_status

    pool = get_extension_pool()
    session = pool.get(profile_id)
    if not session:
        pool.hydrate_db_profiles()
        session = pool.get(profile_id)
    if not session:
        return auth_status(profile_id, live_flow_key_present=False, extension_online=False)
    return auth_status(
        profile_id,
        live_flow_key_present=session.browser_flow_key_present,
        extension_online=session.connected,
    )


@router.post("/profiles/{profile_id}/test-connection")
async def profile_test_connection(profile_id: str, _=Depends(_auth_key_id)):
    from flow2api.services.cookie_service import has_stored_cookies

    pool = get_extension_pool()
    session = pool.get(profile_id)
    if not session:
        pool.hydrate_db_profiles()
        session = pool.get(profile_id)
    if not session:
        raise HTTPException(404, "profile_not_found")
    if not session.connected and not has_stored_cookies(profile_id):
        raise HTTPException(400, "extension_offline")
    result = await session.test_connection()
    if not result.get("success"):
        raise HTTPException(400, str(result.get("error") or "TOKEN_REFRESH_FAILED"))
    return {
        **result,
        "profiles": pool.list_public(),
        "ok": True,
    }


@router.post("/profiles/{profile_id}/open-flow")
async def profile_open_flow(profile_id: str, _=Depends(_auth_key_id)):
    pool = get_extension_pool()
    session = pool.get(profile_id)
    if not session:
        raise HTTPException(404, "profile_not_found")
    if not session.connected:
        raise HTTPException(400, "extension_offline")
    result = await session.open_flow_tab()
    if not result.get("ok"):
        raise HTTPException(400, str(result.get("error") or "OPEN_FLOW_TAB_FAILED"))
    return {**result, "ok": True}


@router.post("/profiles/{profile_id}/refresh-token")
async def profile_refresh_token(profile_id: str, _=Depends(_auth_key_id)):
    from flow2api.services.cookie_service import has_stored_cookies

    pool = get_extension_pool()
    session = pool.get(profile_id)
    if not session:
        pool.hydrate_db_profiles()
        session = pool.get(profile_id)
    if not session:
        raise HTTPException(404, "profile_not_found")
    if not session.connected and not has_stored_cookies(profile_id):
        raise HTTPException(400, "extension_offline")
    result = await session.refresh_flow_token(force=True)
    if not result.get("ok"):
        raise HTTPException(400, str(result.get("error") or "TOKEN_REFRESH_FAILED"))
    meta = session.to_public_dict()
    return {
        "success": True,
        "message": "Access token refreshed successfully!",
        "expires_at": meta.get("access_token_expires_at"),
        "profiles": pool.list_public(),
        "ok": True,
    }


@router.get("/profiles/sessions/export")
async def export_profile_sessions(_=Depends(_auth_key_id)):
    """Export all profile tokens/cookies (+ expiry) as JSON backup."""
    from flow2api.services.flow_profile_service import export_all_profile_sessions

    return export_all_profile_sessions()


@router.get("/profiles/{profile_id}/session/export")
async def export_one_profile_session(profile_id: str, _=Depends(_auth_key_id)):
    from flow2api.services.flow_profile_service import export_profile_session

    data = export_profile_session(profile_id)
    if not data:
        raise HTTPException(404, "profile_not_found")
    return data


@router.post("/profiles/sessions/import")
async def import_profile_sessions_route(request: Request, _=Depends(_auth_key_id)):
    """Import one or many profile sessions (token/cookies/expiry)."""
    from flow2api.services.flow_profile_service import import_profile_sessions

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "invalid_json")
    result = import_profile_sessions(payload)
    pool = get_extension_pool()
    pool.hydrate_db_profiles()
    return {**result, "profiles": pool.list_public()}


@router.post("/profiles/{profile_id}/session/import")
async def import_one_profile_session(
    profile_id: str, body: dict = Body(default_factory=dict), _=Depends(_auth_key_id)
):
    from flow2api.services.flow_profile_service import import_profile_session

    if not isinstance(body, dict):
        raise HTTPException(400, "invalid_payload")
    result = import_profile_session(body, target_profile_id=profile_id)
    if not result.get("ok"):
        raise HTTPException(400, str(result.get("error") or "import_failed"))
    pool = get_extension_pool()
    pool.hydrate_db_profiles()
    return {**result, "profiles": pool.list_public()}

