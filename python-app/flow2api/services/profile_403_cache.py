"""Auto-pause profile dispatch on HTTP 403 for a configurable cooldown."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from flow2api.services.dashboard_events import events
from flow2api.services.worker_settings import (
    clear_profile_403_pause,
    get_profile_403_pause_until,
    is_profile_403_cache_enabled,
    set_profile_403_pause_until,
    set_profile_dispatch_enabled,
)

logger = logging.getLogger(__name__)

_LOCK = asyncio.Lock()
_RESUME_TASKS: dict[str, asyncio.Task] = {}
_AUTO_PAUSED: set[str] = set()


def _seconds_until_resume(profile_id: str) -> int | None:
    until = get_profile_403_pause_until(profile_id)
    if until is None:
        return None
    remaining = int(until - time.time())
    return max(0, remaining)


def profile_403_cache_public(profile_id: str) -> dict[str, Any]:
    pid = str(profile_id or "").strip()
    enabled = is_profile_403_cache_enabled(pid)
    paused = pid in _AUTO_PAUSED or _seconds_until_resume(pid) is not None
    remaining = _seconds_until_resume(pid)
    from flow2api.services.worker_settings import get_profile_403_cache_minutes

    return {
        "cache_403_enabled": enabled,
        "cache_403_minutes": get_profile_403_cache_minutes(pid),
        "cache_403_paused": bool(paused and remaining and remaining > 0),
        "cache_403_seconds_until_resume": remaining if remaining and remaining > 0 else None,
    }


async def trigger_403_cache_pause(profile_id: str) -> bool:
    """Disable dispatch for configured minutes when 403 cache is enabled."""
    pid = str(profile_id or "").strip()
    if not pid or not is_profile_403_cache_enabled(pid):
        return False
    from flow2api.services.worker_settings import get_profile_403_cache_minutes

    minutes = get_profile_403_cache_minutes(pid)
    until = time.time() + max(1, minutes) * 60
    async with _LOCK:
        set_profile_403_pause_until(pid, until)
        set_profile_dispatch_enabled(pid, False)
        _AUTO_PAUSED.add(pid)
        _schedule_resume_locked(pid, until)
    logger.warning(
        "profile 403 cache: ngừng nhận job %s phút profile=%s",
        minutes,
        pid[:12],
    )
    events.publish(
        "profile_dispatch_changed",
        {"profile_id": pid, "enabled": False, "reason": "403_cache"},
    )
    events.publish(
        "profile_403_cache_changed",
        {"profile_id": pid, "paused": True, "minutes": minutes},
    )
    return True


async def cancel_403_cache_pause(profile_id: str, *, re_enable_dispatch: bool = False) -> None:
    pid = str(profile_id or "").strip()
    if not pid:
        return
    async with _LOCK:
        task = _RESUME_TASKS.pop(pid, None)
        if task and not task.done():
            task.cancel()
        was_auto = pid in _AUTO_PAUSED
        _AUTO_PAUSED.discard(pid)
        clear_profile_403_pause(pid)
    if re_enable_dispatch and was_auto:
        set_profile_dispatch_enabled(pid, True)
        events.publish(
            "profile_dispatch_changed",
            {"profile_id": pid, "enabled": True, "reason": "403_cache_cancel"},
        )
    events.publish(
        "profile_403_cache_changed",
        {"profile_id": pid, "paused": False},
    )


def _schedule_resume_locked(profile_id: str, until: float) -> None:
    existing = _RESUME_TASKS.pop(profile_id, None)
    if existing and not existing.done():
        existing.cancel()
    delay = max(0.0, until - time.time())
    _RESUME_TASKS[profile_id] = asyncio.create_task(
        _resume_after_delay(profile_id, delay),
        name=f"profile_403_resume_{profile_id[:8]}",
    )


async def _resume_after_delay(profile_id: str, delay_s: float) -> None:
    try:
        if delay_s > 0:
            await asyncio.sleep(delay_s)
        async with _LOCK:
            if profile_id not in _AUTO_PAUSED:
                return
            _AUTO_PAUSED.discard(profile_id)
            _RESUME_TASKS.pop(profile_id, None)
            clear_profile_403_pause(profile_id)
        set_profile_dispatch_enabled(profile_id, True)
        logger.info("profile 403 cache: mở lại nhận job profile=%s", profile_id[:12])
        events.publish(
            "profile_dispatch_changed",
            {"profile_id": profile_id, "enabled": True, "reason": "403_cache_expired"},
        )
        events.publish(
            "profile_403_cache_changed",
            {"profile_id": profile_id, "paused": False, "expired": True},
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("profile 403 cache resume failed profile=%s: %s", profile_id[:12], exc)


async def bootstrap_403_cache_timers() -> None:
    """Restore pending 403-cache pauses after server restart."""
    from flow2api.services.worker_settings import all_profile_403_pause_until

    now = time.time()
    async with _LOCK:
        for pid, until in all_profile_403_pause_until().items():
            if until <= now:
                _AUTO_PAUSED.discard(pid)
                clear_profile_403_pause(pid)
                try:
                    set_profile_dispatch_enabled(pid, True)
                except ValueError:
                    continue
                logger.info("profile 403 cache: hết hạn (startup) profile=%s", pid[:12])
                continue
            set_profile_dispatch_enabled(pid, False)
            _AUTO_PAUSED.add(pid)
            _schedule_resume_locked(pid, until)
            logger.info(
                "profile 403 cache: tiếp tục ngừng job còn %.0fs profile=%s",
                until - now,
                pid[:12],
            )


async def notify_403_cache_pause_if_needed(
    profile_id: str,
    exc: BaseException | None,
    msg: str,
    api_trace: list[dict] | None = None,
) -> bool:
    """Pause profile dispatch immediately when Cache 403 is enabled."""
    from flow2api.services.flow_sdk import is_http_403_failure

    pid = str(profile_id or "").strip()
    if not pid or not is_profile_403_cache_enabled(pid):
        return False
    if not is_http_403_failure(exc, msg, api_trace):
        return False
    return await trigger_403_cache_pause(pid)


def maybe_trigger_403_cache_pause(
    profile_id: str,
    exc: BaseException | None,
    msg: str,
    api_trace: list[dict] | None,
) -> None:
    """Fire-and-forget 403 cache pause from worker exception handler."""
    pid = str(profile_id or "").strip()
    if not pid or not is_profile_403_cache_enabled(pid):
        return
    from flow2api.services.flow_sdk import is_http_403_failure

    if not is_http_403_failure(exc, msg, api_trace):
        return
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(trigger_403_cache_pause(pid))
    except RuntimeError:
        pass
