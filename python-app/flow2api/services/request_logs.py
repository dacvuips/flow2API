"""Server-side logging for worker/API activity (console only)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

_runtime_logger = logging.getLogger("flow2api.runtime")


def _now_iso() -> str:
    ts = datetime.now(timezone.utc)
    return ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z"


def resolve_log_email(
    request_id: str | None = None,
    profile_id: str | None = None,
    profile_email: str | None = None,
) -> str:
    if profile_email and str(profile_email).strip():
        return str(profile_email).strip()
    if request_id:
        try:
            from flow2api.services.activity import get_request

            row = get_request(request_id)
            if row:
                params = json.loads(row.params_json or "{}")
                email = str(params.get("profile_email") or "").strip()
                if email:
                    return email
                profile_id = profile_id or str(params.get("profile_id") or "").strip() or None
        except Exception:
            pass
    if profile_id:
        try:
            from flow2api.services.extension_pool import get_extension_pool

            session = get_extension_pool().get(str(profile_id))
            if session:
                email = str(session.email or "").strip()
                if email:
                    return email
                label = str(session.display_name() or "").strip()
                if label and "@" in label:
                    return label
        except Exception:
            pass
    return ""


def append_request_log(
    request_id: str | None,
    step: str,
    message: str,
    *,
    level: str = "info",
    data: Any = None,
    profile_id: str | None = None,
    profile_email: str | None = None,
) -> None:
    email = resolve_log_email(request_id, profile_id, profile_email)
    line = f"[{_now_iso()}]"
    if email:
        line += f" [{email}]"
    if request_id:
        line += f" [{request_id}]"
    line += f" {step}: {message}"
    if level == "error":
        _runtime_logger.error(line)
    elif level == "warn":
        _runtime_logger.warning(line)
    else:
        _runtime_logger.info(line)


def log_task_event(
    client: Any,
    step: str,
    message: str,
    *,
    level: str = "warn",
    request_id: str | None = None,
) -> None:
    """Log worker/SDK event with profile email from the active extension session."""
    append_request_log(
        request_id or getattr(client, "trace_request_id", None),
        step,
        message,
        level=level,
        profile_id=getattr(client, "profile_id", None),
        profile_email=getattr(client, "email", None) or None,
    )
