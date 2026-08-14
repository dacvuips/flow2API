"""Playwright control for Flow CDP slots — login page, logout, clear data, cookie sync."""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any

from flow2api.services import system_ops
from flow2api.services.flow_cdp_settings import (
    get_flow_cdp_slot,
    list_flow_cdp_slots,
    remove_flow_cdp_slot,
    update_flow_cdp_slot,
)

logger = logging.getLogger(__name__)

_FLOW_START = "https://labs.google/fx/tools/flow"
_LOCKS: dict[str, asyncio.Lock] = {}
_AUTO_ATTACH_TS: dict[str, float] = {}
_AUTO_ATTACH_COOLDOWN_S = 40.0
_AUTO_ATTACH_LOCK = asyncio.Lock()
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def _is_cdp_unreachable_error(exc: BaseException | str) -> bool:
    text = str(exc or "").lower()
    return "cdp_unreachable" in text or (
        "cdp" in text and ("chưa chạy" in text or "chua chay" in text)
    )


def _slot_lock(slot_id: str) -> asyncio.Lock:
    sid = str(slot_id or "").strip() or "_"
    lock = _LOCKS.get(sid)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[sid] = lock
    return lock


def _looks_like_default_label(label: str, slot_id: str) -> bool:
    lab = str(label or "").strip()
    sid = str(slot_id or "").strip()
    if not lab or lab == sid:
        return True
    low = lab.lower()
    return (
        low.startswith("flow cdp")
        or low.startswith("captcha center cdp")
        or low.startswith("chrome")
    )


def _attach_email_to_slot(slot_id: str, email: str, *, profile_id: str = "") -> str:
    """Persist email on Flow CDP slot + linked FlowProfile. Returns normalized email."""
    from flow2api.services.flow_profile_service import ensure_profile_row, update_profile_meta

    em = str(email or "").strip()
    if not em or "@" not in em:
        return ""
    slot = get_flow_cdp_slot(slot_id)
    if not slot:
        return ""
    pid = str(profile_id or slot.profile_id()).strip() or slot.id
    first_email = not bool(str(slot.email or "").strip())
    new_label = em if _looks_like_default_label(slot.label, slot.id) else None
    kwargs: dict[str, Any] = {"email": em, "linked_profile_id": pid}
    if new_label:
        kwargs["label"] = new_label
    update_flow_cdp_slot(slot.id, **kwargs)
    ensure_profile_row(pid, profile_label=new_label or slot.label or em, email=em)
    update_profile_meta(pid, profile_label=new_label or slot.label or em, email=em)
    # Lần đầu gắn email (login/Sync tay) → standby; auto-run sẽ bật Nhận job sau.
    if first_email and (slot.role or "bridge") != "center":
        try:
            from flow2api.services.flow_cdp_auto import mark_cdp_profile_standby

            mark_cdp_profile_standby(pid, reason=f"first_email:{slot.id}")
        except Exception as exc:
            logger.warning("standby on first email failed slot=%s: %s", slot.id, exc)
    return em


def flow_cdp_public_status(
    slot_id: str | None = None,
    *,
    probe: bool = True,
) -> list[dict[str, Any]]:
    """List slots. probe=True pings each CDP (can be slow); False = DB/settings only."""
    from flow2api.services.cookie_service import has_stored_cookies
    from flow2api.services.flow_profile_service import get_profile_row

    slots = list_flow_cdp_slots()
    if slot_id:
        sid = str(slot_id).strip()
        slots = [s for s in slots if s.id == sid]
    out: list[dict[str, Any]] = []
    for s in slots:
        cdp = s.cdp_url()
        alive = system_ops.cdp_endpoint_alive(cdp) if probe else False
        pid = s.profile_id()
        row = get_profile_row(pid)
        email = (s.email or (row.email if row else "") or "").strip()
        has_cookies = has_stored_cookies(pid)
        has_token = bool(row and row.access_token_enc)
        role_label = "Gen (Img/Vid)" if s.role == "bridge" else "Captcha Center"
        display = email or s.label or s.id
        item = {
            **s.to_dict(),
            "profile_id": s.id,
            "display_name": display,
            "email": email,
            "online": alive,
            "cdp_alive": alive,
            "ready": bool(has_cookies or has_token) or (alive and bool(email)),
            "has_cookies": has_cookies,
            "has_token": has_token,
            "offline_gen_ready": has_cookies and has_token,
            "role_label": role_label,
            "transport": "cdp",
            "logged_in": bool(email),
        }
        if not probe:
            item["probe_skipped"] = True
        out.append(item)
    return out


