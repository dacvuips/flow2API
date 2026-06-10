from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from flow2api.config import ACTIVITY_LIST_LIMIT
from flow2api.db.models import RequestRecord, SessionLocal
from flow2api.services import task_counters


def create_request(
    rid: str,
    req_type: str,
    prompt: str,
    model: str,
    params: dict,
    api_key_id: Optional[int] = None,
) -> RequestRecord:
    db = SessionLocal()
    try:
        row = RequestRecord(
            id=rid,
            type=req_type,
            status="queued",
            prompt=prompt,
            model=model,
            params_json=json.dumps(params, ensure_ascii=False),
            api_key_id=api_key_id,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        task_counters.increment_total()
        return row
    finally:
        db.close()


def requeue_request(rid: str, params: dict[str, Any]) -> Optional[RequestRecord]:
    """Reset an existing request to queued (same id) for manual retry."""
    db = SessionLocal()
    try:
        row = db.get(RequestRecord, rid)
        if not row:
            return None
        old_status = row.status
        row.status = "queued"
        row.params_json = json.dumps(params, ensure_ascii=False)
        row.result_json = "{}"
        row.error = None
        row.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(row)
        task_counters.on_status_transition(old_status, "queued")
        return row
    finally:
        db.close()


def update_request(rid: str, **fields: Any) -> None:
    db = SessionLocal()
    try:
        row = db.get(RequestRecord, rid)
        if not row:
            return
        old_status = row.status
        for k, v in fields.items():
            if k == "result" and isinstance(v, dict):
                row.result_json = json.dumps(v, ensure_ascii=False)
            elif k == "params" and isinstance(v, dict):
                row.params_json = json.dumps(v, ensure_ascii=False)
            elif hasattr(row, k):
                setattr(row, k, v)
        row.updated_at = datetime.utcnow()
        db.commit()
        if "status" in fields:
            task_counters.on_status_transition(old_status, row.status)
    finally:
        db.close()


def get_request(rid: str) -> Optional[RequestRecord]:
    db = SessionLocal()
    try:
        return db.get(RequestRecord, rid)
    finally:
        db.close()


def count_requests() -> int:
    db = SessionLocal()
    try:
        return db.query(RequestRecord).count()
    finally:
        db.close()


def list_requests(
    limit: int = ACTIVITY_LIST_LIMIT,
    offset: int = 0,
) -> list[RequestRecord]:
    db = SessionLocal()
    try:
        q = db.query(RequestRecord).order_by(RequestRecord.created_at.desc())
        if offset > 0:
            q = q.offset(offset)
        return q.limit(limit).all()
    finally:
        db.close()


def next_queued() -> Optional[RequestRecord]:
    batch = next_queued_batch(1)
    return batch[0] if batch else None


def next_queued_batch(limit: int = 1) -> list[RequestRecord]:
    db = SessionLocal()
    try:
        n = max(1, min(int(limit or 1), 32))
        return (
            db.query(RequestRecord)
            .filter(RequestRecord.status == "queued")
            .order_by(RequestRecord.created_at.asc())
            .limit(n)
            .all()
        )
    finally:
        db.close()


def count_running() -> int:
    db = SessionLocal()
    try:
        return (
            db.query(RequestRecord)
            .filter(RequestRecord.status == "running")
            .count()
        )
    finally:
        db.close()


def list_running_requests() -> list[RequestRecord]:
    db = SessionLocal()
    try:
        return (
            db.query(RequestRecord)
            .filter(RequestRecord.status == "running")
            .order_by(RequestRecord.created_at.asc())
            .all()
        )
    finally:
        db.close()


def running_age_seconds(row: RequestRecord) -> float:
    params = json.loads(row.params_json or "{}")
    raw = params.get("running_started_at")
    if raw:
        try:
            text = str(raw).strip().replace("Z", "")
            started = datetime.fromisoformat(text)
            return max(0.0, (datetime.utcnow() - started).total_seconds())
        except (TypeError, ValueError):
            pass
    return max(0.0, (datetime.utcnow() - row.updated_at).total_seconds())


def count_queued() -> int:
    db = SessionLocal()
    try:
        return (
            db.query(RequestRecord)
            .filter(RequestRecord.status == "queued")
            .count()
        )
    finally:
        db.close()


_LIST_HEAVY_PARAM_KEYS = frozenset(
    {
        "image_base64",
        "imageBase64",
        "image_base64s",
        "imageBase64s",
        "video_base64",
        "videoBase64",
        "encoded_video",
        "encodedVideo",
        "raw",
        "raw_dispatch",
        "last_poll",
    }
)


def strip_heavy_params(params: dict[str, Any]) -> dict[str, Any]:
    """Remove large blobs from persisted params (after upload or for API list)."""
    if not any(k in params for k in _LIST_HEAVY_PARAM_KEYS):
        return params
    return slim_params_for_list(params)


def slim_params_for_list(params: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in params.items() if k not in _LIST_HEAVY_PARAM_KEYS}
    if params.get("image_base64s"):
        out["image_base64s_count"] = len(params["image_base64s"])
    elif params.get("imageBase64s"):
        out["image_base64s_count"] = len(params["imageBase64s"])
    return out


def maybe_strip_heavy_params(rid: str) -> None:
    row = get_request(rid)
    if not row:
        return
    params = json.loads(row.params_json or "{}")
    if not any(k in params for k in _LIST_HEAVY_PARAM_KEYS):
        return
    update_request(rid, params=strip_heavy_params(params))


_STATUS_FILTERS = frozenset({"queued", "running", "done", "failed", "active"})


def parse_status_filter(value: str | None) -> str | None:
    raw = (value or "").strip().lower()
    if not raw or raw == "all":
        return None
    if raw not in _STATUS_FILTERS:
        raise ValueError(raw)
    return raw


def _apply_status_filter(query, status_filter: str | None):
    if not status_filter:
        return query
    if status_filter == "queued":
        return query.filter(RequestRecord.status == "queued")
    if status_filter == "running":
        return query.filter(RequestRecord.status == "running")
    if status_filter == "done":
        return query.filter(RequestRecord.status == "done")
    if status_filter == "failed":
        return query.filter(RequestRecord.status.like("failed%"))
    if status_filter == "active":
        return query.filter(RequestRecord.status.in_(("queued", "running")))
    return query


def fetch_list_page(
    page: int,
    page_size: int,
    status_filter: str | None = None,
) -> tuple[int, list[RequestRecord]]:
    db = SessionLocal()
    try:
        q = db.query(RequestRecord)
        q = _apply_status_filter(q, status_filter)
        total = q.count()
        offset = max(0, (page - 1) * page_size)
        rows = (
            q.order_by(RequestRecord.created_at.desc())
            .offset(offset)
            .limit(page_size)
            .all()
        )
        return total, rows
    finally:
        db.close()


def summary_stats(*, list_rows: list[RequestRecord] | None = None) -> dict[str, int]:
    c = task_counters.get_counters()
    if list_rows is not None:
        running = sum(1 for r in list_rows if r.status in ("queued", "running"))
    else:
        running = count_queued() + count_running()
    return {"total": c.total, "done": c.done, "failed": c.failed, "running": running}


def record_to_public(row: RequestRecord, *, for_list: bool = False) -> dict:
    result = json.loads(row.result_json or "{}")
    params = json.loads(row.params_json or "{}")
    profile_label = (
        params.get("profile_label")
        or params.get("profile_email")
        or params.get("profile_id")
        or ""
    )
    if for_list:
        from flow2api.services.result_media import (
            input_preview_items_from_params,
            preview_items_from_result,
            slim_result_for_list,
        )

        preview_items = preview_items_from_result(result, row.type)
        input_preview_items = input_preview_items_from_params(params)
        params = slim_params_for_list(params)
        result = slim_result_for_list(result)
    else:
        preview_items = []
        input_preview_items = []
    payload = {
        "id": row.id,
        "type": row.type,
        "status": row.status,
        "prompt": row.prompt,
        "model": row.model,
        "profile_id": params.get("profile_id"),
        "profile_label": profile_label,
        "profile_email": params.get("profile_email"),
        "params": params,
        "result": result,
        "error": row.error,
        "created_at": row.created_at.isoformat() + "Z",
        "updated_at": row.updated_at.isoformat() + "Z",
    }
    if for_list:
        payload["preview_items"] = preview_items
        payload["output_count"] = len(preview_items)
        payload["input_preview_items"] = input_preview_items
        payload["input_count"] = len(input_preview_items)
    return payload
