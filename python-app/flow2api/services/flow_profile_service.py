"""Persist Flow access tokens per Chrome profile (parity Veo3Studio profile.accessToken)."""

from __future__ import annotations



import logging

from datetime import datetime, timedelta, timezone

from typing import Any, Optional



from flow2api.config import (

    FLOW_ACCESS_TOKEN_FALLBACK_TTL_S,

    FLOW_ACCESS_TOKEN_MAX_TTL_S,

    FLOW_ACCESS_TOKEN_TTL_S,

)

from flow2api.db.models import FlowProfile, SessionLocal

from flow2api.services.token_cipher import decrypt_token, encrypt_token



logger = logging.getLogger(__name__)



_AUTH_STATUS_MARGIN_S = 60





def _utcnow() -> datetime:

    return datetime.utcnow()





def _parse_expires_at(value: Any) -> Optional[datetime]:

    if value is None or value == "":

        return None

    if isinstance(value, datetime):

        dt = value

    elif isinstance(value, (int, float)):

        ts = float(value)

        if ts > 1e12:

            ts /= 1000.0

        dt = datetime.utcfromtimestamp(ts)

    else:

        raw = str(value).strip()

        if not raw:

            return None

        if raw.endswith("Z"):

            raw = raw[:-1] + "+00:00"

        try:

            dt = datetime.fromisoformat(raw)

        except ValueError:

            return None

    if dt.tzinfo is not None:

        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)

    return dt





def _resolve_expires_at(

    expires_at: Optional[datetime],

    captured_at: datetime,

) -> datetime:

    """Normalize expiry like Veo3Studio cookieTokenService (API expires or fallback TTL)."""

    now = _utcnow()

    max_future = now + timedelta(seconds=max(60, FLOW_ACCESS_TOKEN_MAX_TTL_S))

    fallback = captured_at + timedelta(seconds=max(60, FLOW_ACCESS_TOKEN_FALLBACK_TTL_S))

    ttl_fallback = captured_at + timedelta(seconds=max(60, FLOW_ACCESS_TOKEN_TTL_S))

    if expires_at:

        if expires_at <= now or expires_at > max_future:

            logger.info(

                "flow token expiry invalid (%s) — using fallback TTL",

                expires_at.isoformat(),

            )

            return fallback

        return expires_at

    return ttl_fallback





def _remaining_seconds(expires_at: Optional[datetime], now: Optional[datetime] = None) -> Optional[int]:

    if not expires_at:

        return None

    now = now or _utcnow()

    return max(0, int((expires_at - now).total_seconds()))





def _token_status(

    *,

    has_token: bool,

    expires_at: Optional[datetime],

    live_present: bool,

    extension_online: bool = False,

    now: Optional[datetime] = None,

) -> str:

    if not has_token:

        if not extension_online:

            return "no-session"

        return "missing"

    rem = _remaining_seconds(expires_at, now)

    if rem is not None and rem <= 0:

        return "expired"

    if rem is not None and rem <= _AUTH_STATUS_MARGIN_S:

        return "stale"

    return "fresh"





def get_profile_row(profile_id: str) -> Optional[FlowProfile]:

    pid = str(profile_id or "").strip()

    if not pid:

        return None

    with SessionLocal() as db:

        return db.get(FlowProfile, pid)





def ensure_profile_row(profile_id: str, *, profile_label: str = "", email: str = "") -> None:

    pid = str(profile_id or "").strip()

    if not pid:

        return

    with SessionLocal() as db:

        row = db.get(FlowProfile, pid)

        if not row:

            row = FlowProfile(profile_id=pid)

            db.add(row)

        if profile_label and profile_label != row.profile_label:

            row.profile_label = profile_label

        if email and email != row.email:

            row.email = email

        row.updated_at = _utcnow()

        db.commit()





