"""ChatGPT multi-profile pool + public-job scheduler (Playwright multi-CDP first)."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from flow2api.services import system_ops
from flow2api.services.chatgpt_broker import get_chatgpt_broker
from flow2api.services.chatgpt_pool_settings import (
    get_chatgpt_pool_settings,
    get_playwright_slot,
    is_chatgpt_dispatch_enabled,
    list_playwright_slots,
    profile_max_concurrent,
)
from flow2api.services.extension_pool import get_extension_pool

logger = logging.getLogger(__name__)

PLAYWRIGHT_PROFILE_ID = "playwright"

_active_jobs: dict[str, int] = {}
_rr_cursor = 0
_scheduler_task: asyncio.Task | None = None
_scheduler_wake: asyncio.Event | None = None
_run_job_fn: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None


def _ensure_wake() -> asyncio.Event:
    global _scheduler_wake
    if _scheduler_wake is None:
        _scheduler_wake = asyncio.Event()
    return _scheduler_wake


def register_job_runner(fn: Callable[[str, dict[str, Any]], Awaitable[None]]) -> None:
    global _run_job_fn
    _run_job_fn = fn


def nudge_scheduler() -> None:
    try:
        _ensure_wake().set()
    except Exception:
        pass
    ensure_scheduler_started()


def ensure_scheduler_started() -> None:
    global _scheduler_task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _scheduler_task is None or _scheduler_task.done():
        _scheduler_task = loop.create_task(_scheduler_loop(), name="chatgpt-pool-scheduler")


def job_started(profile_id: str) -> None:
    pid = (profile_id or "").strip() or PLAYWRIGHT_PROFILE_ID
    _active_jobs[pid] = int(_active_jobs.get(pid) or 0) + 1


def job_finished(profile_id: str) -> None:
    pid = (profile_id or "").strip() or PLAYWRIGHT_PROFILE_ID
    cur = int(_active_jobs.get(pid) or 0)
    if cur <= 1:
        _active_jobs.pop(pid, None)
    else:
        _active_jobs[pid] = cur - 1


def active_job_count(profile_id: str | None = None) -> int:
    if profile_id is None:
        return sum(_active_jobs.values())
    return int(_active_jobs.get(str(profile_id).strip()) or 0)


def running_public_count() -> int:
    broker = get_chatgpt_broker()
    return sum(1 for j in broker.iter_public_jobs() if j.status == "running")


def is_playwright_slot_id(profile_id: str | None) -> bool:
    pid = (profile_id or "").strip()
    if not pid:
        return False
    if pid == PLAYWRIGHT_PROFILE_ID:
        return True
    return get_playwright_slot(pid) is not None


async def list_chatgpt_profiles() -> list[dict[str, Any]]:
    """List worker profiles. When transport=playwright → only CDP slots."""
    cgpt = system_ops.chatgpt_config()
    transport = str(cgpt.get("transport") or "playwright").strip().lower()

    if transport == "playwright":
        return await _list_playwright_profiles()

    # Extension mode — keep extension + http workers (legacy)
    return await _list_extension_profiles()


async def _list_playwright_profiles() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        from flow2api.services.chatgpt_playwright import playwright_slot_status

        st = await playwright_slot_status()
        slot_status = {s["id"]: s for s in (st.get("slots") or []) if s.get("id")}
    except Exception:
        slot_status = {}

    for slot in list_playwright_slots():
        pid = slot.id
        max_c = 1
        active = active_job_count(pid)
        dispatch = is_chatgpt_dispatch_enabled(pid)
        info = slot_status.get(pid) or {}
        online = bool(info.get("cdp_alive"))
        ready = online
        slots_left = max(0, max_c - active) if ready and dispatch else 0
        out.append(
            {
                "profile_id": pid,
                "display_name": slot.label or pid,
                "kind": "playwright",
                "online": online,
                "ready": ready,
                "dispatch_enabled": dispatch,
                "active_jobs": active,
                "max_concurrent": max_c,
                "slots_available": slots_left,
                "accepts_new_jobs": bool(ready and dispatch and slots_left > 0),
                "email": None,
                "port": slot.port,
                "cdp_url": slot.cdp_url(),
                "user_data_dir": slot.user_data_dir(),
                "playwright": {
                    "cdp_alive": online,
                    "browser_open": bool(info.get("browser_open")),
                    "cdp_url": slot.cdp_url(),
                    "port": slot.port,
                },
            }
        )

    out.sort(
        key=lambda p: (
            0 if p.get("accepts_new_jobs") else 1,
            int(p.get("port") or 0),
            str(p.get("profile_id") or ""),
        )
    )
    return out


async def _list_extension_profiles() -> list[dict[str, Any]]:
    pool = get_extension_pool()
    pool.hydrate_db_profiles()
    broker = get_chatgpt_broker()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    for session in pool.list_sessions():
        pid = session.profile_id
        if not pid or pid.startswith("_"):
            continue
        seen.add(pid)
        max_c = profile_max_concurrent(pid)
        active = active_job_count(pid)
        online = bool(session.connected)
        dispatch = is_chatgpt_dispatch_enabled(pid)
        slots = max(0, max_c - active) if online and dispatch else 0
        out.append(
            {
                "profile_id": pid,
                "display_name": session.display_name(),
                "kind": "extension",
                "online": online,
                "ready": online,
                "dispatch_enabled": dispatch,
                "active_jobs": active,
                "max_concurrent": max_c,
                "slots_available": slots,
                "accepts_new_jobs": bool(online and dispatch and slots > 0),
                "email": session.email,
            }
        )

    for w in broker.online_workers():
        wid = str(w.get("worker_id") or "").strip()
        if not wid or wid in seen:
            continue
        seen.add(wid)
        max_c = profile_max_concurrent(wid)
        active = active_job_count(wid)
        dispatch = is_chatgpt_dispatch_enabled(wid)
        slots = max(0, max_c - active) if dispatch else 0
        out.append(
            {
                "profile_id": wid,
                "display_name": w.get("label") or wid[:10],
                "kind": "http_worker",
                "online": True,
                "ready": True,
                "dispatch_enabled": dispatch,
                "active_jobs": active,
                "max_concurrent": max_c,
                "slots_available": slots,
                "accepts_new_jobs": bool(dispatch and slots > 0),
                "email": None,
            }
        )
    return out


def queue_summary() -> dict[str, Any]:
    broker = get_chatgpt_broker()
    queued = running = done = failed = 0
    for j in broker.iter_public_jobs():
        if j.status == "queued":
            queued += 1
        elif j.status == "running":
            running += 1
        elif j.status == "done":
            done += 1
        elif j.status == "failed":
            failed += 1
    settings = get_chatgpt_pool_settings()
    return {
        "queued": queued,
        "running": running,
        "done": done,
        "failed": failed,
        "max_concurrent": settings.max_concurrent,
        "active_slots": active_job_count(),
        "playwright_slots": len(settings.playwright_slots),
    }


def _profile_by_id(profiles: list[dict[str, Any]], profile_id: str) -> dict[str, Any] | None:
    pid = (profile_id or "").strip()
    for p in profiles:
        if p.get("profile_id") == pid:
            return p
    return None


def pick_profile_for_job(
    profiles: list[dict[str, Any]],
    *,
    pinned: str | None = None,
) -> str | None:
    global _rr_cursor
    pinned = (pinned or "").strip() or None
    if pinned == PLAYWRIGHT_PROFILE_ID:
        # Map legacy id → first accepting playwright slot
        for p in profiles:
            if p.get("kind") == "playwright" and p.get("accepts_new_jobs"):
                return str(p["profile_id"])
        return None
    if pinned:
        p = _profile_by_id(profiles, pinned)
        if p and p.get("accepts_new_jobs"):
            return pinned
        return None

    eligible = [p for p in profiles if p.get("accepts_new_jobs")]
    if not eligible:
        return None
    eligible.sort(key=lambda p: (int(p.get("active_jobs") or 0), str(p.get("profile_id"))))
    idx = _rr_cursor % len(eligible)
    _rr_cursor += 1
    return str(eligible[idx]["profile_id"])


async def _scheduler_loop() -> None:
    wake = _ensure_wake()
    while True:
        try:
            await _scheduler_tick()
        except Exception:
            logger.exception("chatgpt scheduler tick failed")
        wake.clear()
        try:
            await asyncio.wait_for(wake.wait(), timeout=1.5)
        except asyncio.TimeoutError:
            pass


async def _scheduler_tick() -> None:
    if _run_job_fn is None:
        return
    broker = get_chatgpt_broker()
    settings = get_chatgpt_pool_settings()
    running = running_public_count()
    slots = max(0, int(settings.max_concurrent) - running)
    if slots <= 0:
        return

    profiles = await list_chatgpt_profiles()
    if not any(p.get("accepts_new_jobs") for p in profiles):
        return

    queued = sorted(
        (j for j in broker.iter_public_jobs() if j.status == "queued"),
        key=lambda j: j.created_at,
    )
    for job in queued:
        if slots <= 0:
            break
        pinned = None
        if job.params_summary.get("profile_assigned_by_user"):
            pinned = str(job.params_summary.get("profile_id") or "").strip() or None
        elif job.params_summary.get("profile_id"):
            pinned = str(job.params_summary.get("profile_id") or "").strip() or None

        chosen = pick_profile_for_job(profiles, pinned=pinned)
        if not chosen:
            if pinned:
                continue
            break

        kwargs = broker.get_public_kwargs(job.job_id) or {}
        kwargs = {**kwargs, "profile_id": chosen}
        if is_playwright_slot_id(chosen):
            kwargs["_force_playwright"] = True
            kwargs["_force_extension"] = False
            kwargs["slot_id"] = chosen
        else:
            kwargs["_force_playwright"] = False
            kwargs["_force_extension"] = True

        broker.update_public_params(
            job.job_id,
            {
                "profile_id": chosen,
                "assigned_profile_id": chosen,
                "slot_id": chosen if is_playwright_slot_id(chosen) else None,
            },
        )
        broker.mark_public_running(job.job_id)
        job_started(chosen)
        slots -= 1

        for p in profiles:
            if p.get("profile_id") == chosen:
                p["active_jobs"] = int(p.get("active_jobs") or 0) + 1
                mc = int(p.get("max_concurrent") or 1)
                p["slots_available"] = max(0, mc - int(p["active_jobs"]))
                p["accepts_new_jobs"] = bool(
                    p.get("ready")
                    and p.get("dispatch_enabled")
                    and p["slots_available"] > 0
                )

        async def _wrapped(jid: str = job.job_id, kw: dict = kwargs, pid: str = chosen) -> None:
            try:
                assert _run_job_fn is not None
                await _run_job_fn(jid, kw)
            finally:
                job_finished(pid)
                nudge_scheduler()

        task = asyncio.create_task(_wrapped(), name=f"chatgpt-job-{job.job_id[:8]}")
        broker.track_public_task(job.job_id, task)
        logger.info(
            "chatgpt scheduler started job=%s profile=%s",
            job.job_id[:8],
            chosen,
        )