async def _agen_page(slot_id: str):
    """Async generator: connect over CDP once, then detach (Chrome stays open)."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(
            "playwright_not_installed — pip install playwright && playwright install chrome"
        ) from exc

    slot = get_flow_cdp_slot(slot_id)
    if not slot:
        raise RuntimeError("slot_not_found")
    cdp = slot.cdp_url()
    if not system_ops.cdp_endpoint_alive(cdp):
        raise RuntimeError(
            f"cdp_unreachable — CDP {cdp} chưa chạy. Bấm Mở CDP trước."
        )

    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.connect_over_cdp(cdp)
        contexts = browser.contexts
        context = contexts[0] if contexts else await browser.new_context()
        pages = context.pages
        page = pages[0] if pages else await context.new_page()
        yield slot, browser, context, page
    finally:
        try:
            await pw.stop()
        except Exception:
            pass


async def open_flow_login(slot_id: str) -> dict[str, Any]:
    """Navigate slot Chrome to Flow login page (nhanh — tránh Cloudflare 524)."""
    async with _slot_lock(slot_id):
        async for slot, _browser, _context, page in _agen_page(slot_id):
            url = _FLOW_START
            try:
                cfg = system_ops.load_config()
                url = str(cfg.get("flow_url") or _FLOW_START).strip() or _FLOW_START
            except Exception:
                pass
            nav_err = ""
            try:
                # commit: navigation đã bắt đầu — không chờ DOM đầy đủ (SSO/Flow hay chậm)
                await page.goto(url, wait_until="commit", timeout=25_000)
            except Exception as exc:
                nav_err = str(exc or "")[:160]
                # Trang có thể đã về Flow / đang load — vẫn coi là OK nếu URL hợp lý
                try:
                    cur = str(page.url or "")
                except Exception:
                    cur = ""
                if not (
                    "labs.google" in cur
                    or "flow" in cur.lower()
                    or "accounts.google" in cur
                ):
                    raise RuntimeError(
                        f"open_flow_nav_failed — không mở được Flow trên CDP {slot.id}. "
                        f"Thử lại khi cửa sổ Chrome sẵn sàng. ({nav_err})"
                    ) from exc
            try:
                final_url = str(page.url or url)
            except Exception:
                final_url = url
            msg = f"Đã mở Flow trên CDP {slot.id}. Đăng nhập Google trong cửa sổ này."
            if nav_err:
                msg += " (trang đang load — kiểm tra cửa sổ CDP)"
            return {
                "ok": True,
                "slot_id": slot.id,
                "url": final_url,
                "message": msg,
            }
        return {"ok": False, "error": "attach_failed"}


async def logout_flow(slot_id: str) -> dict[str, Any]:
    """Clear Google/Labs session cookies in the CDP browser (keeps user-data dir)."""
    async with _slot_lock(slot_id):
        async for slot, _browser, context, page in _agen_page(slot_id):
            try:
                await context.clear_cookies()
            except Exception as exc:
                logger.warning("clear_cookies failed slot=%s: %s", slot.id, exc)
            try:
                await page.goto(
                    "https://accounts.google.com/Logout",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
            except Exception:
                pass
            try:
                await page.goto(_FLOW_START, wait_until="domcontentloaded", timeout=30_000)
            except Exception:
                pass
            update_flow_cdp_slot(slot.id, email="")
            return {
                "ok": True,
                "slot_id": slot.id,
                "message": (
                    f"Đã logout slot {slot.id}. Cookie browser đã xóa — "
                    "DB cookies giữ nguyên (bấm Clear data nếu muốn xóa luôn)."
                ),
            }
        return {"ok": False, "error": "attach_failed"}


async def clear_slot_data(slot_id: str, *, wipe_disk: bool = False) -> dict[str, Any]:
    """Clear cookies in browser + optional wipe user-data dir + clear linked DB profile cookies."""
    from flow2api.services.cookie_service import clear_profile_cookies
    from flow2api.services.flow_profile_service import clear_access_token

    slot = get_flow_cdp_slot(slot_id)
    if not slot:
        return {"ok": False, "error": "slot_not_found"}

    browser_cleared = False
    if system_ops.cdp_endpoint_alive(slot.cdp_url()):
        async with _slot_lock(slot_id):
            try:
                async for _s, _b, context, page in _agen_page(slot_id):
                    await context.clear_cookies()
                    try:
                        await page.evaluate(
                            """async () => {
                              try { localStorage.clear(); } catch (e) {}
                              try { sessionStorage.clear(); } catch (e) {}
                              try {
                                if (indexedDB && indexedDB.databases) {
                                  const dbs = await indexedDB.databases();
                                  for (const db of dbs || []) {
                                    if (db && db.name) indexedDB.deleteDatabase(db.name);
                                  }
                                }
                              } catch (e) {}
                            }"""
                        )
                    except Exception:
                        pass
                    browser_cleared = True
            except Exception as exc:
                logger.warning("clear browser data failed slot=%s: %s", slot_id, exc)

    pid = slot.profile_id()
    clear_profile_cookies(pid)
    clear_access_token(pid)
    update_flow_cdp_slot(slot.id, email="")

    disk_wiped = False
    if wipe_disk:
        if system_ops.cdp_endpoint_alive(slot.cdp_url()):
            return {
                "ok": False,
                "error": "cdp_still_running",
                "message": "Đóng cửa sổ Chrome CDP trước khi xóa user-data trên disk.",
                "browser_cleared": browser_cleared,
            }
        ud = Path(slot.user_data_dir())
        if ud.is_dir():
            try:
                shutil.rmtree(ud, ignore_errors=True)
                disk_wiped = True
            except Exception as exc:
                return {
                    "ok": False,
                    "error": "wipe_failed",
                    "message": str(exc),
                    "browser_cleared": browser_cleared,
                }

    return {
        "ok": True,
        "slot_id": slot.id,
        "browser_cleared": browser_cleared,
        "disk_wiped": disk_wiped,
        "message": (
            f"Đã clear data slot {slot.id}"
            + (" + xóa user-data dir" if disk_wiped else "")
            + "."
        ),
    }


async def delete_slot_fully(slot_id: str) -> dict[str, Any]:
    """Xóa đúng nghĩa một CDP slot: đóng Chrome → wipe disk → xóa DB profile → retire port/id."""
    from flow2api.services.extension_pool import get_extension_pool
    from flow2api.services.flow_cdp_auto_settings import (
        get_flow_cdp_auto_settings,
        save_flow_cdp_auto_settings,
    )
    from flow2api.services.flow_profile_service import delete_profile_row
    from flow2api.services.worker_settings import purge_profile

    sid = str(slot_id or "").strip()
    slot = get_flow_cdp_slot(sid)
    if not slot:
        return {"ok": False, "error": "slot_not_found"}

    pid = slot.profile_id()
    port = int(slot.port)
    ud = Path(slot.user_data_dir())

    # 1) Đóng Chrome CDP
    close = system_ops.close_flow_cdp_slot(sid)
    await asyncio.sleep(0.8)
    if system_ops.cdp_endpoint_alive(slot.cdp_url()):
        close = system_ops.close_flow_cdp_slot(sid)
        await asyncio.sleep(1.2)

    # 2) Wipe user-data dir
    disk_wiped = False
    disk_err = None
    if ud.is_dir():
        try:
            shutil.rmtree(ud, ignore_errors=False)
            disk_wiped = not ud.exists()
            if not disk_wiped:
                shutil.rmtree(ud, ignore_errors=True)
                disk_wiped = not ud.exists()
        except Exception as exc:
            disk_err = str(exc)
            logger.warning("wipe user-data failed slot=%s: %s", sid, exc)
            try:
                shutil.rmtree(ud, ignore_errors=True)
                disk_wiped = not ud.exists()
            except Exception:
                pass

    # 3) Xóa FlowProfile DB + purge worker settings + bỏ khỏi pool
    db_deleted = False
    try:
        db_deleted = bool(delete_profile_row(pid))
    except Exception as exc:
        logger.warning("delete profile row failed pid=%s: %s", pid, exc)
    try:
        purge_profile(pid)
    except Exception as exc:
        logger.warning("purge worker settings failed pid=%s: %s", pid, exc)
    try:
        pool = get_extension_pool()
        pool._sessions.pop(pid, None)  # noqa: SLF001
        if pid != sid:
            pool._sessions.pop(sid, None)  # noqa: SLF001
    except Exception:
        pass

    # 4) Gỡ khỏi auto settings
    try:
        auto = get_flow_cdp_auto_settings()
        new_order = [x for x in auto.slot_order if x != sid]
        new_enabled = {k: v for k, v in auto.slot_enabled.items() if k != sid}
        save_flow_cdp_auto_settings(slot_order=new_order, slot_enabled=new_enabled)
    except Exception as exc:
        logger.warning("cleanup auto settings failed slot=%s: %s", sid, exc)

    # 5) Xóa slot + retire port/id (không tái cấp)
    remove_flow_cdp_slot(sid)

    msg = f"Đã xóa {sid} · port {port} đã retire · CDP mới sẽ nhận port khác"
    if disk_wiped:
        msg += " · đã xóa user-data"
    elif disk_err:
        msg += f" · user-data chưa xóa hết ({disk_err})"

    return {
        "ok": True,
        "slot_id": sid,
        "profile_id": pid,
        "retired_port": port,
        "db_deleted": db_deleted,
        "disk_wiped": disk_wiped,
        "close": close,
        "message": msg,
    }


def _guess_email_from_cookies(cookies: list[dict[str, Any]]) -> str:
    for c in cookies:
        name = str(c.get("name") or "").lower()
        val = str(c.get("value") or "").strip()
        if "@" not in val:
            continue
        if name == "email" or "email" in name:
            m = _EMAIL_RE.search(val)
            if m:
                return m.group(0)
        if name in ("sapisid", "apisid", "hsid", "sid"):
            continue
        m = _EMAIL_RE.search(val)
        if m and "google" in str(c.get("domain") or "").lower():
            return m.group(0)
    return ""


async def _detect_email(page) -> str:
    try:
        raw = await page.evaluate(
            """() => {
              const texts = [];
              const nodes = document.querySelectorAll(
                '[data-email], [aria-label*="@"], img[alt*="@"], [data-user-email], [data-identifier]'
              );
              nodes.forEach(n => {
                texts.push(
                  n.getAttribute('data-email')
                  || n.getAttribute('data-user-email')
                  || n.getAttribute('data-identifier')
                  || n.getAttribute('aria-label')
                  || n.getAttribute('alt')
                  || ''
                );
              });
              const body = (document.body && document.body.innerText) || '';
              return { texts: texts.join(' '), body: body.slice(0, 12000), title: document.title || '' };
            }"""
        )
        blob = (
            f"{(raw or {}).get('texts') or ''} "
            f"{(raw or {}).get('body') or ''} "
            f"{(raw or {}).get('title') or ''}"
        )
        m = _EMAIL_RE.search(blob)
        if m:
            return m.group(0)
    except Exception:
        pass
    return ""


async def _fetch_email_from_token(token: str) -> str:
    """Same as extension: GET oauth2/v2/userinfo with Bearer ya29."""
    tok = str(token or "").strip()
    if not tok:
        return ""
    try:
        import httpx

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {tok}"},
            )
            if resp.status_code == 200:
                data = resp.json() if resp.content else {}
                email = str((data or {}).get("email") or "").strip()
                if email and "@" in email:
                    return email
    except Exception as exc:
        logger.debug("userinfo email failed: %s", exc)
    return ""


def _has_flow_session_cookie(cookies: list[dict[str, Any]]) -> bool:
    for c in cookies:
        if not isinstance(c, dict):
            continue
        if str(c.get("name") or "") == "__Secure-next-auth.session-token":
            return bool(str(c.get("value") or "").strip())
    return False


async def _wait_flow_ready(page, *, timeout_ms: int = 12_000) -> None:
    """Poll until NextAuth session-token exists — return immediately if already set."""
    deadline = time.time() + (timeout_ms / 1000.0)
    while time.time() < deadline:
        try:
            cookies = await page.context.cookies()
            if _has_flow_session_cookie(cookies):
                return
        except Exception:
            pass
        try:
            await page.wait_for_timeout(200)
        except Exception:
            break


async def _auth_session_from_page(page) -> dict[str, Any]:
    """Same path as extension: fetch auth/session inside the Flow tab."""
    try:
        raw = await page.evaluate(
            """async () => {
              const resp = await fetch('https://labs.google/fx/api/auth/session', {
                credentials: 'include',
                headers: { accept: 'application/json' },
              });
              let body = null;
              try { body = await resp.json(); } catch { body = null; }
              return { ok: !!resp.ok, status: resp.status, body };
            }"""
        )
    except Exception as exc:
        return {"ok": False, "error": f"page_auth_session:{exc}"}
    body = (raw or {}).get("body")
    if not isinstance(body, dict):
        return {
            "ok": False,
            "error": "AUTH_SESSION_EMPTY",
            "status": (raw or {}).get("status"),
        }
    token = str(body.get("access_token") or "").strip()
    user = body.get("user") if isinstance(body.get("user"), dict) else {}
    email = str((user or {}).get("email") or "").strip()
    if not token:
        return {
            "ok": False,
            "error": "AUTH_SESSION_EMPTY",
            "status": (raw or {}).get("status"),
            "keys": list(body.keys()),
            "email": email,
        }
    return {
        "ok": True,
        "method": "page_auth_session",
        "flowKey": token,
        "email": email,
        "expiresAt": body.get("expires"),
    }


def _sync_not_logged_in_message(slot_id: str, *, cookies_n: int, has_session: bool) -> str:
    base = f"Đã sync session slot {slot_id}"
    if cookies_n:
        base += f" · {cookies_n} cookies"
    if has_session:
        return (
            f"{base} · chưa lấy được token/email. "
            "Mở đúng cửa sổ CDP này, đợi Flow load xong rồi Sync lại."
        )
    return (
        f"{base} · cửa sổ CDP này CHƯA đăng nhập Flow "
        "(thiếu session-token). Email trên extension có thể thuộc CDP/profile khác — "
        "bấm Sign in Google ngay trong cửa sổ Chrome của slot này, đợi vào Flow rồi Sync lại."
    )


async def attach_email_only(slot_id: str) -> dict[str, Any]:
    """Detect logged-in email and persist slot + FlowProfile metadata.

    Does NOT save cookies/token to DB — that is only for Sync session / auto cycle.
    Chrome user-data already keeps the Google login for later automation.
    """
    from flow2api.services.flow_profile_service import ensure_profile_row

    async with _slot_lock(slot_id):
        async for slot, _browser, context, page in _agen_page(slot_id):
            try:
                cur = str(page.url or "")
            except Exception:
                cur = ""
            if not _url_looks_like_flow(cur):
                try:
                    await page.goto(_FLOW_START, wait_until="commit", timeout=45_000)
                except Exception as exc:
                    logger.warning("goto flow failed slot=%s: %s", slot.id, exc)
            await _wait_flow_ready(page, timeout_ms=8_000)

            cookies = await context.cookies()
            useful = [
                c
                for c in cookies
                if isinstance(c, dict)
                and (
                    "google" in str(c.get("domain") or "").lower()
                    or "labs" in str(c.get("domain") or "").lower()
                )
            ]
            if not useful:
                useful = [c for c in cookies if isinstance(c, dict)]

            page_sess = await _auth_session_from_page(page)
            email = (
                str(page_sess.get("email") or "").strip()
                or await _detect_email(page)
                or _guess_email_from_cookies(useful)
            )
            if not email and page_sess.get("ok"):
                email = await _fetch_email_from_token(str(page_sess.get("flowKey") or ""))
            pid = slot.profile_id()
            ensure_profile_row(pid, profile_label=slot.label or slot.id, email=email or "")
            if email:
                _attach_email_to_slot(slot.id, email, profile_id=pid)

            return {
                "ok": True,
                "slot_id": slot.id,
                "profile_id": pid,
                "email": email,
                "cookies_saved": False,
                "token_refreshed": False,
                "message": (
                    f"Đã lưu profile {slot.id}"
                    + (f" · {email}" if email else " · chưa thấy email (login xong chờ rồi Refresh)")
                    + " · chưa lấy cookies (dùng Sync / Auto khi generate)"
                ),
            }
        return {"ok": False, "error": "attach_failed"}


async def _persist_session_from_page(slot, context, page) -> dict[str, Any]:
    """Read cookies + auth/session from an already-attached Flow tab and save to DB."""
    from flow2api.services.cookie_service import save_profile_cookies
    from flow2api.services.cookie_token_service import refresh_access_token_from_cookies
    from flow2api.services.extension_pool import get_extension_pool
    from flow2api.services.flow_profile_service import (
        ensure_profile_row,
        get_stored_access_token,
        save_access_token,
    )

    cookies = await context.cookies()
    useful = [
        c
        for c in cookies
        if isinstance(c, dict)
        and (
            "google" in str(c.get("domain") or "").lower()
            or "labs" in str(c.get("domain") or "").lower()
        )
    ]
    if not useful:
        useful = [c for c in cookies if isinstance(c, dict)]
    has_session = _has_flow_session_cookie(useful)

    page_sess = await _auth_session_from_page(page)
    email = (
        str(page_sess.get("email") or "").strip()
        or await _detect_email(page)
        or _guess_email_from_cookies(useful)
    )
    pid = slot.profile_id()
    ensure_profile_row(pid, profile_label=slot.label or slot.id, email=email or "")
    if useful:
        save_profile_cookies(pid, useful)

    token_ok = False
    token_err = None
    token = ""
    expires_at = None
    if page_sess.get("ok"):
        token_ok = True
        token = str(page_sess.get("flowKey") or "").strip()
        expires_at = page_sess.get("expiresAt")
    else:
        token_err = str(page_sess.get("error") or "AUTH_SESSION_EMPTY")
        try:
            tok = await refresh_access_token_from_cookies(pid, force=True)
            token_ok = bool(tok and tok.get("ok"))
            if token_ok:
                token = str(
                    tok.get("flowKey") or get_stored_access_token(pid) or ""
                ).strip()
                expires_at = tok.get("expiresAt")
                token_err = None
            else:
                token_err = str((tok or {}).get("error") or token_err)
        except Exception as exc:
            token_err = str(exc)
            logger.warning(
                "refresh token from CDP cookies failed slot=%s: %s", slot.id, exc
            )

    if not email:
        token = token or (get_stored_access_token(pid) or "")
        if token:
            email = await _fetch_email_from_token(token)

    if email:
        _attach_email_to_slot(slot.id, email, profile_id=pid)
    if token_ok and token:
        save_access_token(
            pid,
            token,
            profile_label=email or (slot.label or slot.id),
            email=email or "",
            expires_at=expires_at,
        )

    try:
        get_extension_pool().hydrate_db_profiles()
    except Exception:
        pass

    if token_ok and email:
        msg = (
            f"Đã sync session slot {slot.id} · {email}"
            f" · {len(useful)} cookies · token OK"
        )
    elif token_ok:
        msg = (
            f"Đã sync session slot {slot.id} · {len(useful)} cookies"
            " · token OK · chưa thấy email"
        )
    else:
        msg = _sync_not_logged_in_message(
            slot.id, cookies_n=len(useful), has_session=has_session
        )
        if token_err:
            msg += f" ({token_err})"

    return {
        "ok": True,
        "slot_id": slot.id,
        "profile_id": pid,
        "email": email,
        "cookies_count": len(useful),
        "has_flow_session_cookie": has_session,
        "token_refreshed": token_ok,
        "token_error": token_err,
        "message": msg,
    }


def _url_looks_like_flow(url: str) -> bool:
    cur = str(url or "").lower()
    return "labs.google" in cur or "flow" in cur


async def sync_session(
    slot_id: str,
    *,
    page=None,
    context=None,
    skip_goto: bool = False,
) -> dict[str, Any]:
    """Export cookies from CDP → FlowProfile DB so Direct HTTP / offline gen can use them.

    Pass an already-attached Playwright ``page`` (auto cycle) to skip a second
    connect_over_cdp + reload — that was the slowest extra hop.
    """
    if page is not None:
        slot = get_flow_cdp_slot(slot_id)
        if not slot:
            return {"ok": False, "error": "slot_not_found"}
        ctx = context if context is not None else page.context
        if not skip_goto:
            try:
                await page.goto(_FLOW_START, wait_until="commit", timeout=45_000)
            except Exception as exc:
                logger.warning("goto flow failed slot=%s: %s", slot.id, exc)
            await _wait_flow_ready(page)
        else:
            try:
                has_sess = _has_flow_session_cookie(await ctx.cookies())
            except Exception:
                has_sess = False
            if not has_sess:
                await _wait_flow_ready(page, timeout_ms=6_000)
        return await _persist_session_from_page(slot, ctx, page)

    async with _slot_lock(slot_id):
        async for slot, _browser, ctx, attached in _agen_page(slot_id):
            try:
                await attached.goto(_FLOW_START, wait_until="commit", timeout=45_000)
            except Exception as exc:
                logger.warning("goto flow failed slot=%s: %s", slot.id, exc)
            await _wait_flow_ready(page=attached)
            return await _persist_session_from_page(slot, ctx, attached)
        return {"ok": False, "error": "attach_failed"}


async def auto_attach_emails(*, only_missing: bool = True, force: bool = False) -> dict[str, Any]:
    """For live CDP slots, attach logged-in email + profile only (no cookie/token sync)."""
    attached: list[dict[str, Any]] = []
    skipped: list[str] = []
    errors: list[dict[str, Any]] = []
    async with _AUTO_ATTACH_LOCK:
        now = time.time()
        for slot in list_flow_cdp_slots():
            if not system_ops.cdp_endpoint_alive(slot.cdp_url()):
                skipped.append(slot.id)
                continue
            if only_missing and str(slot.email or "").strip() and not force:
                skipped.append(slot.id)
                continue
            last = _AUTO_ATTACH_TS.get(slot.id, 0.0)
            if not force and (now - last) < _AUTO_ATTACH_COOLDOWN_S:
                skipped.append(slot.id)
                continue
            _AUTO_ATTACH_TS[slot.id] = now
            try:
                result = await attach_email_only(slot.id)
                attached.append(
                    {
                        "slot_id": slot.id,
                        "email": result.get("email") or "",
                        "ok": bool(result.get("ok")),
                        "message": result.get("message"),
                    }
                )
            except Exception as exc:
                if _is_cdp_unreachable_error(exc):
                    # CDP tắt giữa lúc probe/attach — bình thường, không spam WARNING
                    logger.debug(
                        "auto_attach skip slot=%s (CDP tắt): %s", slot.id, exc
                    )
                    skipped.append(slot.id)
                else:
                    logger.warning("auto_attach email failed slot=%s: %s", slot.id, exc)
                    errors.append({"slot_id": slot.id, "error": str(exc)})
    return {
        "ok": True,
        "attached": attached,
        "skipped": skipped,
        "errors": errors,
        "profiles": flow_cdp_public_status(),
    }


async def schedule_auto_attach(slot_id: str, *, attempts: int = 4, delay_s: float = 8.0) -> None:
    """Background: retry email/profile attach after login — never auto-sync cookies."""
    sid = str(slot_id or "").strip()
    if not sid:
        return

    async def _run() -> None:
        for i in range(max(1, attempts)):
            await asyncio.sleep(delay_s)
            slot = get_flow_cdp_slot(sid)
            if not slot:
                return
            if str(slot.email or "").strip():
                return
            if not system_ops.cdp_endpoint_alive(slot.cdp_url()):
                # CDP đã tắt (user đóng cửa sổ) — dừng hẳn, không retry spam
                logger.debug("schedule_auto_attach stop slot=%s — CDP tắt", sid)
                return
            try:
                result = await attach_email_only(sid)
                if str(result.get("email") or "").strip():
                    logger.info(
                        "auto-attached email for CDP %s: %s", sid, result.get("email")
                    )
                    return
            except Exception as exc:
                if _is_cdp_unreachable_error(exc):
                    logger.debug(
                        "schedule_auto_attach stop slot=%s — CDP tắt: %s", sid, exc
                    )
                    return
                logger.debug(
                    "schedule_auto_attach attempt %s slot=%s: %s", i + 1, sid, exc
                )

    asyncio.create_task(_run())


async def click_selector(slot_id: str, selector: str, *, timeout_ms: int = 15_000) -> dict[str, Any]:
    """Generic click helper for future automation (buttons on Flow UI)."""
    sel = str(selector or "").strip()
    if not sel:
        return {"ok": False, "error": "missing_selector"}
    async with _slot_lock(slot_id):
        async for slot, _b, _c, page in _agen_page(slot_id):
            await page.click(sel, timeout=timeout_ms)
            return {
                "ok": True,
                "slot_id": slot.id,
                "selector": sel,
                "url": page.url,
                "message": f"Đã click `{sel}` trên slot {slot.id}",
            }
        return {"ok": False, "error": "attach_failed"}
