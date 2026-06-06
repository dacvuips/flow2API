from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from flow2api.services import activity
from flow2api.services.result_media import with_base64_media

router = APIRouter(prefix="/api/activity", tags=["activity"])


def _bearer(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing_bearer_token")
    return authorization.split(" ", 1)[1].strip()


@router.get("")
async def list_activity(
    limit: int = Query(20, ge=1, le=100),
    summary: bool = False,
    _: str = Depends(_bearer),
):
    rows = activity.list_requests(limit=limit)
    items = [
        await with_base64_media(activity.record_to_public(r), embed=False) for r in rows
    ]
    if summary:
        stats = activity.summary_stats()
        return {"items": items, "summary": stats}
    return {"items": items}


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
