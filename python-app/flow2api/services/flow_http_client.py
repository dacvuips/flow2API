"""Direct Google API HTTP (parity Veo3Studio googleFetch / tlsFetch)."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, Optional

from flow2api.config import (
    FLOW_HTTP_CHROME_MAJOR,
    FLOW_HTTP_IMPERSONATE,
    FLOW_HTTP_USER_AGENT,
)
from flow2api.services.cookie_service import get_stored_cookie_header

logger = logging.getLogger(__name__)


def _derive_sec_ch_ua(chrome_major: int) -> str:
    not_a_brand = '"Not(A:Brand";v="99"' if chrome_major >= 129 else '"Not_A Brand";v="8"'
    return f'"Chromium";v="{chrome_major}", "Google Chrome";v="{chrome_major}", {not_a_brand}'


def _derive_platform(ua: str) -> str:
    if "Windows" in ua:
        return '"Windows"'
    if "Linux" in ua and "Android" not in ua:
        return '"Linux"'
    if "Android" in ua:
        return '"Android"'
    return '"macOS"'


def _default_ua() -> str:
    if FLOW_HTTP_USER_AGENT:
        return FLOW_HTTP_USER_AGENT
    if sys.platform == "win32":
        major = FLOW_HTTP_CHROME_MAJOR
        return (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
        )
    major = FLOW_HTTP_CHROME_MAJOR
    return (
        f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )


def _merge_headers(base: dict[str, str], extra: Optional[dict[str, Any]]) -> dict[str, str]:
    out = dict(base)
    for k, v in (extra or {}).items():
        if v is None:
            continue
        kl = str(k).lower()
        for bk in list(out.keys()):
            if bk.lower() == kl:
                del out[bk]
        out[str(k)] = str(v)
    return out


def _build_base_headers(url: str, ua: str) -> dict[str, str]:
    is_labs = url.startswith("https://labs.google")
    is_cross = "googleapis.com" in url
    major = FLOW_HTTP_CHROME_MAJOR
    return {
        "User-Agent": ua,
        "Accept": "*/*",
        "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
        "Origin": "https://labs.google",
        "Referer": "https://labs.google/",
        "sec-ch-ua": _derive_sec_ch_ua(major),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": _derive_platform(ua),
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin" if is_labs else "cross-site",
        "Priority": "u=1, i",
    }


async def tls_fetch(
    *,
    profile_id: str,
    url: str,
    method: str = "GET",
    headers: Optional[dict[str, Any]] = None,
    body: Any = None,
    timeout: float = 120.0,
    allow_redirects: bool = True,
) -> dict[str, Any]:
    """Low-level TLS impersonated fetch. Returns {status, body, headers, url}."""
    ua = _default_ua()
    hdrs = _merge_headers(_build_base_headers(url, ua), headers)
    is_cross = "googleapis.com" in url
    if not is_cross:
        cookie_hdr = get_stored_cookie_header(profile_id)
        if cookie_hdr:
            hdrs["Cookie"] = cookie_hdr
    else:
        hdrs.pop("Cookie", None)
        for k in list(hdrs.keys()):
            if k.lower() == "cookie":
                del hdrs[k]

    content: bytes | str | None = None
    json_body = None
    if body is not None and method.upper() not in ("GET", "HEAD"):
        if isinstance(body, (dict, list)):
            ct = ""
            for k, v in hdrs.items():
                if k.lower() == "content-type":
                    ct = str(v).lower()
                    break
            if "text/plain" in ct or not ct:
                if not ct:
                    hdrs["Content-Type"] = "text/plain;charset=UTF-8"
                content = json.dumps(body, ensure_ascii=False)
            else:
                json_body = body
        elif isinstance(body, str):
            content = body
        elif isinstance(body, (bytes, bytearray)):
            content = bytes(body)

    try:
        from curl_cffi.requests import AsyncSession
    except ImportError as exc:
        raise RuntimeError("curl_cffi_not_installed") from exc

    async with AsyncSession(impersonate=FLOW_HTTP_IMPERSONATE) as session:
        resp = await session.request(
            method.upper(),
            url,
            headers=hdrs,
            data=content if json_body is None else None,
            json=json_body,
            timeout=timeout,
            allow_redirects=allow_redirects,
        )
        text = resp.text if resp.text is not None else ""
        return {
            "status": int(resp.status_code),
            "body": text,
            "headers": dict(resp.headers),
            "url": str(getattr(resp, "url", None) or url),
        }


async def google_fetch(
    *,
    profile_id: str,
    url: str,
    method: str = "POST",
    headers: Optional[dict[str, Any]] = None,
    body: Any = None,
    timeout: float = 180.0,
    allow_redirects: bool = True,
) -> dict[str, Any]:
    """Extension-compatible response: {status, data, error?, headers, url}."""
    raw = await tls_fetch(
        profile_id=profile_id,
        url=url,
        method=method,
        headers=headers,
        body=body,
        timeout=timeout,
        allow_redirects=allow_redirects,
    )
    status = int(raw.get("status") or 0)
    body_text = str(raw.get("body") or "")
    data: Any = body_text
    if body_text:
        try:
            data = json.loads(body_text)
        except json.JSONDecodeError:
            data = body_text
    out: dict[str, Any] = {
        "status": status,
        "data": data,
        "headers": raw.get("headers") or {},
        "url": raw.get("url") or url,
    }
    if status >= 400:
        out["error"] = f"HTTP_{status}"
    return out
