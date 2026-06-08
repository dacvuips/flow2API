from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from flow2api.config import ACTIVITY_LIST_LIMIT, ACTIVITY_PAGE_SIZE
from flow2api.services import activity
from flow2api.services.result_media import with_base64_media
from flow2api.services.task_counters import reset_counters
from flow2api.services.task_retention import purge_old_requests

router = APIRouter(prefix="/api/activity", tags=["activity"])


def _bearer(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing_bearer_token")
    return authorization.split(" ", 1)[1].strip()


@router.get("")
async def list_activity(
    page: int = Query(1, ge=1),
    page_size: int = Query(ACTIVITY_PAGE_SIZE, ge=1, le=100),
    summary: bool = False,
    _: str = Depends(_bearer),
):
    purge_old_requests(ACTIVITY_LIST_LIMIT)
    total = activity.count_requests()
    offset = (page - 1) * page_size
    rows = activity.list_requests(limit=page_size, offset=offset)
    items = [
        await with_base64_media(activity.record_to_public(r), embed=False) for r in rows
    ]
    total_pages = max(1, (total + page_size - 1) // page_size) if total else 1
    payload: dict = {
        "items": items,
        "pagination": {
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "offset": offset,
        },
    }
    if summary:
        payload["summary"] = activity.summary_stats()
    return payload


@router.post("/kpi/reset")
async def reset_kpi_counters(_: str = Depends(_bearer)):
    counters = reset_counters()
    return {"ok": True, "summary": counters.to_dict()}


@router.get("/{request_id}")
async def get_activity(
    request_id: str,
    compact: bool = False,
    _: str = Depends(_bearer),
):
    row = activity.get_request(request_id)
    if not row:
        raise HTTPException(404, "not_found")
    data = await with_base64_media(activity.record_to_public(row), embed=True)
    if compact:
        return {
            "id": data["id"],
            "type": data["type"],
            "status": data["status"],
            "model": data["model"],
            "prompt": data["prompt"],
        }
    return data
