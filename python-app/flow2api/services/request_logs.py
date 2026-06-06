"""Runtime command/response logs — persisted per request + global ring buffer + SSE."""
from __future__ import annotations

import json
import logging
from collections import deque
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Optional

from flow2api.db.models import RequestRecord, SessionLocal
from flow2api.services.dashboard_events import events

_MAX_GLOBAL = 600
_MAX_PER_REQUEST = 250

_global_logs: deque[dict[str, Any]] = deque(maxlen=_MAX_GLOBAL)
_lock = Lock()
_runtime_logger = logging.getLogger("flow2api.runtime")


def _now_iso() -> str:
    ts = datetime.now(timezone.utc)
    return ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z"


def _compact_data(data: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "[nested]"
    if isinstance(data, str):
        if data.startswith("data:image/") or data.startswith("data:video/"):
            return f"[omitted data url: {len(data)} chars]"
        if len(data) > 800:
            return data[:800] + f"... [{len(data)} chars]"
        return data
    if isinstance(data, dict):
        heavy = {"imageBytes", "image_base64", "imageBase64", "image_base64s", "imageBase64s", "encoded_video", "encodedVideo"}
        out: dict[str, Any] = {}
        for k, v in data.items():
            if k in heavy:
                out[k] = f"[omitted: {type(v).__name__}]"
            else:
                out[k] = _compact_data(v, depth + 1)
        return out
    if isinstance(data, list):
        if len(data) > 12:
            return [_compact_data(x, depth + 1) for x in data[:12]] + [f"... +{len(data) - 12} more"]
        return [_compact_data(x, depth + 1) for x in data]
    return data


def append_request_log(
    request_id: str | None,
    step: str,
    message: str,
    *,
    level: str = "info",
    data: Any = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "ts": _now_iso(),
        "request_id": request_id or None,
        "step": step,
        "level": level,
        "message": message,
    }
    if data is not None:
        entry["data"] = _compact_data(data)

    with _lock:
        _global_logs.append(entry)

    if request_id:
        _persist_request_log(request_id, entry)

    try:
        events.publish("runtime_log", entry)
    except Exception:
        pass

    line = f"[{entry['ts']}]"
    if request_id:
        line += f" [{request_id[:8]}]"
    line += f" {step}: {message}"
    if level == "error":
        _runtime_logger.error(line)
    elif level == "warn":
        _runtime_logger.warning(line)
    else:
        _runtime_logger.info(line)

    return entry


def _persist_request_log(request_id: str, entry: dict[str, Any]) -> None:
    db = SessionLocal()
    try:
        row = db.get(RequestRecord, request_id)
        if not row:
            return
        logs = json.loads(row.logs_json or "[]")
        logs.append(entry)
        if len(logs) > _MAX_PER_REQUEST:
            logs = logs[-_MAX_PER_REQUEST:]
        row.logs_json = json.dumps(logs, ensure_ascii=False)
        db.commit()
    except Exception as exc:
        _runtime_logger.warning("persist log failed rid=%s: %s", request_id[:8], exc)
    finally:
        db.close()


def get_request_logs(request_id: str, limit: int = 200) -> list[dict[str, Any]]:
    db = SessionLocal()
    try:
        row = db.get(RequestRecord, request_id)
        if not row:
            return []
        logs = json.loads(row.logs_json or "[]")
        return logs[-max(1, min(limit, _MAX_PER_REQUEST)) :]
    finally:
        db.close()


def list_global_logs(limit: int = 150, request_id: Optional[str] = None) -> list[dict[str, Any]]:
    limit = max(1, min(limit, _MAX_GLOBAL))
    with _lock:
        items = list(_global_logs)
    if request_id:
        items = [x for x in items if x.get("request_id") == request_id]
    return items[-limit:]


def list_logs_from_db(limit: int = 200, request_id: Optional[str] = None) -> list[dict[str, Any]]:
    db = SessionLocal()
    try:
        q = db.query(RequestRecord).order_by(RequestRecord.updated_at.desc())
        if request_id:
            q = q.filter(RequestRecord.id == request_id)
        rows = q.limit(30).all()
        out: list[dict[str, Any]] = []
        for row in rows:
            logs = json.loads(row.logs_json or "[]")
            out.extend(logs)
        out.sort(key=lambda x: x.get("ts") or "")
        return out[-max(1, min(limit, _MAX_PER_REQUEST)) :]
    finally:
        db.close()


def list_combined_logs(limit: int = 200, request_id: Optional[str] = None) -> list[dict[str, Any]]:
    mem = list_global_logs(limit=limit, request_id=request_id)
    if len(mem) >= limit:
        return mem
    db_logs = list_logs_from_db(limit=limit, request_id=request_id)
    seen = {json.dumps(x, sort_keys=True, ensure_ascii=False) for x in mem}
    merged = list(mem)
    for item in db_logs:
        key = json.dumps(item, sort_keys=True, ensure_ascii=False)
        if key not in seen:
            merged.append(item)
            seen.add(key)
    merged.sort(key=lambda x: x.get("ts") or "")
    return merged[-limit:]
