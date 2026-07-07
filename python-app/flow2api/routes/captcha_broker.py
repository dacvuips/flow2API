"""HTTP endpoints cho Captcha Center extension (long-poll broker).

Path prefix: /api/internal/captcha/*
Auth: X-Center-Secret header — secret sinh 1 lần ở storage/captcha-center.secret.
      (Same-origin loopback, không expose ra Cloudflare tunnel.)

Endpoints:
- GET  /api/internal/captcha/poll?centerId=X&label=Y&timeout=25
- POST /api/internal/captcha/result   { commandId, token | error, centerId }
- POST /api/internal/captcha/event    { centerId, type, ... }
- GET  /api/internal/captcha/stats
- GET  /api/internal/captcha/secret   (chỉ trả cho localhost — để extension đọc)
"""
from __future__ import annotations

import ipaddress
import logging

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from flow2api.services.captcha_broker import (
    DEFAULT_REQUEST_TIMEOUT_S,
    POLL_TIMEOUT_S,
    get_captcha_broker,
)

router = APIRouter(prefix="/api/internal/captcha", tags=["captcha-broker"])
logger = logging.getLogger(__name__)


def _require_secret(x_center_secret: str | None) -> None:
    broker = get_captcha_broker()
    if not broker.check_secret(x_center_secret):
        raise HTTPException(401, "invalid_center_secret")


def _is_loopback(request: Request) -> bool:
    try:
        client_host = (request.client.host if request.client else "") or ""
        if not client_host:
            return False
        # Strip IPv6 brackets/scope
        client_host = client_host.split("%", 1)[0].strip("[]")
        ip = ipaddress.ip_address(client_host)
        return ip.is_loopback
    except Exception:
        return False


@router.get("/secret")
async def get_secret(request: Request):
    """Trả secret CHỈ cho loopback — dùng cho extension tự-config ở localhost."""
    if not _is_loopback(request):
        raise HTTPException(403, "loopback_only")
    broker = get_captcha_broker()
    return {"secret": broker.secret}


class ResultBody(BaseModel):
    commandId: str
    centerId: str | None = None
    token: str | None = None
    error: str | None = None


class EventBody(BaseModel):
    centerId: str
    type: str
    label: str | None = None
    version: str | None = None
    payload: dict | None = None


@router.get("/poll")
async def poll(
    request: Request,
    centerId: str = Query(..., min_length=1, max_length=128),
    label: str = Query("", max_length=200),
    version: str = Query("", max_length=32),
    timeout: float = Query(POLL_TIMEOUT_S, ge=1.0, le=60.0),
    x_center_secret: str | None = Header(default=None, alias="X-Center-Secret"),
):
    _require_secret(x_center_secret)
    broker = get_captcha_broker()
    commands = await broker.poll(centerId, label=label, version=version, timeout=timeout)
    return {"commands": commands}


@router.post("/result")
async def submit_result(
    body: ResultBody,
    x_center_secret: str | None = Header(default=None, alias="X-Center-Secret"),
):
    _require_secret(x_center_secret)
    broker = get_captcha_broker()
    ok = broker.submit_result(
        command_id=body.commandId,
        token=body.token,
        error=body.error,
        center_id=body.centerId,
    )
    return {"ok": ok}


@router.post("/event")
async def submit_event(
    body: EventBody,
    x_center_secret: str | None = Header(default=None, alias="X-Center-Secret"),
):
    _require_secret(x_center_secret)
    broker = get_captcha_broker()
    payload = dict(body.payload or {})
    if body.label:
        payload.setdefault("label", body.label)
    if body.version:
        payload.setdefault("version", body.version)
    broker.submit_event(body.centerId, body.type, payload)
    return {"ok": True}


@router.get("/stats")
async def stats():
    """Public stats (không cần secret) — dùng cho dashboard/popup."""
    broker = get_captcha_broker()
    return broker.stats()


class HardResetBody(BaseModel):
    bridgeProfileId: str = ""
    reason: str = Field("manual", max_length=64)


@router.post("/hard-reset")
async def request_hard_reset(
    body: HardResetBody,
    x_center_secret: str | None = Header(default=None, alias="X-Center-Secret"),
):
    """Bridge/worker báo API 403 → yêu cầu 1 center hard_reset."""
    _require_secret(x_center_secret)
    broker = get_captcha_broker()
    picked = broker.request_hard_reset(bridge_profile_id=body.bridgeProfileId, reason=body.reason)
    return {"ok": bool(picked), "centerId": picked}
