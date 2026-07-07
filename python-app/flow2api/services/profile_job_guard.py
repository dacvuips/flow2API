"""Profile job dispatch guard — HTTP 403 cooldown + clear busy."""
from __future__ import annotations

import threading
import time
from typing import Any

from flow2api.services.dashboard_events import events

_LOCK = threading.Lock()
_profile_403_cooldown_until: dict[str, float] = {}
_profile_clear_busy_until: dict[str, float] = {}


def _pid(profile_id: str) -> str:
    return str(profile_id or "").strip()


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


def begin_profile_clear_busy(profile_id: str, *, safety_cap_s: float = 120.0) -> None:
    pid = _pid(profile_id)
    if not pid or pid.startswith("_"):
        return
    until = time.time() + max(5.0, float(safety_cap_s))
    with _LOCK:
        prev = float(_profile_clear_busy_until.get(pid) or 0)
        _profile_clear_busy_until[pid] = max(prev, until)


def finish_profile_clear_cooldown(profile_id: str, wait_s: float) -> None:
    pid = _pid(profile_id)
    if not pid or pid.startswith("_"):
        return
    until = time.time() + max(0.0, float(wait_s))
    with _LOCK:
        _profile_clear_busy_until[pid] = until


def get_clear_busy_remaining(profile_id: str) -> int:
    pid = _pid(profile_id)
    if not pid:
        return 0
    with _LOCK:
        until = float(_profile_clear_busy_until.get(pid) or 0)
    if until <= time.time():
        with _LOCK:
            _profile_clear_busy_until.pop(pid, None)
        return 0
    return max(0, int(until - time.time()) + (1 if until > time.time() else 0))


def is_profile_clear_busy(profile_id: str) -> bool:
    return get_clear_busy_remaining(profile_id) > 0


def is_profile_job_dispatch_blocked(profile_id: str) -> bool:
    """True when profile must not receive new jobs."""
    pid = _pid(profile_id)
    if not pid or pid.startswith("_"):
        return True
    if is_profile_in_403_cooldown(pid):
        return True
    return is_profile_clear_busy(pid)


def _cooldown_min_from_sec(sec: int) -> int:
    return max(1, min(60, int(round(max(60, int(sec or 60)) / 60))))


def get_profile_job_pause_public(profile_id: str) -> dict[str, Any]:
    pid = _pid(profile_id)
    clear_remaining = get_clear_busy_remaining(pid)
    if clear_remaining > 0:
        with _LOCK:
            until = float(_profile_clear_busy_until.get(pid) or 0)
        return {
            "job_paused": True,
            "job_pause_reason": "clear_busy",
            "job_pause_until": until,
            "job_pause_remaining_s": clear_remaining,
            "job_pause_remaining_min": max(1, int((clear_remaining + 59) / 60)),
            "profile_error_cooldown_sec": get_profile_error_cooldown_sec(pid),
            "profile_error_cooldown_min": _cooldown_min_from_sec(
                get_profile_error_cooldown_sec(pid)
            ),
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
            "job_pause_remaining_min": max(1, int((remaining + 59) / 60)),
            "profile_error_cooldown_sec": get_profile_error_cooldown_sec(pid),
            "profile_error_cooldown_min": _cooldown_min_from_sec(
                get_profile_error_cooldown_sec(pid)
            ),
        }
    cd_sec = get_profile_error_cooldown_sec(pid)
    return {
        "job_paused": False,
        "job_pause_reason": None,
        "job_pause_until": None,
        "job_pause_remaining_s": None,
        "job_pause_remaining_min": None,
        "profile_error_cooldown_sec": cd_sec,
        "profile_error_cooldown_min": _cooldown_min_from_sec(cd_sec),
    }
