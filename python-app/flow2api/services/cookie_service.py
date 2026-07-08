"""Persist and filter Google/Labs cookies per profile (parity Veo3Studio cookieTokenService)."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Optional

from flow2api.db.models import FlowProfile, SessionLocal
from flow2api.services.token_cipher import decrypt_token, encrypt_token

logger = logging.getLogger(__name__)

ESSENTIAL_COOKIE_PATTERNS = frozenset(
    {
        "__Secure-1PSID",
        "__Secure-1PAPISID",
        "__Secure-1PSIDTS",
        "__Secure-1PSIDCC",
        "__Secure-3PSID",
        "__Secure-3PAPISID",
        "__Secure-3PSIDTS",
        "__Secure-3PSIDCC",
        "SAPISID",
        "APISID",
        "SSID",
        "SID",
        "HSID",
        "LSID",
        "LSOSID",
        "OSID",
        "S",
        "SIDCC",
        "__Secure-OSID",
        "OGPC",
        "OGP",
        "ACCOUNT_CHOOSER",
        "NID",
        "1P_JAR",
        "3P_JAR",
        "AEC",
        "__Secure-ENID",
        "CONSENT",
        "SOCS",
        "__Secure-next-auth.session-token",
        "__Secure-next-auth.callback-url",
        "__Host-next-auth.csrf-token",
        "email",
        "EMAIL",
    }
)


def _utcnow() -> datetime:
    return datetime.utcnow()


def _is_essential_name(name: str) -> bool:
    n = str(name or "").strip()
    if not n:
        return False
    if n in ESSENTIAL_COOKIE_PATTERNS:
        return True
    return n.startswith("__Secure-") or n.startswith("__Host-")


def _domain_score(domain: str) -> int:
    d = str(domain or "").lower()
    if not d:
        return 0
    if d in ("labs.google", ".labs.google"):
        return 3
    if d in ("google.com", ".google.com"):
        return 2
    if d.endswith(".google.com") or d.endswith(".google"):
        return 1
    return 0


def _dedup_cookies(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[str, dict[str, Any]] = {}
    for c in cookies:
        name = str(c.get("name") or "").strip()
        if not name or not _is_essential_name(name):
            continue
        incoming = _domain_score(str(c.get("domain") or ""))
        if incoming == 0:
            continue
        existing = best.get(name)
        if not existing or _domain_score(str(existing.get("domain") or "")) < incoming:
            best[name] = c
    return list(best.values())


def build_cookie_header(cookies: Any) -> str:
    """Build filtered Cookie header from JSON array or header string."""
    if cookies is None:
        return ""
    if isinstance(cookies, str):
        raw = cookies.strip()
        if not raw:
            return ""
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                cookies = parsed
            else:
                pairs = [p.strip() for p in raw.split(";") if p.strip()]
                return "; ".join(p for p in pairs if _is_essential_name(p.split("=", 1)[0]))
        except json.JSONDecodeError:
            pairs = [p.strip() for p in raw.split(";") if p.strip()]
            return "; ".join(p for p in pairs if _is_essential_name(p.split("=", 1)[0]))
    if not isinstance(cookies, list):
        return ""
    deduped = _dedup_cookies([c for c in cookies if isinstance(c, dict)])
    return "; ".join(f"{c['name']}={c['value']}" for c in deduped if c.get("name") and c.get("value"))


def save_profile_cookies(profile_id: str, cookies: Any) -> None:
    pid = str(profile_id or "").strip()
    if not pid or cookies is None:
        return
    if isinstance(cookies, str):
        payload = cookies.strip()
        if not payload:
            return
    else:
        payload = json.dumps(cookies, ensure_ascii=False)
    if not payload:
        return
    enc = encrypt_token(payload)
    with SessionLocal() as db:
        row = db.get(FlowProfile, pid)
        if not row:
            row = FlowProfile(profile_id=pid)
            db.add(row)
        row.cookies_enc = enc
        row.cookies_captured_at = _utcnow()
        row.updated_at = _utcnow()
        db.commit()
    logger.info("profile cookies saved profile=%s bytes=%s", pid[:12], len(payload))


def get_profile_cookies_raw(profile_id: str) -> Optional[Any]:
    pid = str(profile_id or "").strip()
    if not pid:
        return None
    with SessionLocal() as db:
        row = db.get(FlowProfile, pid)
        if not row or not row.cookies_enc:
            return None
        try:
            plain = decrypt_token(row.cookies_enc)
        except Exception as exc:
            logger.warning("decrypt cookies failed profile=%s: %s", pid[:12], exc)
            return None
    try:
        return json.loads(plain)
    except json.JSONDecodeError:
        return plain


def get_stored_cookie_header(profile_id: str) -> str:
    raw = get_profile_cookies_raw(profile_id)
    return build_cookie_header(raw) if raw else ""


def has_stored_cookies(profile_id: str) -> bool:
    return bool(get_stored_cookie_header(profile_id))


def clear_profile_cookies(profile_id: str) -> None:
    pid = str(profile_id or "").strip()
    if not pid:
        return
    with SessionLocal() as db:
        row = db.get(FlowProfile, pid)
        if not row:
            return
        row.cookies_enc = None
        row.cookies_captured_at = None
        row.updated_at = _utcnow()
        db.commit()