def save_access_token(

    profile_id: str,

    access_token: str,

    *,

    profile_label: str = "",

    email: str = "",

    paygate_tier: Optional[str] = None,

    captured_at: Optional[datetime] = None,

    expires_at: Optional[datetime] = None,

) -> None:

    pid = str(profile_id or "").strip()

    token = str(access_token or "").strip()

    if not pid or not token:

        return

    captured = captured_at or _utcnow()

    parsed_expires = _parse_expires_at(expires_at) if expires_at is not None else None

    expires = _resolve_expires_at(parsed_expires, captured)

    enc = encrypt_token(token)

    with SessionLocal() as db:

        row = db.get(FlowProfile, pid)

        if not row:

            row = FlowProfile(profile_id=pid)

            db.add(row)

        row.access_token_enc = enc

        row.token_captured_at = captured

        row.access_token_expires_at = expires

        if profile_label:

            row.profile_label = profile_label

        if email:

            row.email = email

        if paygate_tier:

            row.paygate_tier = paygate_tier

        row.updated_at = _utcnow()

        db.commit()

    logger.debug(

        "flow token saved profile=%s expires=%s",

        pid[:12],

        expires.isoformat(),

    )





def update_profile_meta(

    profile_id: str,

    *,

    profile_label: str = "",

    email: str = "",

    paygate_tier: Optional[str] = None,

) -> None:

    pid = str(profile_id or "").strip()

    if not pid:

        return

    with SessionLocal() as db:

        row = db.get(FlowProfile, pid)

        if not row:

            row = FlowProfile(profile_id=pid)

            db.add(row)

        if profile_label:

            row.profile_label = profile_label

        if email:

            row.email = email

        if paygate_tier:

            row.paygate_tier = paygate_tier

        row.updated_at = _utcnow()

        db.commit()





def clear_access_token(profile_id: str) -> None:

    pid = str(profile_id or "").strip()

    if not pid:

        return

    with SessionLocal() as db:

        row = db.get(FlowProfile, pid)

        if not row:

            return

        row.access_token_enc = None

        row.token_captured_at = None

        row.access_token_expires_at = None

        row.cookies_enc = None

        row.cookies_captured_at = None

        row.updated_at = _utcnow()

        db.commit()





def delete_profile_row(profile_id: str) -> bool:

    """Xóa hẳn dòng FlowProfile khỏi DB. Trả True nếu đã xóa."""

    pid = str(profile_id or "").strip()

    if not pid:

        return False

    with SessionLocal() as db:

        row = db.get(FlowProfile, pid)

        if not row:

            return False

        db.delete(row)

        db.commit()

        return True





def clear_all_access_tokens() -> None:

    with SessionLocal() as db:

        rows = db.query(FlowProfile).all()

        for row in rows:

            row.access_token_enc = None

            row.token_captured_at = None

            row.access_token_expires_at = None

            row.cookies_enc = None

            row.cookies_captured_at = None

            row.updated_at = _utcnow()

        db.commit()





def get_stored_access_token(profile_id: str) -> Optional[str]:

    """Return decrypted ya29 if present and not expired (Veo3Studio getAccessTokenFromProfile)."""

    pid = str(profile_id or "").strip()

    if not pid:

        return None

    with SessionLocal() as db:

        row = db.get(FlowProfile, pid)

        if not row or not row.access_token_enc:

            return None

        if row.access_token_expires_at and row.access_token_expires_at <= _utcnow():

            return None

        try:

            return decrypt_token(row.access_token_enc)

        except Exception as exc:

            logger.warning("decrypt flow token failed profile=%s: %s", pid[:12], exc)

            return None





def list_all_profile_rows() -> list[FlowProfile]:

    with SessionLocal() as db:

        return list(db.query(FlowProfile).all())





def profile_direct_lane_ready(profile_id: str) -> bool:

    from flow2api.config import FLOW_DIRECT_HTTP_ENABLED

    from flow2api.services.cookie_service import has_stored_cookies

    if not FLOW_DIRECT_HTTP_ENABLED:

        return False

    pid = str(profile_id or "").strip()

    if not pid:

        return False

    return has_stored_cookies(pid) and bool(get_stored_access_token(pid))






