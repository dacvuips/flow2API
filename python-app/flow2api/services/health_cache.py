"""Cached /api/health — keep dashboard polls off the DB hot path."""
from __future__ import annotations

import asyncio
import time
from typing import Any

from flow2api.config import HEALTH_CACHE_TTL_S, FLOW_DIRECT_HTTP_ENABLED
from flow2api.services import activity
from flow2api.services.extension_pool import get_extension_pool
from flow2api.services.worker_settings import get_worker_settings
from flow2api.worker.processor import get_worker

_cache: dict[str, Any] = {"payload": None, "at": 0.0}
_refresh_lock = asyncio.Lock()


def _captcha_public_stats() -> dict[str, Any]:
    try:
        from flow2api.services.captcha_broker import get_captcha_broker

        return get_captcha_broker().stats()
    except Exception:
        return {
            "centers": [],
            "online_count": 0,
            "pending_count": 0,
            "queued_count": 0,
            "pairings": [],
            "bridge_to_centers": {},
            "center_to_bridges": {},
            "pairing_center_count": 0,
            "pairing_bridge_count": 0,
        }


def _enrich_profiles_with_captcha(
    profiles: list[dict[str, Any]],
    captcha: dict[str, Any],
) -> list[dict[str, Any]]:
    """Gắn thông tin Center reCAPTCHA đã cặp vào từng Bridge profile."""
    centers_by_id = {
        str(c.get("center_id") or ""): c
        for c in (captcha.get("centers") or [])
        if c.get("center_id")
    }
    bridge_to_centers = captcha.get("bridge_to_centers") or {}
    out: list[dict[str, Any]] = []
    for p in profiles:
        row = dict(p)
        pid = str(row.get("profile_id") or "")
        cids = list(bridge_to_centers.get(pid) or [])
        labels: list[str] = []
        for cid in cids:
            c = centers_by_id.get(cid) or {}
            labels.append(str(c.get("label") or cid[:8]))
        row["paired_center_ids"] = cids
        row["paired_center_labels"] = labels
        row["captcha_pair_label"] = " · ".join(labels) if labels else ""
        out.append(row)
    return out


def _build_health_sync() -> dict[str, Any]:
    pool = get_extension_pool()
    worker_cfg = get_worker_settings()
    worker = get_worker()
    captcha = _captcha_public_stats()
    profiles = _enrich_profiles_with_captcha(pool.list_public(), captcha)
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
            "profiles_direct_lane": pool.direct_lane_count(),
            "profiles_offline_gen": pool.offline_gen_count(),
        },
        "profiles": profiles,
        "captcha": captcha,
        "extension": {
            "connected": pool.any_connected(),
            "flow_key_present": bool(first_ready and first_ready.flow_key),
            "token_age_s": first_ready.to_public_dict().get("token_age_s") if first_ready else None,
            "profiles_online": pool.online_count(),
            "profiles_ready": pool.ready_count(),
            "profiles_direct_lane": pool.direct_lane_count(),
            "profiles_offline_gen": pool.offline_gen_count(),
        },
        "extension_connected": pool.any_connected(),
        "direct_http_enabled": FLOW_DIRECT_HTTP_ENABLED,
        "profiles_offline_gen": pool.offline_gen_count(),
        "ws_stats": {
            "connected": pool.any_connected(),
            "profiles_online": pool.online_count(),
            "profiles_ready": pool.ready_count(),
            "profiles_direct_lane": pool.direct_lane_count(),
            "profiles_offline_gen": pool.offline_gen_count(),
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
