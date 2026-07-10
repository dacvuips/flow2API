from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException, Query

from flow2api.config import (
    ACTIVITY_LIST_LIMIT,
    ACTIVITY_META_LIMIT,
    ACTIVITY_PAGE_SIZE,
    HTTP_HANDLER_TIMEOUT_S,
    PURGE_INTERVAL_S,
)
from flow2api.services import activity
from flow2api.services.api_auth import auth_key_id
from flow2api.services.result_media import with_base64_media
from flow2api.services.task_counters import reset_counters
from flow2api.services.task_retention import purge_storage

router = APIRouter(prefix="/api/activity", tags=["activity"])

_last_purge_monotonic = 0.0


def _maybe_schedule_purge() -> None:
    global _last_purge_monotonic
    interval = max(60.0, float(PURGE_INTERVAL_S or 300))
    now = time.monotonic()
    if now - _last_purge_monotonic < interval:
        return
    _last_purge_monotonic = now
    asyncio.create_task(asyncio.to_thread(purge_storage, ACTIVITY_LIST_LIMIT, ACTIVITY_META_LIMIT))


async def _list_activity_payload(
    page: int,
    page_size: int,
    summary: bool,
    status_filter: str | None = None,
) -> dict:
    _maybe_schedule_purge()
    total, rows = await asyncio.to_thread(
        activity.fetch_list_page, page, page_size, status_filter
    )
    offset = (page - 1) * page_size
    items = [activity.record_to_public(r, for_list=True) for r in rows]
    total_pages = max(1, (total + page_size - 1) // page_size) if total else 1
    payload: dict = {
        "items": items,
        "pagination": {
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "offset": offset,
            "status_filter": status_filter or "all",
        },
    }
    if summary:
        payload["summary"] = await asyncio.to_thread(activity.summary_stats)
    return payload


@router.get("")
async def list_activity(
    page: int = Query(1, ge=1),
    page_size: int = Query(ACTIVITY_PAGE_SIZE, ge=1, le=100),
    summary: bool = False,
    status: str | None = Query(None, description="all|queued|running|done|failed|active"),
    _: int = Depends(auth_key_id),
):
    try:
        status_filter = activity.parse_status_filter(status)
    except ValueError:
        raise HTTPException(400, "invalid_status_filter") from None
    timeout = max(5.0, float(HTTP_HANDLER_TIMEOUT_S or 25))
    try:
        return await asyncio.wait_for(
            _list_activity_payload(page, page_size, summary, status_filter),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        raise HTTPException(503, "activity_list_timeout") from None


@router.post("/kpi/reset")
async def reset_kpi_counters(_: int = Depends(auth_key_id)):
    counters = reset_counters()
    return {"ok": True, "summary": counters.to_dict()}


@router.get("/{request_id}")
async def get_activity(
    request_id: str,
    compact: bool = False,
    _: int = Depends(auth_key_id),
):
    timeout = max(10.0, float(HTTP_HANDLER_TIMEOUT_S or 25) * 2)

    async def _load() -> dict:
        row = await asyncio.to_thread(activity.get_request, request_id)
        if not row:
            raise HTTPException(404, "not_found")
        return await with_base64_media(
            activity.record_to_public(row, include_preview=True),
            embed=False,
        )

    try:
        data = await asyncio.wait_for(_load(), timeout=timeout)
    except asyncio.TimeoutError:
        raise HTTPException(503, "activity_detail_timeout") from None
    if compact:
        return {
            "id": data["id"],
            "type": data["type"],
            "status": data["status"],
            "model": data["model"],
            "prompt": data["prompt"],
        }
    return data