def get_access_token_for_export(profile_id: str) -> Optional[str]:
    """Decrypt ya29 for backup/export even if marked expired."""
    pid = str(profile_id or "").strip()
    if not pid:
        return None
    with SessionLocal() as db:
        row = db.get(FlowProfile, pid)
        if not row or not row.access_token_enc:
            return None
        try:
            return decrypt_token(row.access_token_enc)
        except Exception as exc:
            logger.warning("export decrypt flow token failed profile=%s: %s", pid[:12], exc)
            return None


def export_profile_session(profile_id: str) -> Optional[dict[str, Any]]:
    from flow2api.services.cookie_service import get_profile_cookies_raw, has_stored_cookies

    pid = str(profile_id or "").strip()
    if not pid:
        return None
    row = get_profile_row(pid)
    if not row:
        return None
    token = get_access_token_for_export(pid)
    cookies = get_profile_cookies_raw(pid)
    return {
        "profile_id": pid,
        "profile_label": row.profile_label or "",
        "email": row.email or "",
        "access_token": token,
        "access_token_expires_at": (
            row.access_token_expires_at.isoformat() + "Z" if row.access_token_expires_at else None
        ),
        "token_captured_at": (
            row.token_captured_at.isoformat() + "Z" if row.token_captured_at else None
        ),
        "cookies": cookies,
        "cookies_captured_at": (
            row.cookies_captured_at.isoformat() + "Z"
            if getattr(row, "cookies_captured_at", None)
            else None
        ),
        "paygate_tier": row.paygate_tier,
        "has_access_token": bool(token),
        "has_cookies": has_stored_cookies(pid),
    }


def export_all_profile_sessions() -> dict[str, Any]:
    from datetime import datetime

    profiles = []
    for row in list_all_profile_rows():
        pid = str(row.profile_id or "").strip()
        if not pid or pid.startswith("_"):
            continue
        item = export_profile_session(pid)
        if item:
            profiles.append(item)
    return {
        "format": "flow2api-profile-sessions",
        "version": 1,
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "count": len(profiles),
        "profiles": profiles,
    }


def import_profile_session(payload: dict[str, Any], *, target_profile_id: str = "") -> dict[str, Any]:
    from flow2api.services.cookie_service import save_profile_cookies

    if not isinstance(payload, dict):
        return {"ok": False, "error": "invalid_payload"}
    pid = str(target_profile_id or payload.get("profile_id") or "").strip()
    if not pid:
        email = str(payload.get("email") or "").strip().lower()
        if email:
            for row in list_all_profile_rows():
                if str(row.email or "").strip().lower() == email:
                    pid = str(row.profile_id)
                    break
    if not pid:
        return {"ok": False, "error": "missing_profile_id"}

    token = str(payload.get("access_token") or payload.get("flowKey") or "").strip()
    cookies = payload.get("cookies")
    label = str(payload.get("profile_label") or payload.get("display_name") or "").strip()
    email = str(payload.get("email") or "").strip()
    tier = payload.get("paygate_tier")
    expires_at = (
        payload.get("access_token_expires_at")
        or payload.get("expiresAt")
        or payload.get("expires_at")
    )

    ensure_profile_row(pid, profile_label=label, email=email)
    if token:
        save_access_token(
            pid,
            token,
            profile_label=label,
            email=email,
            paygate_tier=str(tier) if tier else None,
            expires_at=expires_at,
        )
    elif label or email or tier:
        update_profile_meta(
            pid,
            profile_label=label,
            email=email,
            paygate_tier=str(tier) if tier else None,
        )
    if cookies is not None and cookies != "":
        save_profile_cookies(pid, cookies)

    return {
        "ok": True,
        "profile_id": pid,
        "imported_token": bool(token),
        "imported_cookies": cookies is not None and cookies != "",
    }


