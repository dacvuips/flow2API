"""Capture Google Flow API calls per worker request (keyed by request id)."""
from __future__ import annotations

from typing import Any

_traces: dict[str, list[dict[str, Any]]] = {}


def begin_api_trace(request_id: str) -> None:
    _traces[request_id] = []


def end_api_trace(request_id: str) -> list[dict[str, Any]]:
    return _traces.pop(request_id, [])


def trace_count(request_id: str) -> int:
    return len(_traces.get(request_id) or [])


def _compact_body(body: Any) -> Any:
    if not isinstance(body, dict):
        return body
    out: dict[str, Any] = {}
    for k, v in body.items():
        if k in ("imageBytes", "image_base64", "imageBase64s", "image_base64s"):
            out[k] = f"[omitted: {len(v) if isinstance(v, (str, list)) else type(v).__name__}]"
        elif isinstance(v, str) and len(v) > 500:
            out[k] = v[:500] + f"... [{len(v)} chars]"
        elif isinstance(v, dict):
            out[k] = _compact_body(v)
        elif isinstance(v, list) and len(v) > 8:
            out[k] = [_compact_body(x) if isinstance(x, dict) else x for x in v[:8]] + [
                f"... +{len(v) - 8} more"
            ]
        else:
            out[k] = v
    return out


def _label_from_url(url: str, method: str) -> str:
    u = url.lower()
    if "batchasyncgeneratevideotext" in u:
        return "video_submit_t2v"
    if "batchasyncgeneratevideostart" in u:
        return "video_submit_i2v"
    if "batchasyncgeneratevideoreference" in u:
        return "video_submit_r2v"
    if "batchcheckasync" in u:
        return "video_poll"
    if "/media/" in u and method.upper() == "GET":
        return "get_media"
    if "batchgenerateimages" in u:
        return "gen_image"
    if "uploadimage" in u:
        return "upload_image"
    if "/credits" in u:
        return "credits"
    return method.lower() or "api"


def record_api_call(request_id: str | None, url: str, method: str, body: Any, resp: dict) -> None:
    if not request_id or request_id not in _traces:
        return
    if "aisandbox-pa.googleapis.com" not in url:
        return

    from flow2api.services.flow_sdk import compact_api_response

    label = _label_from_url(url, method)
    entry: dict[str, Any] = {
        "label": label,
        "url": url.split("?", 1)[0],
        "method": method.upper(),
        "request_body": _compact_body(body) if method.upper() != "GET" else None,
        **compact_api_response(resp, label),
    }
    buf = _traces[request_id]
    buf.append(entry)
    if len(buf) > 30:
        del buf[:-30]

    from flow2api.services.request_logs import append_request_log

    status = int(resp.get("status") or 0)
    if label == "get_media" and status in (404, 500):
        level = "warn"
    else:
        level = "error" if status >= 400 or resp.get("error") else "info"
    msg = f"{label} {method} → {status}"
    if resp.get("error"):
        msg += f" — {resp.get('error')}"
    append_request_log(request_id, label, msg, level=level, data=entry)
