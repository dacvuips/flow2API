"""Refresh Flow ya29 from stored cookies via auth/session (parity Veo3Studio cookieTokenService)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from flow2api.services.cookie_service import get_stored_cookie_header, has_stored_cookies
from flow2api.services.flow_profile_service import get_profile_row, save_access_token

logger = logging.getLogger(__name__)

AUTH_SESSION_URL = "https://labs.google/fx/api/auth/session"

# Coalesce concurrent refreshes per profile (tránh spam auth/session khi nhiều job cùng lúc)
_inflight: dict[str, asyncio.Task] = {}
_claim_locks: dict[str, asyncio.Lock] = {}


def _claim_lock(pid: str) -> asyncio.Lock:
    lock = _claim_locks.get(pid)
    if lock is None:
        lock = asyncio.Lock()
        _claim_locks[pid] = lock
    return lock


async def refresh_access_token_from_cookies(
    profile_id: str,
    *,
    force: bool = False,
) -> dict[str, Any]:
    pid = str(profile_id or "").strip()
    if not pid:
        return {"ok": False, "error": "missing_profile_id"}

    async with _claim_lock(pid):
        existing = _inflight.get(pid)
        if existing is not None and not existing.done():
            task = existing
        else:

            async def _run() -> dict[str, Any]:
                try:
                    return await _refresh_access_token_from_cookies_impl(pid, force=force)
                finally:
                    cur = _inflight.get(pid)
                    if cur is not None and cur.done():
                        _inflight.pop(pid, None)

            task = asyncio.create_task(_run())
            _inflight[pid] = task

    return await task


async def _refresh_access_token_from_cookies_impl(
    profile_id: str,
    *,
    force: bool = False,
) -> dict[str, Any]:
    pid = str(profile_id or "").strip()
    if not has_stored_cookies(pid):
        return {"ok": False, "error": "no_stored_cookies"}

    cookie_header = get_stored_cookie_header(pid)
    if not cookie_header:
        return {"ok": False, "error": "empty_cookie_header"}

    from flow2api.services.flow_http_client import tls_fetch

    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9",
        "cache-control": "max-age=0",
        "Cookie": cookie_header,
        "referer": "https://labs.google/fx/en/tools/flow",
        "upgrade-insecure-requests": "1",
    }
    try:
        resp = await tls_fetch(
            profile_id=pid,
            url=AUTH_SESSION_URL,
            method="GET",
            headers=headers,
            timeout=30.0,
        )
    except Exception as exc:
        logger.warning("cookie token refresh fetch failed profile=%s: %s", pid[:12], exc)
        return {"ok": False, "error": str(exc)}

    status = int(resp.get("status") or 0)
    body_text = str(resp.get("body") or "")
    if status >= 400 or not body_text.strip():
        return {
            "ok": False,
            "error": f"auth_session_http_{status}",
            "status": status,
            "body_preview": body_text[:200],
        }

    try:
        session_data = json.loads(body_text)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"auth_session_json:{exc}"}

    token = str(session_data.get("access_token") or "").strip()
    if not token:
        return {"ok": False, "error": "AUTH_SESSION_EMPTY", "keys": list(session_data.keys())}
    logger.info(
        "auth/session response keys=%s expires=%r",
        list(session_data.keys()),
        session_data.get("expires"),
    )

    row = get_profile_row(pid)
    save_access_token(
        pid,
        token,
        profile_label=str(row.profile_label if row else ""),
        email=str(row.email if row else ""),
        paygate_tier=str(row.paygate_tier) if row and row.paygate_tier else None,
        expires_at=session_data.get("expires"),
    )
    logger.info("cookie token refresh ok profile=%s", pid[:12])
    return {
        "ok": True,
        "method": "cookie_auth_session",
        "flowKey": token,
        "expiresAt": session_data.get("expires"),
    }
