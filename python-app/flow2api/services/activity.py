from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from flow2api.db.models import RequestRecord, SessionLocal


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
        return row
    finally:
        db.close()


def update_request(rid: str, **fields: Any) -> None:
    db = SessionLocal()
    try:
        row = db.get(RequestRecord, rid)
        if not row:
            return
        for k, v in fields.items():
            if k == "result" and isinstance(v, dict):
                row.result_json = json.dumps(v, ensure_ascii=False)
            elif k == "params" and isinstance(v, dict):
                row.params_json = json.dumps(v, ensure_ascii=False)
            elif hasattr(row, k):
                setattr(row, k, v)
        row.updated_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()


def get_request(rid: str) -> Optional[RequestRecord]:
    db = SessionLocal()
    try:
        return db.get(RequestRecord, rid)
    finally:
        db.close()


def list_requests(limit: int = 50) -> list[RequestRecord]:
    db = SessionLocal()
    try:
        return db.query(RequestRecord).order_by(RequestRecord.created_at.desc()).limit(limit).all()
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


def summary_stats() -> dict[str, int]:
    db = SessionLocal()
    try:
        rows = db.query(RequestRecord).all()
        total = len(rows)
        done = sum(1 for r in rows if r.status == "done")
        failed = sum(1 for r in rows if r.status.startswith("failed"))
        running = sum(1 for r in rows if r.status in ("running", "queued"))
        return {"total": total, "done": done, "failed": failed, "running": running}
    finally:
        db.close()


def record_to_public(row: RequestRecord) -> dict:
    result = json.loads(row.result_json or "{}")
    params = json.loads(row.params_json or "{}")
    profile_label = (
        params.get("profile_label")
        or params.get("profile_email")
        or params.get("profile_id")
        or ""
    )
    return {
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
        "logs": json.loads(row.logs_json or "[]"),
        "created_at": row.created_at.isoformat() + "Z",
        "updated_at": row.updated_at.isoformat() + "Z",
    }
