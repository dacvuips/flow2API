"""
Bridge tới Chrome Extension (Flow2API Bridge) — multi-profile pool.

Mỗi Chrome profile cài cùng extension, kết nối WS ws://127.0.0.1:1609.
Agent phân bổ task round-robin qua ExtensionPool.
"""
from __future__ import annotations

from typing import Optional

from flow2api.services.extension_pool import ExtensionSession, get_extension_pool

# Back-compat alias
FlowClient = ExtensionSession


def get_flow_client() -> ExtensionSession:
    """Session đang bind cho worker task, hoặc profile ready đầu tiên."""
    pool = get_extension_pool()
    bound = pool.get_bound()
    if bound and bound.connected:
        return bound
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
    from flow2api.services.worker_settings import get_profile_max_concurrent

    pool = get_extension_pool()
    if existing_profile_id:
        session = pool.get(existing_profile_id)
        if session and session.is_ready():
            limit = get_profile_max_concurrent(existing_profile_id)
            if session.active_jobs < limit:
                return existing_profile_id
        return None
    return pool.pick_round_robin()
