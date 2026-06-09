"""Cached /api/health — keep dashboard polls off the DB hot path."""
from __future__ import annotations

import asyncio
import time
from typing import Any

from flow2api.config import HEALTH_CACHE_TTL_S
from flow2api.services import activity
from flow2api.services.extension_pool import get_extension_pool
from flow2api.services.worker_settings import get_worker_settings
from flow2api.worker.processor import get_worker

_cache: dict[str, Any] = {"payload": None, "at": 0.0}
_refresh_lock = asyncio.Lock()


def _build_health_sync() -> dict[str, Any]:
    pool = get_extension_pool()
    worker_cfg = get_worker_settings()
    worker = get_worker()
    profiles = pool.list_public()
    first_ready = pool.first_ready()
    stats = activity.summary_stats()
    queued = activity.count_queued()
    return {
        "ok": True,
        "worker": {
            **worker_cfg.to_dict(),
            "running_slots": worker.running_count(),
            "queued": queued,
            "scheduler_alive": worker.scheduler_alive(),
            "profiles_online": pool.online_count(),
            "profiles_ready": pool.ready_count(),
        },
        "profiles": profiles,
        "extension": {
            "connected": pool.any_connected(),
            "flow_key_present": bool(first_ready and first_ready.flow_key),
            "token_age_s": first_ready.to_public_dict().get("token_age_s") if first_ready else None,
            "profiles_online": pool.online_count(),
            "profiles_ready": pool.ready_count(),
        },
        "extension_connected": pool.any_connected(),
        "ws_stats": {
            "connected": pool.any_connected(),
            "profiles_online": pool.online_count(),
            "profiles_ready": pool.ready_count(),
            "accounts": profiles,
        },
        "queue": stats,
        "debug_version": 4,
    }


async def get_health_payload(*, force: bool = False) -> dict[str, Any]:
    ttl = max(0.5, float(HEALTH_CACHE_TTL_S or 3))
    now = time.monotonic()
    cached = _cache.get("payload")
    if not force and cached and now - float(_cache.get("at") or 0) < ttl:
        return dict(cached)

    async with _refresh_lock:
        now = time.monotonic()
        cached = _cache.get("payload")
        if not force and cached and now - float(_cache.get("at") or 0) < ttl:
            return dict(cached)
        payload = await asyncio.to_thread(_build_health_sync)
        _cache["payload"] = payload
        _cache["at"] = time.monotonic()
        return dict(payload)