def import_profile_sessions(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict) and isinstance(payload.get("profiles"), list):
        items = payload["profiles"]
    elif isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = [payload]
    else:
        return {"ok": False, "error": "invalid_payload", "imported": 0, "results": []}

    results = []
    ok_n = 0
    for item in items:
        if not isinstance(item, dict):
            results.append({"ok": False, "error": "invalid_item"})
            continue
        r = import_profile_session(item)
        results.append(r)
        if r.get("ok"):
            ok_n += 1
    return {"ok": ok_n > 0, "imported": ok_n, "total": len(items), "results": results}


def token_public_fields(

    profile_id: str,

    *,

    live_flow_key_present: bool = False,

    extension_online: bool = False,

) -> dict[str, Any]:

    row = get_profile_row(profile_id)

    now = _utcnow()

    has_token = bool(row and row.access_token_enc)

    expires_at = row.access_token_expires_at if row else None

    captured_at = row.token_captured_at if row else None

    # Active = hạn ya29 từ auth/session (thường ~55 phút). Không dùng cookies_expires_at:
    # cookie Google thường sống hàng tháng → trước đây bị cap 24h nên UI luôn hiện ~24h.
    remaining = _remaining_seconds(expires_at, now) if has_token else None

    status = _token_status(

        has_token=has_token,

        expires_at=expires_at,

        live_present=live_flow_key_present,

        extension_online=extension_online,

        now=now,

    )

    hours_left = round(remaining / 3600, 1) if remaining is not None and remaining > 0 else None

    from flow2api.services.cookie_service import has_stored_cookies

    cookies_ok = has_stored_cookies(profile_id)

    direct_ready = profile_direct_lane_ready(profile_id)

    cookies_exp = getattr(row, "cookies_expires_at", None) if row else None
    cookies_rem = _remaining_seconds(cookies_exp, now) if cookies_exp else None

    return {

        "access_token_expires_at": expires_at.isoformat() + "Z" if expires_at else None,

        "token_captured_at": captured_at.isoformat() + "Z" if captured_at else None,

        "token_remaining_seconds": remaining,

        "token_remaining_seconds_real": remaining,

        "token_hours_left": hours_left,

        "token_status": status,

        "cookies_expires_at": cookies_exp.isoformat() + "Z" if cookies_exp else None,

        "cookies_remaining_seconds": cookies_rem,

        "stored_flow_key_present": has_token and (remaining or 0) > 0,

        "stored_cookies_present": cookies_ok,

        "direct_lane_ready": direct_ready,

        "extension_online": extension_online,

    }





def profile_auth_status(

    profile_id: str,

    *,

    live_flow_key_present: bool = False,

    extension_online: bool = False,

) -> dict[str, Any]:

    """Pre-flight auth check (parity Veo3Studio GET /api/profiles/:id/auth-status)."""

    pid = str(profile_id or "").strip()

    if not pid:

        return {"status": "unknown", "error": "missing_profile_id"}

    if not get_profile_row(pid) and not extension_online:

        return {"status": "unknown", "error": "profile_not_found"}

    meta = token_public_fields(

        pid,

        live_flow_key_present=live_flow_key_present,

        extension_online=extension_online,

    )

    status = str(meta.get("token_status") or "unknown")

    return {

        "status": status,

        "extension_online": extension_online,

        "live_flow_key_present": live_flow_key_present,

        "stored_flow_key_present": meta.get("stored_flow_key_present"),

        "token_expires_at": meta.get("access_token_expires_at"),

        "token_remaining_seconds": meta.get("token_remaining_seconds"),

        "token_hours_left": meta.get("token_hours_left"),

    }





def enrich_public_profile(public: dict[str, Any]) -> dict[str, Any]:

    pid = str(public.get("profile_id") or "").strip()

    if not pid:

        return public

    meta = token_public_fields(

        pid,

        live_flow_key_present=bool(public.get("browser_flow_key_present")),

        extension_online=bool(public.get("online")),

    )

    return {**public, **meta}


