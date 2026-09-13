"""Shared reCAPTCHA Enterprise minting for batchexecute generate calls.

Verified empirically that a reCAPTCHA Enterprise token is NOT bound to the
Google account that minted it — a token minted under one account's session was
accepted by Google when submitted as part of a request authenticated by a
completely different account's cookies/f.sid/at (see flow_batchexecute_client.py
docstring for the full picture). This means one "Center" profile — any Flow CDP
slot with role="center", parked on any one of its own projects — can mint tokens
on behalf of every Gen profile, instead of every Gen profile needing to mint its
own (which would mean every Gen profile also needs a live CDP tab just for this).

Gen profiles still need their own project id + f.sid/bl/at (those ARE bound to
the account/session — see flow_profile_service.save_batchexecute_session), but
that triple is reusable across many calls and does not need a live tab per call,
so pairing it with tokens minted here lets Gen CDP stay closed most of the time.
"""
from __future__ import annotations

import asyncio
import logging

from flow2api.services import system_ops
from flow2api.services.flow_cdp_settings import list_flow_cdp_slots

logger = logging.getLogger(__name__)

_RECAPTCHA_SITE_KEY = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"

# Nhiều job Gen mint captcha đồng thời qua cùng Center → mỗi job tự
# connect_over_cdp riêng, không khoá tuần tự → nhiều lần navigate/goto chồng
# nhau trên cùng browser gây tích tụ tab flow.google.com dư (Flow SPA tự mở
# tab mới khi phát hiện điều hướng chồng chéo). Khoá 1 mint tại một thời điểm.
_MINT_LOCK = asyncio.Lock()


class CaptchaCenterError(RuntimeError):
    pass


def _require_playwright():
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        raise CaptchaCenterError(
            "playwright_not_installed — pip install playwright && playwright install chrome"
        ) from exc


def _pick_center_slot():
    for slot in list_flow_cdp_slots():
        if slot.role != "center":
            continue
        if system_ops.cdp_endpoint_alive(slot.cdp_url()):
            return slot
    return None


async def _find_project_page(context):
    """Return a page sitting on a Flow project — reCAPTCHA's enterprise.js is
    only loaded there (confirmed empirically: neither / nor /about load it),
    navigating to one of the account's own existing projects if needed."""
    for page in context.pages:
        if "flow.google.com/project/" in (page.url or ""):
            return page

    page = next(
        (p for p in context.pages if "flow.google.com" in (p.url or "")),
        context.pages[0] if context.pages else None,
    )
    if page is None:
        raise CaptchaCenterError("center_has_no_page")

    project_links = await page.evaluate(
        """() => Array.from(document.querySelectorAll('a[href*="/project/"]'))
            .map(a => a.href)
            .filter(h => h.includes('flow.google.com/project/'))"""
    )
    if not project_links:
        raise CaptchaCenterError(
            "center_has_no_project — tài khoản Captcha Center cần có ít nhất 1 project Flow đã tạo."
        )
    await page.goto(project_links[0], wait_until="load", timeout=30_000)
    await page.wait_for_timeout(1000)
    return page


async def mint_captcha_token(*, action: str) -> str:
    """Mint a reCAPTCHA Enterprise token on the Captcha Center's own tab.

    Clearing `_grecaptcha*` localStorage before and after minting mirrors what
    the Captcha Center extension's injected.js does for the REST lane — leftover
    markers between mints otherwise accumulate and depress the risk score.
    """
    _require_playwright()
    async with _MINT_LOCK:
        return await _mint_captcha_token_locked(action=action)


async def _mint_captcha_token_locked(*, action: str) -> str:
    slot = _pick_center_slot()
    if not slot:
        raise CaptchaCenterError(
            "no_center_available — cần ít nhất 1 Flow CDP slot role=center đang mở."
        )

    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.connect_over_cdp(slot.cdp_url())
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await _find_project_page(context)

        # Dọn tab flow.google.com dư tích tụ từ các lần mint/navigate trước —
        # chỉ giữ tab đang dùng, tránh cửa sổ Center phình to theo thời gian.
        if len(context.pages) > 1:
            for extra in list(context.pages):
                if extra is page:
                    continue
                if "flow.google.com" not in (extra.url or ""):
                    continue
                try:
                    await extra.close()
                except Exception:
                    pass

        token = await page.evaluate(
            """async ({ siteKey, action, timeoutMs }) => {
                const clear = () => {
                    try {
                        Object.keys(localStorage)
                            .filter((k) => k.startsWith('_grecaptcha'))
                            .forEach((k) => localStorage.removeItem(k));
                    } catch (e) { /* cross-origin storage denied */ }
                };
                const waitReady = () => new Promise((resolve, reject) => {
                    const start = Date.now();
                    const check = () => {
                        if (window.grecaptcha && window.grecaptcha.enterprise && window.grecaptcha.enterprise.execute) {
                            return resolve();
                        }
                        if (Date.now() - start > timeoutMs) return reject(new Error('grecaptcha_not_available'));
                        setTimeout(check, 150);
                    };
                    check();
                });
                await waitReady();
                clear();
                const token = await new Promise((resolve, reject) => {
                    window.grecaptcha.enterprise.ready(async () => {
                        try {
                            resolve(await window.grecaptcha.enterprise.execute(siteKey, { action }));
                        } catch (e) {
                            reject(e);
                        }
                    });
                });
                clear();
                return token;
            }""",
            {"siteKey": _RECAPTCHA_SITE_KEY, "action": action, "timeoutMs": 20_000},
        )
        if not token:
            raise CaptchaCenterError("recaptcha_mint_empty")
        return str(token)
    finally:
        try:
            await pw.stop()
        except Exception:
            pass
