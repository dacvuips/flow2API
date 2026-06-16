"""Server-side logging for worker/API activity (console only)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

_runtime_logger = logging.getLogger("flow2api.runtime")


def _now_iso() -> str:
    ts = datetime.now(timezone.utc)
    return ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z"


def append_request_log(
    request_id: str | None,
    step: str,
    message: str,
    *,
    level: str = "info",
    data: Any = None,
) -> None:
    line = f"[{_now_iso()}]"
    if request_id:
        line += f" [{request_id}]"
    line += f" {step}: {message}"
    if level == "error":
        _runtime_logger.error(line)
    elif level == "warn":
        _runtime_logger.warning(line)
    else:
        _runtime_logger.info(line)
