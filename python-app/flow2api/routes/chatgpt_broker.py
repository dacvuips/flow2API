"""HTTP endpoints — extension long-poll nhận job ChatGPT (không cần Bridge WS).

Path: /api/internal/chatgpt/*
Chỉ loopback (127.0.0.1 / ::1).
"""
from __future__ import annotations

import ipaddress
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from flow2api.services.chatgpt_broker import POLL_TIMEOUT_S, get_chatgpt_broker

router = APIRouter(prefix="/api/internal/chatgpt", tags=["chatgpt-broker"])
logger = logging.getLogger(__name__)


def _is_loopback(request: Request) -> bool:
    try:
        client_host = (request.client.host if request.client else "") or ""
        if not client_host:
            return False
        client_host = client_host.split("%", 1)[0].strip("[]")
        return ipaddress.ip_address(client_host).is_loopback
    except Exception:
        return False


def _require_loopback(request: Request) -> None:
    if not _is_loopback(request):
        raise HTTPException(403, "loopback_only")


class ResultBody(BaseModel):
    jobId: str = Field(min_length=1)
    result: dict[str, Any] | None = None
    error: str | None = None


@router.get("/poll")
async def poll(
    request: Request,
    workerId: str = Query(..., min_length=1, max_length=128),
    label: str = Query("", max_length=200),
    timeout: float = Query(POLL_TIMEOUT_S, ge=1.0, le=60.0),
):
    _require_loopback(request)
    broker = get_chatgpt_broker()
    jobs = await broker.poll(workerId, label=label, timeout=timeout)
    return {"jobs": jobs}


@router.post("/result")
async def submit_result(request: Request, body: ResultBody):
    _require_loopback(request)
    broker = get_chatgpt_broker()
    ok = broker.complete(body.jobId, result=body.result, error=body.error)
    if not ok:
        raise HTTPException(404, "job_not_found")
    return {"ok": True}


@router.get("/stats")
async def stats(request: Request):
    _require_loopback(request)
    return {"ok": True, **get_chatgpt_broker().stats()}
