"""Clear labs.google cookies + reload — blocks profile dispatch while running."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from flow2api.config import PROFILE_POST_CLEAR_WAIT_S
from flow2api.services.dashboard_events import events
from flow2api.services.profile_job_guard import (
    begin_profile_clear_busy,
    finish_profile_clear_cooldown,
)

logger = logging.getLogger(__name__)

_clear_locks: dict[str, asyncio.Lock] = {}


def _lock_for(profile_id: str) -> asyncio.Lock:
    pid = str(profile_id or "").strip()
    if pid not in _clear_locks:
        _clear_locks[pid] = asyncio.Lock()
    return _clear_locks[pid]


async def run_profile_clear(
    session: Any,
    *,
    source: str = "manual",
    reload: bool = True,
) -> dict:
    """Clear labs.google cookies; optional tab reload; blocks dispatch while running."""
    pid = str(getattr(session, "profile_id", "") or "").strip()
    if not pid or pid.startswith("_"):
        return {"ok": False, "error": "invalid_profile"}
    if not getattr(session, "connected", False):
        return {"ok": False, "error": "extension_not_connected"}

    async with _lock_for(pid):
        begin_profile_clear_busy(pid)
        events.publish(
            "profile_job_guard_changed",
            {"profile_id": pid, "reason": "clear_busy", "source": source},
        )
        result: dict = {"ok": False, "error": "clear_failed"}
        try:
            raw = await session.clear_control("now", timeout=60.0, reload=reload)
            result = raw if isinstance(raw, dict) else {"ok": False, "raw": raw}
            if result.get("ok"):
                logger.info(
                    "profile clear ok profile=%s source=%s count=%s",
                    pid[:12],
                    source,
                    (result.get("state") or {}).get("clearCount"),
                )
            else:
                logger.warning(
                    "profile clear failed profile=%s source=%s err=%s",
                    pid[:12],
                    source,
                    result.get("error") or result.get("message"),
                )
        except Exception as exc:
            logger.warning("profile clear error profile=%s: %s", pid[:12], exc)
            result = {"ok": False, "error": str(exc)}
        finally:
            finish_profile_clear_cooldown(pid, PROFILE_POST_CLEAR_WAIT_S)
            events.publish(
                "profile_job_guard_changed",
                {
                    "profile_id": pid,
                    "reason": "clear_cooldown",
                    "source": source,
                    "wait_s": PROFILE_POST_CLEAR_WAIT_S,
                },
            )
        return result
