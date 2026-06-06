"""Step logs disabled — kept as no-op stubs for backward imports."""
from __future__ import annotations

from typing import Any, Optional


def append_request_log(
    request_id: str,
    step: str,
    message: str,
    *,
    level: str = "info",
    data: Any = None,
) -> None:
    return None


def get_request_logs(request_id: str, limit: int = 200) -> list[dict[str, Any]]:
    return []


def list_global_logs(limit: int = 150, request_id: Optional[str] = None) -> list[dict[str, Any]]:
    return []


def list_logs_from_db(limit: int = 200, request_id: Optional[str] = None) -> list[dict[str, Any]]:
    return []


def list_combined_logs(limit: int = 200, request_id: Optional[str] = None) -> list[dict[str, Any]]:
    return []
