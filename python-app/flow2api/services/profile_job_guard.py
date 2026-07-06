"""Profile job dispatch guard — TRPC_401 lock + HTTP 403 cooldown."""
from __future__ import annotations

import threading
import time
from typing import Any

from flow2api.services.dashboard_events import events

_LOCK = threading.Lock()
_profile_403_cooldown_until: dict[str, float] = {}


def _pid(profile_id: str) -> str:
    return str(profile_id or "").strip()


def is_profile_trpc401_blocked(profile_id: str) -> bool:
    from flow2api.services.worker_settings import is_profile_trpc401_blocked as _blocked

    return _blocked(_pid(profile_id))


def block_profile_trpc401(profile_id: str) -> None:
    from flow2api.services.worker_settings import block_profile_trpc401 as _block

    pid = _pid(profile_id)
    if not pid or pid.startswith("_"):
        return
    _block(pid)
    events.publish(
        "profile_job_guard_changed",
        {"profile_id": pid, "reason": "trpc_401", "blocked": True},
    )


def unblock_profile_trpc401(profile_id: str) -> None:
    from flow2api.services.worker_settings import unblock_profile_trpc401 as _unblock

    pid = _pid(profile_id)
    if not pid:
        return
    _unblock(pid)
    events.publish(
        "profile_job_guard_changed",
        {"profile_id": pid, "reason": "trpc_401", "blocked": False},
    )


def get_profile_error_cooldown_sec(profile_id: str) -> int:
    from flow2api.services.worker_settings import get_profile_error_cooldown_sec

    return get_profile_error_cooldown_sec(_pid(profile_id))


def start_profile_403_cooldown(profile_id: str) -> float:
    """Pause dispatch on profile for configured seconds; return unix until."""
    pid = _pid(profile_id)
    if not pid or pid.startswith("_"):
        return 0.0
    duration = float(get_profile_error_cooldown_sec(pid))
    until = time.time() + duration
    with _LOCK:
        _profile_403_cooldown_until[pid] = until
    events.publish(
        "profile_job_guard_changed",
        {
            "profile_id": pid,
            "reason": "http_403",
            "until": until,
            "cooldown_sec": int(duration),
        },
    )
    return until


def get_403_cooldown_remaining(profile_id: str) -> int:
    pid = _pid(profile_id)
    if not pid:
        return 0
    with _LOCK:
        until = float(_profile_403_cooldown_until.get(pid) or 0)
    if until <= time.time():
        with _LOCK:
            _profile_403_cooldown_until.pop(pid, None)
        return 0
    return max(0, int(until - time.time()) + (1 if until > time.time() else 0))


def is_profile_in_403_cooldown(profile_id: str) -> bool:
    return get_403_cooldown_remaining(profile_id) > 0


def is_profile_job_dispatch_blocked(profile_id: str) -> bool:
    """True when profile must not receive new jobs (TRPC_401 lock or 403 cooldown)."""
    pid = _pid(profile_id)
    if not pid or pid.startswith("_"):
        return True
    if is_profile_trpc401_blocked(pid):
        return True
    return is_profile_in_403_cooldown(pid)


def get_profile_job_pause_public(profile_id: str) -> dict[str, Any]:
    pid = _pid(profile_id)
    if is_profile_trpc401_blocked(pid):
        return {
            "job_paused": True,
            "job_pause_reason": "trpc_401",
            "job_pause_until": None,
            "job_pause_remaining_s": None,
            "profile_error_cooldown_sec": get_profile_error_cooldown_sec(pid),
        }
    remaining = get_403_cooldown_remaining(pid)
    if remaining > 0:
        with _LOCK:
            until = float(_profile_403_cooldown_until.get(pid) or 0)
        return {
            "job_paused": True,
            "job_pause_reason": "http_403",
            "job_pause_until": until,
            "job_pause_remaining_s": remaining,
            "profile_error_cooldown_sec": get_profile_error_cooldown_sec(pid),
        }
    return {
        "job_paused": False,
        "job_pause_reason": None,
        "job_pause_until": None,
        "job_pause_remaining_s": None,
        "profile_error_cooldown_sec": get_profile_error_cooldown_sec(pid),
    }
