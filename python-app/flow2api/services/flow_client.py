"""
Bridge tới Chrome Extension (Flow2API Bridge) — multi-profile pool.

Mỗi Chrome profile cài cùng extension, kết nối WS ws://127.0.0.1:1609.
Agent phân bổ task round-robin qua ExtensionPool.
"""
from __future__ import annotations

from typing import Any, Optional

from flow2api.services.extension_pool import ExtensionSession, get_extension_pool

# Back-compat alias
FlowClient = ExtensionSession


def get_flow_client() -> ExtensionSession:
    """Session đang bind cho worker task, hoặc profile ready đầu tiên."""
    pool = get_extension_pool()
    bound = pool.get_bound()
    if bound and bound.connected:
        return bound
    return _first_ready_or_offline(pool)


def get_flow_client_for_profile(profile_id: Optional[str] = None) -> ExtensionSession:
    """Session cho HTTP upscale — ưu tiên profile_id đã tạo ảnh (multi-account)."""
    pool = get_extension_pool()
    pid = str(profile_id or "").strip()
    if pid:
        session = pool.get(pid)
        if session and session.is_ready():
            return session
        raise RuntimeError("profile_not_ready")
    return _first_ready_or_offline(pool)


def _first_ready_or_offline(pool: Any) -> ExtensionSession:
    ready = pool.first_ready()
    if ready:
        return ready
    offline = pool.get("_offline")
    if not offline:
        offline = ExtensionSession("_offline", "Chưa có profile")
        pool._sessions["_offline"] = offline  # noqa: SLF001
    return offline


def bind_task_profile(profile_id: str) -> None:
    get_extension_pool().bind_profile(profile_id)


def unbind_task_profile() -> None:
    get_extension_pool().unbind_profile()


def pick_profile_for_task(existing_profile_id: Optional[str] = None) -> Optional[str]:
    from flow2api.services.worker_settings import (
        get_profile_max_concurrent,
        is_profile_dispatch_enabled,
    )

    pool = get_extension_pool()
    if existing_profile_id and is_profile_dispatch_enabled(existing_profile_id):
        session = pool.get(existing_profile_id)
        if session and session.is_ready():
            limit = get_profile_max_concurrent(existing_profile_id)
            if session.active_jobs < limit:
                return existing_profile_id
        return None
    return pool.pick_round_robin()


def pick_profile_for_retry(current_profile_id: str) -> Optional[str]:
    return get_extension_pool().pick_profile_for_retry(current_profile_id)


def profile_available_for_queue(params: dict[str, Any]) -> bool:
    from flow2api.services.worker_settings import is_profile_dispatch_enabled

    pid = params.get("profile_id")
    if pid and not is_profile_dispatch_enabled(str(pid)):
        return bool(pick_profile_for_task(None))
    if pid:
        return bool(pick_profile_for_task(str(pid)))
    exclude = params.get("retry_exclude_profile_id")
    if exclude:
        return bool(pick_profile_for_retry(str(exclude)))
    return bool(pick_profile_for_task(None))


def apply_retry_profile_rotation(params: dict[str, Any]) -> dict[str, Any]:
    """Advance retry to the next profile in ring order (idle first; same profile after full cycle)."""
    out = dict(params or {})
    current = str(
        out.get("profile_id") or out.get("retry_exclude_profile_id") or ""
    ).strip()
    if not current:
        out.pop("retry_exclude_profile_id", None)
        return out

    pool = get_extension_pool()
    next_id = pool.pick_profile_for_retry(current)

    if not next_id:
        out.pop("profile_id", None)
        out.pop("profile_label", None)
        out.pop("profile_email", None)
        out["retry_exclude_profile_id"] = current
        return out

    if next_id != current:
        for media_key in ("start_media_id", "end_media_id", "reference_media_ids"):
            out.pop(media_key, None)

    session = pool.get(next_id)
    out["profile_id"] = next_id
    if session:
        out["profile_label"] = session.display_name()
        if session.email:
            out["profile_email"] = session.email
        else:
            out.pop("profile_email", None)
    out.pop("retry_exclude_profile_id", None)
    return out
