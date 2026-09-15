"""Flow generate via batchexecute (flow.google.com) — fallback lane when access_token
(OAuth ya29) is unavailable, which is now the normal case since Google moved Flow off
labs.google's NextAuth session and stopped issuing OAuth access tokens for it.

How it works (verified empirically, see project notes):
- aisandbox-pa.googleapis.com REST API is still alive but requires a real OAuth
  access_token — and Flow no longer issues one anywhere (not in localStorage,
  sessionStorage, IndexedDB, or via the old labs.google auth/session endpoint).
- The only living path is POSTing to flow.google.com's own
  /_/AiSandboxAngularFrontend/data/batchexecute endpoint, authenticated purely by
  browser session cookies plus a per-page f.sid/bl/at triple read from
  window.WIZ_global_data, and a reCAPTCHA Enterprise token in the payload.
- The reCAPTCHA token is minted via grecaptcha.enterprise.execute() using the
  real action Flow's own production code uses for this call ("IMAGE_GENERATION"
  — see extension_pool.py's _captcha_action_for_url), clearing `_grecaptcha*`
  localStorage before and after (same as the Captcha Center extension's
  injected.js does). Using an arbitrary action string instead ("generate") gets
  flagged by Google's risk engine (PUBLIC_ERROR_UNUSUAL_ACTIVITY) even with an
  otherwise-correct payload.
- Empirically, the reCAPTCHA token is NOT bound to the account that minted it —
  a token minted under one Google account's session was accepted for a request
  authenticated by a completely different account's cookies/f.sid/at. So one
  shared "Captcha Center" CDP tab (flow_captcha_center.py) can mint tokens for
  every Gen profile.
- The f.sid/bl/at triple IS bound to the account/session (mixing it with another
  account's cookies gets HTTP 400), but is NOT single-use — the same triple was
  reused successfully across multiple generate calls with only a fresh reCAPTCHA
  token each time. It changes on every page load, but does not need to be
  re-read from a live tab before every call: it can be captured once (via
  capture_batchexecute_session, using a temporarily-opened CDP tab) and cached
  in the DB (flow_profile_service.save_batchexecute_session), letting Gen's CDP
  stay closed for actual generation. When Google eventually invalidates a cached
  triple, gen_image_via_batchexecute raises BatchExecuteError and the caller is
  expected to re-run capture_batchexecute_session.
- The whole request (including reCAPTCHA token minting) can be built and sent
  without touching the page's UI at all — no typing into the prompt box, no
  clicking Generate.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.parse
import uuid
from typing import Any

from flow2api.services import system_ops
from flow2api.services.cookie_service import get_stored_cookie_header
from flow2api.services.flow_cdp_settings import get_flow_cdp_slot

logger = logging.getLogger(__name__)

# httpx's own request logger prints the full URL on every call (rpcids/bl/
# f.sid/_reqid query string, very long) — muted so the short label logged
# below (e.g. "video-start-img -> HTTP 200") is what shows up instead.
logging.getLogger("httpx").setLevel(logging.WARNING)

_FLOW_PROJECT_URL_RE = re.compile(r"/project/([0-9a-fA-F-]{36})")

# A profile can have many tasks running concurrently (e.g. several gen_image
# jobs in parallel). If the cached session expires, every one of them hits a
# 401 at roughly the same moment — without coordination they'd each try to
# open their own CDP connection to the same Chrome tab simultaneously, which
# is exactly the "Connection closed while reading from the driver" hang seen
# in practice when multiple connect_over_cdp calls raced each other. See
# recapture_session_coalesced (defined after capture_batchexecute_session,
# further down this file) — it's the entry point every caller should use
# instead of calling capture_batchexecute_session directly when recovering
# from a 401, precisely to avoid that race.
_session_recapture_locks: dict[str, asyncio.Lock] = {}
_last_recapture_at: dict[str, float] = {}
_RECAPTURE_COALESCE_WINDOW_S = 30.0


def _recapture_lock(profile_id: str) -> asyncio.Lock:
    lock = _session_recapture_locks.get(profile_id)
    if lock is None:
        lock = asyncio.Lock()
        _session_recapture_locks[profile_id] = lock
    return lock

# rpcid + reCAPTCHA action for image generation (Nano Banana etc). Discovered
# empirically from a real batchexecute call captured in Chrome DevTools; action
# string matches extension_pool.py's _captcha_action_for_url for the REST
# equivalent endpoint (batchGenerateImages).
RPCID_GEN_IMAGE = "ogiZ0b"
CAPTCHA_ACTION_IMAGE = "IMAGE_GENERATION"

# rpcid for uploading a reference image (used before gen_image/gen_video calls
# that take an uploaded image as input). Also discovered empirically. Action
# string mirrors _captcha_action_for_url's mapping for the REST uploadImage
# equivalent, which shares the IMAGE_GENERATION bucket.
RPCID_UPLOAD_IMAGE = "maseQ"
CAPTCHA_ACTION_UPLOAD_IMAGE = "IMAGE_GENERATION"

# rpcid to resolve a media id into its signed CDN URL — needed after upload,
# which only ever returns a media id, never a direct URL (unlike generate,
# which returns both in the same response). Discovered empirically; does not
# appear to need a reCAPTCHA token (read-only lookup, not a generation action).
RPCID_GET_MEDIA_URL = "as29s"

# rpcid for reference-to-video generation ("Video Thành Phần" — multiple
# reference images, no start/end frame). Discovered empirically. Action string
# assumed to mirror _captcha_action_for_url's VIDEO_GENERATION bucket for the
# REST batchAsyncGenerateVideoReferenceImages equivalent.
RPCID_GEN_R2V_VIDEO = "MZZa6b"
CAPTCHA_ACTION_VIDEO = "VIDEO_GENERATION"

# Duration is encoded directly in the model key, not a separate field —
# confirmed by capturing real Generate clicks on Flow's own UI with a live
# CDP hook: 4s -> "veo_3_1_t2v_lite_4s_low_priority", 6s ->
# "..._lite_6s_low_priority". The plain "..._lite_low_priority" key (no
# duration segment) is what the UI sends for its default, 8s.
_VIDEO_DURATION_MODEL_BASE = {
    "t2v": "veo_3_1_t2v_lite",
    "i2v": "veo_3_1_i2v_lite",
    "r2v": "veo_3_1_r2v_lite",
    "i2v_fl": "veo_3_1_interpolation_lite",
}
_VIDEO_DURATIONS_WITH_SUFFIX = (4, 5, 6, 7)  # 8s is the bare "..._lite_low_priority" default


def video_duration_model_key(mode: str, duration_s: int | None) -> str:
    base = _VIDEO_DURATION_MODEL_BASE.get(mode)
    if not base:
        raise BatchExecuteError(f"unknown_video_mode_for_duration: {mode}")
    dur = int(duration_s or 8)
    if dur in _VIDEO_DURATIONS_WITH_SUFFIX:
        return f"{base}_{dur}s_low_priority"
    return f"{base}_low_priority"

# rpcid for text-to-video generation (no images at all). Discovered
# empirically — same response shape as r2v, and the request item is the exact
# same shape minus the reference-images field (see _build_gen_t2v_video_params).
RPCID_GEN_T2V_VIDEO = "YhhmEf"

# rpcid for image-to-video generation (single start-frame image). Discovered
# empirically. Same response shape as t2v/r2v.
RPCID_GEN_I2V_VIDEO = "eb1hJf"

# rpcid for start+end frame video generation ("interpolation"). Discovered
# empirically. Same response shape as the other video rpcids.
RPCID_GEN_I2V_FL_VIDEO = "nprQif"

# rpcid to upsample an existing video to 1080p. Discovered empirically. The
# resulting media id is the source id with "_upsampled" appended (not a new
# random UUID like the other video types) — poll_video_via_batchexecute and
# resolve_media_url_via_batchexecute both work on it unchanged.
RPCID_UPSAMPLE_VIDEO = "p0UkFb"
UPSAMPLE_MODEL_KEY = "veo_3_1_upsampler_1080p"

# rpcid to upsample an existing image to 2K/4K. Discovered empirically —
# unlike every other rpcid here, the response contains the upscaled image's
# base64 data directly (no media id + separate poll/resolve step). The
# "factor" field's meaning was confirmed by comparing two real captures with
# different output sizes: factor=1 -> 2K (~3MB jpeg), factor=2 -> 4K (~10.5MB
# jpeg) for the same source image.
RPCID_UPSAMPLE_IMAGE = "SPrCad"
IMAGE_UPSAMPLE_FACTOR = {"2k": 1, "4k": 2}

# rpcid to poll a submitted video generation's status by media id. Does not
# need a reCAPTCHA token (read-only, like RPCID_GET_MEDIA_URL). Status codes
# observed empirically: [2] = still processing, [3] = done (ready to resolve
# via RPCID_GET_MEDIA_URL for the final video URL).
RPCID_POLL_VIDEO = "jwpduf"
_VIDEO_STATUS_DONE = 3

# rpcid for Gemini text generation (the "Prop Writer" / screenplay tool at
# /project/<id>/tool/<appletId>?mode=APP). Discovered empirically. Action
# string assumed to mirror _captcha_action_for_url's bucket for the REST
# /v1/flow:generateContent equivalent.
RPCID_GEN_TEXT = "agJzFb"
CAPTCHA_ACTION_TEXT = "TEXT_GENERATION"
DEFAULT_TEXT_MODEL_BE = "gemini-3-flash-preview"

IMAGE_ASPECT_CODE = {
    "16:9": 3,
    "4:3": 4,
    "1:1": 1,
    "3:4": 5,
    "9:16": 2,
}

# Aspect code cho video (slot thứ 3 của item trong RPCID_GEN_*_VIDEO) — verified
# từ live capture: t2v 9:16 -> item[2]=1, t2v 16:9 -> item[2]=2 (khác bộ mã với
# IMAGE_ASPECT_CODE, vốn dùng số riêng cho ảnh).
VIDEO_ASPECT_CODE = {
    "16:9": 2,
    "9:16": 1,
}

_BATCHEXECUTE_URL = "https://flow.google.com/_/AiSandboxAngularFrontend/data/batchexecute"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)

# Tên ngắn gọn theo rpcid cho log — thay vì httpx tự log nguyên URL dài
# (…/batchexecute?rpcids=…&source-path=…&bl=…&f.sid=…&_reqid=…). Đặt ở đây
# thay vì cạnh từng RPCID_* vì dùng để tra ngược tại log-time, sau khi mọi
# hằng số đã định nghĩa.
_RPCID_LABELS = {
    RPCID_GEN_IMAGE: "image-generate",
    RPCID_UPLOAD_IMAGE: "image-upload",
    RPCID_GET_MEDIA_URL: "media-resolve-url",
    RPCID_GEN_R2V_VIDEO: "video-reference",
    RPCID_GEN_T2V_VIDEO: "video-text",
    RPCID_GEN_I2V_VIDEO: "video-start-img",
    RPCID_GEN_I2V_FL_VIDEO: "video-start-end-img",
    RPCID_UPSAMPLE_VIDEO: "video-upsample",
    RPCID_UPSAMPLE_IMAGE: "image-upsample",
    RPCID_POLL_VIDEO: "video-poll",
    RPCID_GEN_TEXT: "text-generate",
}


def _rpcid_label(rpcid: str) -> str:
    return _RPCID_LABELS.get(rpcid, rpcid)


class BatchExecuteError(RuntimeError):
    pass


class BatchExecuteSessionRecoveryFailed(BatchExecuteError):
    """Raised when a 401 (expired session) could not be recovered — the
    profile's CDP tab isn't reachable, so recapture_session_coalesced itself
    failed. Distinct from a plain BatchExecuteError so processor.py can treat
    this as an account-level problem (switch to another profile) rather than
    a task-level failure to just report and give up on."""


def _require_playwright():
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        raise BatchExecuteError(
            "playwright_not_installed — pip install playwright && playwright install chrome"
        ) from exc


def _extract_project_id(url: str) -> str | None:
    m = _FLOW_PROJECT_URL_RE.search(url or "")
    return m.group(1) if m else None


async def _ensure_flow_cdp_running(profile_id: str, cdp: str) -> None:
    """Launch Chrome for this profile's CDP slot if it isn't already running
    (e.g. the profile isn't under CDP Auto and has no Chrome up yet) — brings
    it up on demand instead of requiring it be opened by hand. Session capture
    no longer closes Chrome afterwards (see capture_batchexecute_session), so
    this mainly matters the first time a profile is used.
    launch_flow_cdp_slot is a blocking sync call, so it runs off-thread."""
    if system_ops.cdp_endpoint_alive(cdp):
        return
    result = await asyncio.to_thread(system_ops.launch_flow_cdp_slot, profile_id)
    if not result.get("ok"):
        raise BatchExecuteError(
            f"cdp_launch_failed: {profile_id}: {result.get('message') or result.get('error')}"
        )
    for _ in range(40):
        if system_ops.cdp_endpoint_alive(cdp):
            return
        await asyncio.sleep(0.25)
    raise BatchExecuteError(f"cdp_launch_timeout: {profile_id} ({cdp})")


async def _attach_flow_page(profile_id: str):
    """Return (playwright, browser, context, page) attached to the profile's CDP slot,
    with the page already sitting on a Flow project (not the landing page) and
    freshly reloaded so window.WIZ_global_data holds a just-minted fsid/bl/at
    rather than whatever was left over from the tab's last navigation.

    Chrome for this profile may currently be closed if it isn't under CDP
    Auto — this launches it on demand, and if the tab that comes up is
    sitting on the bare flow.google.com landing page rather than a project,
    navigates it to the last known project id cached in the DB for this
    profile."""
    from playwright.async_api import async_playwright

    slot = get_flow_cdp_slot(profile_id)
    if not slot:
        raise BatchExecuteError(f"cdp_slot_not_found: {profile_id}")
    cdp = slot.cdp_url()
    await _ensure_flow_cdp_running(profile_id, cdp)

    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.connect_over_cdp(cdp)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        pages = [p for p in context.pages if "flow.google.com" in (p.url or "")]
        page = pages[0] if pages else (context.pages[0] if context.pages else await context.new_page())
        if "flow.google.com" not in (page.url or ""):
            raise BatchExecuteError(
                f"no_flow_tab: {profile_id} — mở tab flow.google.com đã login trước."
            )
        if _extract_project_id(page.url):
            await page.reload(wait_until="domcontentloaded")
        else:
            from flow2api.services.flow_profile_service import get_batchexecute_session

            cached = get_batchexecute_session(profile_id)
            project_id = (cached or {}).get("project_id")
            if not project_id:
                raise BatchExecuteError(
                    f"no_project_in_tab_url: {page.url} — chưa từng capture session cho profile này để biết project nào."
                )
            await page.goto(f"https://flow.google.com/project/{project_id}", wait_until="domcontentloaded")
        return pw, browser, context, page
    except Exception:
        await pw.stop()
        raise


async def _read_page_session(page) -> dict[str, str]:
    """Read the f.sid / bl / at triple Flow's own JS embeds in the page, used to
    authenticate batchexecute calls alongside the account's session cookies."""
    wiz = await page.evaluate(
        """() => {
            const d = window.WIZ_global_data || {};
            return { fsid: d['FdrFJe'], bl: d['cfb2h'], at: d['SNlM0e'] };
        }"""
    )
    fsid, bl, at = wiz.get("fsid"), wiz.get("bl"), wiz.get("at")
    if not fsid or not bl or not at:
        raise BatchExecuteError(f"missing_wiz_global_data: {wiz}")
    return {"fsid": str(fsid), "bl": str(bl), "at": str(at)}


async def capture_batchexecute_session(profile_id: str) -> dict[str, str]:
    """Launch Chrome for this profile's CDP slot if needed, attach to its Flow
    tab, read its project id + f.sid/bl/at, persist them to the DB. Chrome is
    left running afterwards — CDP Auto (flow_cdp_auto.py) manages its own
    lifecycle (keeps it open, reloads periodically for a fresh fsid), and
    closing it here would fight that. gen_image_via_batchexecute (and
    friends) read the cached session from the DB.

    Also re-saves the browser's current cookies alongside the session triple.
    Cookies and f.sid/at must come from the SAME moment — mixing a fresh f.sid/at
    with a stale cookie snapshot (e.g. captured hours earlier by a different
    flow) gets rejected with HTTP 401, since Google validates them together as
    one session.
    """
    _require_playwright()
    from flow2api.services.cookie_service import save_profile_cookies
    from flow2api.services.flow_profile_service import save_batchexecute_session

    pw = None
    try:
        pw, _browser, context, page = await _attach_flow_page(profile_id)
        project_id = _extract_project_id(page.url)
        if not project_id:
            raise BatchExecuteError(f"no_project_in_tab_url: {page.url}")
        session = await _read_page_session(page)

        cookies = await context.cookies()
        if cookies:
            save_profile_cookies(profile_id, cookies)

        save_batchexecute_session(
            profile_id,
            project_id=project_id,
            fsid=session["fsid"],
            bl=session["bl"],
            at=session["at"],
        )
        return {"project_id": project_id, **session}
    finally:
        # Không đóng CDP sau capture — CDP Auto (flow_cdp_auto.py) giữ Chrome
        # mở liên tục và tự reload lấy fsid mới định kỳ; đóng CDP ở đây (như
        # trước) xung đột với model đó, khiến Gen tưởng như "tự tắt CDP" mỗi
        # khi một request gặp 401 và trigger recapture.
        if pw is not None:
            try:
                await pw.stop()
            except Exception:
                pass


async def recapture_session_coalesced(profile_id: str) -> dict[str, str]:
    """Re-capture profile_id's batchexecute session, coalescing concurrent
    callers so only one of them actually opens a CDP connection — this is
    what every caller recovering from a 401 should use instead of calling
    capture_batchexecute_session directly (see the module-level comment near
    _session_recapture_locks for why: many tasks on the same profile can hit
    401 within moments of each other, and racing connect_over_cdp calls hang).

    If another caller already re-captured the session very recently (within
    _RECAPTURE_COALESCE_WINDOW_S), skip re-capturing again and just return the
    session that's already cached — it's almost certainly the fresh one that
    caller just fetched, not the stale one that caused the original 401s.
    """
    from flow2api.services.flow_profile_service import get_batchexecute_session

    async with _recapture_lock(profile_id):
        cached = get_batchexecute_session(profile_id)
        now = time.time()
        last = _last_recapture_at.get(profile_id, 0.0)
        if cached and (now - last) < _RECAPTURE_COALESCE_WINDOW_S:
            return cached
        result = await capture_batchexecute_session(profile_id)
        _last_recapture_at[profile_id] = time.time()
        return result


# Back-compat name used by worker/processor.py to get a project id without an
# access_token (labs.google's tRPC project.createProject needs the same dead
# OAuth as the REST API). Capturing the full session also gets us the id.
async def get_project_id_from_cdp_tab(profile_id: str) -> str:
    session = await capture_batchexecute_session(profile_id)
    return session["project_id"]


_TRANSPORT_RETRY_ATTEMPTS = 10
_TRANSPORT_RETRY_BACKOFF_S = 3.0
# 1 lần fetch() trong tab không được treo vô hạn nếu mạng của Chrome đó chết —
# giữ ngắn để 1 attempt fail nhanh thay vì đợi cả timeout_s.
_FETCH_TIMEOUT_S = 20.0

# Gửi batchexecute request qua chính tab CDP của profile (page.evaluate +
# fetch trong ngữ cảnh trang) thay vì httpx riêng của Python — dùng đúng
# network stack + cookie của Chrome thật, tránh lệch route/DNS/TLS giữa máy
# chủ Python và trình duyệt. Đổi lại: cần CDP của profile đang mở sẵn (CDP
# Auto giữ) — không tự launch Chrome mới cho mỗi request, và mỗi tab xử lý
# JS/fetch tuần tự nên kém song song hơn connection pool httpx cũ.
#
# 1 kết nối playwright (browser + page) được cache và tái sử dụng cho mỗi
# profile giữa nhiều lần gọi — poll_video_via_batchexecute một mình có thể
# gọi hàng chục lần cho 1 video, mở/đóng connect_over_cdp mỗi lần sẽ rất chậm.
_cdp_page_cache: dict[str, tuple[Any, Any, Any]] = {}  # profile_id -> (playwright, browser, page)
_cdp_page_cache_locks: dict[str, asyncio.Lock] = {}


def _cdp_page_cache_lock(profile_id: str) -> asyncio.Lock:
    lock = _cdp_page_cache_locks.get(profile_id)
    if lock is None:
        lock = asyncio.Lock()
        _cdp_page_cache_locks[profile_id] = lock
    return lock


async def _get_cdp_page_for_profile(profile_id: str):
    """Trả về Playwright Page đã attach sẵn vào tab flow.google.com CDP của
    profile này — tái sử dụng kết nối đã cache nếu còn sống, không reload/
    navigate (khác _attach_flow_page, dùng cho capture session). Không tự
    launch Chrome — nếu CDP không mở/không sẵn sàng thì báo lỗi rõ ràng để
    caller biết cần mở CDP (CDP Auto hoặc tay) trước."""
    async with _cdp_page_cache_lock(profile_id):
        cached = _cdp_page_cache.get(profile_id)
        if cached is not None:
            _pw, _browser, page = cached
            try:
                if not page.is_closed():
                    return page
            except Exception:
                pass
            _cdp_page_cache.pop(profile_id, None)
            try:
                await _pw.stop()
            except Exception:
                pass

        from playwright.async_api import async_playwright

        slot = get_flow_cdp_slot(profile_id)
        if not slot:
            raise BatchExecuteError(f"cdp_slot_not_found: {profile_id}")
        cdp = slot.cdp_url()
        if not system_ops.cdp_endpoint_alive(cdp):
            raise BatchExecuteError(
                f"cdp_not_running: {profile_id} — mở CDP cho profile này trước "
                "(CDP Auto hoặc mở tay), không tự launch cho mỗi request."
            )

        pw = await async_playwright().start()
        try:
            browser = await pw.chromium.connect_over_cdp(cdp)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            pages = [p for p in context.pages if "flow.google.com" in (p.url or "")]
            page = pages[0] if pages else None
            if page is None:
                raise BatchExecuteError(
                    f"no_flow_tab: {profile_id} — chưa có tab flow.google.com đang mở."
                )
        except Exception:
            await pw.stop()
            raise
        _cdp_page_cache[profile_id] = (pw, browser, page)
        return page


async def _invalidate_cdp_page_cache(profile_id: str) -> None:
    """Bỏ kết nối đã cache cho profile này — gọi khi phát hiện nó đã chết
    (tab đóng, browser disconnect) để lần gọi kế tiếp attach lại từ đầu."""
    async with _cdp_page_cache_lock(profile_id):
        cached = _cdp_page_cache.pop(profile_id, None)
        if cached is None:
            return
        pw, _browser, _page = cached
        try:
            await pw.stop()
        except Exception:
            pass


class _CdpFetchError(RuntimeError):
    """fetch() trong tab thất bại vì lý do mạng (không phải lỗi HTTP status) —
    tương đương httpx.TransportError/TimeoutException, được retry loop bắt
    giống như cũ."""


async def _post_batchexecute_http(
    *,
    profile_id: str,
    cookie_header: str,
    rpcid: str,
    project_id: str,
    session: dict[str, str],
    params_json: str,
    timeout_s: float = 60.0,
    log_each_call: bool = True,
    applet_id: str = "",
) -> tuple[int, str]:
    """Send the batchexecute request via page.evaluate(fetch(...)) inside the
    profile's own CDP tab — uses Chrome's real network stack and the tab's
    own cookies (browsers won't let fetch() set a Cookie header manually, so
    `cookie_header` is unused here; kept in the signature since some callers
    still pass it for logging/back-compat) instead of a separate Python HTTP
    client. Avoids any mismatch between the Python process's network route
    and the browser's.

    Retries a few times on bare network/transport failures (ReadError,
    ConnectError, timeouts — a dropped connection or transient DNS/TLS hiccup
    talking to Google, not an account/auth problem) since those otherwise crash
    the whole task uncleanly with an unhelpful bare exception-class name on the
    dashboard (see worker/processor.py's "task died uncleanly" safety net).
    Does not retry HTTP error status codes — those are handled by callers
    inspecting `status` (401 triggers a session re-capture, etc).
    """
    del cookie_header  # browser tự gắn cookie của tab — không set thủ công được qua fetch()

    label = _rpcid_label(rpcid)
    reqid = str(int(time.time() * 1000) % 9_000_000 + 1_000_000)
    source_path = f"/project/{project_id}"
    if applet_id:
        # Applet-scoped rpcs (e.g. agJzFb/gen_text "Prop Writer" tool) are
        # called from a /project/<id>/tool/<appletId>?mode=APP page — Google
        # rejects (rpc_error [3]) without the /tool/<appletId> suffix here,
        # verified from a live capture.
        source_path += f"/tool/{applet_id}"
    url = (
        f"{_BATCHEXECUTE_URL}?rpcids={rpcid}"
        f"&source-path={urllib.parse.quote(source_path)}"
        f"&bl={urllib.parse.quote(session['bl'])}&f.sid={urllib.parse.quote(session['fsid'])}"
        f"&hl=en-US&_reqid={reqid}&rt=c"
    )
    freq = json.dumps(
        [[[rpcid, params_json, None, "generic"]]], separators=(",", ":"), ensure_ascii=False
    )
    body = f"f.req={urllib.parse.quote(freq)}&at={urllib.parse.quote(session['at'])}&"

    # fetch() chạy trong ngữ cảnh trang flow.google.com nên browser tự thêm
    # cookie/origin/referer/user-agent đúng — chỉ cần set content-type và
    # x-same-domain giống Flow's own JS làm.
    fetch_js = """
        async ({url, body, timeoutMs}) => {
            const ctrl = new AbortController();
            const t = setTimeout(() => ctrl.abort(), timeoutMs);
            try {
                const resp = await fetch(url, {
                    method: 'POST',
                    credentials: 'include',
                    headers: {
                        'content-type': 'application/x-www-form-urlencoded;charset=UTF-8',
                        'x-same-domain': '1',
                    },
                    body,
                    signal: ctrl.signal,
                });
                const text = await resp.text();
                return {ok: true, status: resp.status, text};
            } catch (e) {
                return {ok: false, error: String((e && e.name) || 'FetchError') + ': ' + String((e && e.message) || e)};
            } finally {
                clearTimeout(t);
            }
        }
    """

    last_exc: Exception | None = None
    for attempt in range(1, _TRANSPORT_RETRY_ATTEMPTS + 1):
        try:
            page = await _get_cdp_page_for_profile(profile_id)
            result = await page.evaluate(
                fetch_js,
                {"url": url, "body": body, "timeoutMs": int(_FETCH_TIMEOUT_S * 1000)},
            )
            if not result.get("ok"):
                raise _CdpFetchError(str(result.get("error") or "unknown_fetch_error"))
            status = int(result["status"])
            text = str(result.get("text") or "")
            if log_each_call:
                logger.info("batchexecute %s -> HTTP %s", label, status)
            else:
                # video-poll gọi lặp lại mỗi vài giây tới khi xong — logger
                # riêng của caller (poll_video_via_batchexecute) tóm tắt 1
                # dòng khi kết thúc thay vì spam 1 dòng mỗi lần poll.
                logger.debug("batchexecute %s -> HTTP %s", label, status)
            return status, text
        except _CdpFetchError as exc:
            last_exc = exc
        except BatchExecuteError:
            raise
        except Exception as exc:
            # Playwright/CDP-level failure (tab đóng, browser disconnect, target
            # crashed...) — coi như connection chết, bỏ cache để lần retry kế
            # tiếp attach lại từ đầu thay vì lặp lại lỗi tương tự.
            last_exc = exc
            await _invalidate_cdp_page_cache(profile_id)
        if attempt >= _TRANSPORT_RETRY_ATTEMPTS:
            break
        logger.warning(
            "batchexecute transport error (attempt %s/%s): %s — retry sau %.1fs",
            attempt,
            _TRANSPORT_RETRY_ATTEMPTS,
            type(last_exc).__name__,
            _TRANSPORT_RETRY_BACKOFF_S,
        )
        await asyncio.sleep(_TRANSPORT_RETRY_BACKOFF_S)
    assert last_exc is not None
    raise last_exc


def _decode_batchexecute_response(body_text: str, rpcid: str) -> dict[str, Any]:
    """Parse the batchexecute wire format and return the inner rpc payload for
    `rpcid`, or an error description on failure."""
    lines = [l for l in body_text.splitlines() if l.strip()]
    payload_lines = [l for l in lines if l.strip() not in (")]}'",)]
    for line in payload_lines:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, list):
            continue
        for entry in data:
            if not (
                isinstance(entry, list)
                and len(entry) >= 2
                and entry[0] == "wrb.fr"
                and entry[1] == rpcid
            ):
                continue
            if len(entry) >= 3 and isinstance(entry[2], str):
                try:
                    return {"ok": True, "data": json.loads(entry[2])}
                except json.JSONDecodeError:
                    return {"ok": True, "data_raw": entry[2]}
            # entry[2] is null -> error is in entry[5]
            error_info = entry[5] if len(entry) > 5 else None
            return {"ok": False, "error": error_info}
    return {"ok": False, "error": "unrecognized_response_format", "raw": body_text[:500]}


def _image_result_from_ogiZ0b(decoded: list[Any]) -> dict[str, Any]:
    """Map the decoded ogiZ0b payload to the same REST-style shape flow_sdk.py's
    extract_image_urls/extract_image_media_ids expect:
    {"media": [{"image": {"generatedImage": {"fifeUrl", "mediaId"}}}]}.

    Real shape (verified from live captures): decoded[0] is a list of items, each
    item = [mediaId, null, sessionId, null, null, null, [fields_wrapper]], where
    fields_wrapper[0][13] is the signed CDN URL.
    """
    media: list[dict[str, Any]] = []
    try:
        items = decoded[0]
        for item in items:
            media_id = item[0]
            fields = item[6][0]
            url = fields[13] if len(fields) > 13 else None
            if media_id or url:
                media.append(
                    {
                        "image": {
                            "generatedImage": {
                                "mediaId": media_id,
                                "fifeUrl": url,
                            }
                        }
                    }
                )
    except (IndexError, TypeError, KeyError) as exc:
        logger.warning("ogiZ0b response shape unexpected: %s", exc)
    return {"media": media}


def _decode_upload_image_response(decoded: list[Any]) -> dict[str, Any]:
    """Map the decoded maseQ payload to {"media_id": str}.

    Real shape (verified from a live capture): decoded[0] is
    [mediaId, projectId, sessionId, ..., [..., width, height]] — the upload
    response only ever returns the media id, never a direct CDN URL (unlike
    generate, which returns both). Callers resolve a URL from the media id via
    a subsequent gen_image call's imageInputs, exactly like the REST lane did.
    """
    try:
        media_id = decoded[0][0]
    except (IndexError, TypeError, KeyError) as exc:
        raise BatchExecuteError(f"upload_response_shape_unexpected: {exc}") from exc
    if not media_id:
        raise BatchExecuteError("upload_response_missing_media_id")
    return {"media_id": str(media_id)}


def _strip_data_url(b64: str) -> str:
    if b64.startswith("data:"):
        return b64.split(",", 1)[-1]
    return b64


def _build_upload_image_params(
    *,
    project_id: str,
    image_base64: str,
    mime_type: str,
    file_name: str,
    recaptcha_token: str,
) -> str:
    uuid1 = str(uuid.uuid4()).upper()
    uuid2 = str(uuid.uuid4()).upper()
    project_ctx = [
        None, 22, None, None, None, project_id,
        None, None, None, None, [recaptcha_token, 1],
    ]
    # [project_ctx, base64Data, mimeType, 1, null, null, null, null, fileName,
    #  null, uuid1, uuid2] — field order verified from a live capture. Google
    # rejects (rpc_error [3]) if base64Data still carries a data: URL prefix —
    # must be the raw base64 payload, same as the REST lane's imageBytes field.
    params = [
        project_ctx, _strip_data_url(image_base64), mime_type, 1,
        None, None, None, None, file_name, None, uuid1, uuid2,
    ]
    return json.dumps(params, separators=(",", ":"), ensure_ascii=False)


async def upload_image_via_batchexecute(
    *,
    profile_id: str,
    project_id: str | None,
    image_base64: str,
    mime_type: str = "image/jpeg",
    file_name: str = "upload.jpg",
) -> str:
    """Upload a reference image via batchexecute and return its media id.

    Requires a cached DB session (see capture_batchexecute_session) — unlike
    gen_image_via_batchexecute this has no live-CDP fallback, since uploads are
    always paired with a generate call on the same profile, which will have
    already captured (or will capture) the session.
    """
    from flow2api.services.flow_captcha_center import mint_captcha_token
    from flow2api.services.flow_profile_service import get_batchexecute_session

    cached = get_batchexecute_session(profile_id)
    if not cached:
        raise BatchExecuteError(
            f"no_cached_session: {profile_id} — chạy capture_batchexecute_session trước."
        )
    cookie_header = get_stored_cookie_header(profile_id)
    if not cookie_header:
        raise BatchExecuteError(f"no_stored_cookies: {profile_id}")

    effective_project_id = project_id or cached["project_id"]
    recaptcha_token = await mint_captcha_token(action=CAPTCHA_ACTION_UPLOAD_IMAGE)
    params_json = _build_upload_image_params(
        project_id=effective_project_id,
        image_base64=image_base64,
        mime_type=mime_type,
        file_name=file_name,
        recaptcha_token=recaptcha_token,
    )
    status, body_text = await _post_batchexecute_http(
        profile_id=profile_id,
        cookie_header=cookie_header,
        rpcid=RPCID_UPLOAD_IMAGE,
        project_id=effective_project_id,
        session=cached,
        params_json=params_json,
        timeout_s=120.0,
    )
    if status != 200:
        raise BatchExecuteError(f"http_{status}: {body_text[:300]}")

    decoded = _decode_batchexecute_response(body_text, RPCID_UPLOAD_IMAGE)
    if not decoded.get("ok"):
        raise BatchExecuteError(f"rpc_error: {decoded.get('error')}")

    result = _decode_upload_image_response(decoded["data"])
    return result["media_id"]


def _decode_get_media_url_response(decoded: Any) -> str | None:
    """Map the decoded as29s payload to its signed CDN URL, if present.

    Same rpcid (as29s) resolves both images and videos, but the URL lives in a
    different slot for each (verified from live captures):
    - image: [mediaId, projectId, genId, "CAE", null, [...,fields[10]=fifeUrl], genId2]
    - video: same prefix, plus a trailing [[...,videoUrl@index8], ...] block
      whose own fifeUrl-shaped field ([5][10]) actually holds a thumbnail image,
      not the video — the real video URL is decoded[7][0][8].
    Google returns error code 5 (NOT_FOUND-ish) if called too soon, before the
    media has finished processing — callers should retry with a short delay.
    """
    try:
        video_url = decoded[7][0][8]
        if video_url:
            return str(video_url)
    except (IndexError, TypeError, KeyError):
        pass
    try:
        fields = decoded[5]
        url = fields[10]
    except (IndexError, TypeError, KeyError):
        return None
    return str(url) if url else None


async def resolve_media_url_via_batchexecute(
    *,
    profile_id: str,
    media_id: str,
    retries: int = 3,
    retry_delay_s: float = 2.0,
) -> str:
    """Resolve a media id (e.g. from upload_image_via_batchexecute) to its
    signed CDN URL. Retries briefly since Google can return an error if the
    media hasn't finished processing yet right after upload.
    """
    import asyncio

    from flow2api.services.flow_profile_service import get_batchexecute_session

    cached = get_batchexecute_session(profile_id)
    if not cached:
        raise BatchExecuteError(
            f"no_cached_session: {profile_id} — chạy capture_batchexecute_session trước."
        )
    cookie_header = get_stored_cookie_header(profile_id)
    if not cookie_header:
        raise BatchExecuteError(f"no_stored_cookies: {profile_id}")

    params_json = json.dumps([media_id], separators=(",", ":"), ensure_ascii=False)

    last_error: str = "unknown_error"
    for attempt in range(max(1, retries)):
        status, body_text = await _post_batchexecute_http(
            profile_id=profile_id,
            cookie_header=cookie_header,
            rpcid=RPCID_GET_MEDIA_URL,
            project_id=cached["project_id"],
            session=cached,
            params_json=params_json,
        )
        if status != 200:
            last_error = f"http_{status}: {body_text[:300]}"
        else:
            decoded = _decode_batchexecute_response(body_text, RPCID_GET_MEDIA_URL)
            if decoded.get("ok"):
                url = _decode_get_media_url_response(decoded["data"])
                if url:
                    return url
                last_error = f"no_url_in_response: {body_text[:300]}"
            else:
                last_error = f"rpc_error: {decoded.get('error')}"
        if attempt < retries - 1:
            await asyncio.sleep(retry_delay_s)

    raise BatchExecuteError(f"resolve_media_url_failed: {last_error}")


def _build_gen_r2v_video_params(
    *,
    project_id: str,
    prompt: str,
    reference_media_ids: list[str],
    model_key: str,
    aspect_ratio: str,
    recaptcha_token: str,
    voice: str | None = None,
) -> str:
    """Build params for RPCID_GEN_R2V_VIDEO ("Video Thành Phần" — reference
    images, no start/end frame). Field order verified from a live capture:
    item = [promptWrapper, [[null, mediaId], ...], modelKey, aspectCode, null,
            [null,null,null,null, uuid1, uuid2]]
    params = [[item], project_ctx, [uuid3, 2]]
    — the reCAPTCHA token lives in project_ctx, NOT nested inside item (unlike
    image generation, where it's duplicated in both places).

    aspectCode uses the same VIDEO_ASPECT_CODE mapping as t2v (16:9 -> 2,
    9:16 -> 1) — previously hard-coded to 2, which silently forced every
    reference-image video to 16:9 regardless of the requested aspect ratio.

    With a voice: verified from a live capture with narration enabled — two
    extra trailing fields appear on item: [..., null, [[voiceName]]], and the
    narration line itself is prefixed into the prompt text as "Thoại : <line>"
    rather than living in its own field (that's what Flow's own UI does when
    you type dialogue with a voice selected — there's no evidence of a
    separate "dialogue text" field independent of the main prompt).
    """
    uuid1 = str(uuid.uuid4()).upper()
    uuid2 = str(uuid.uuid4()).upper()
    uuid3 = str(uuid.uuid4()).upper()
    aspect_code = VIDEO_ASPECT_CODE.get(aspect_ratio, VIDEO_ASPECT_CODE["16:9"])

    project_ctx = [
        None, 22, None, None, None, project_id,
        None, None, None, None, [recaptcha_token, 1],
    ]
    reference_images = [[None, mid] for mid in reference_media_ids]
    item = [
        [None, None, [[[prompt]]]],
        reference_images,
        model_key,
        aspect_code,
        None,
        [None, None, None, None, uuid1, uuid2],
    ]
    if voice:
        item.append(None)
        item.append([[voice]])
    params = [[item], project_ctx, [uuid3, 2]]
    return json.dumps(params, separators=(",", ":"), ensure_ascii=False)


def _decode_video_submit_response(decoded: list[Any]) -> tuple[str, str]:
    """Extract (media_id, generation_id) from a video submit response
    (MZZa6b/YhhmEf/eb1hJf/nprQif/p0UkFb).

    Real shape (verified from a live capture):
    [null, code, [[genId, null, null, [prompt, ts, null, null, mediaId, ...], projectId]],
     [[mediaId, projectId, genId, "CAE", ...]]]
    decoded[3][0][0] is the media id needed for polling/resolving; [0][2] is
    the generation id — a separate value the UI tracks internally, and which
    upsample_video_via_batchexecute needs (not the media id itself).
    """
    try:
        media_id = decoded[3][0][0]
        generation_id = decoded[3][0][2]
    except (IndexError, TypeError, KeyError) as exc:
        raise BatchExecuteError(f"video_submit_response_shape_unexpected: {exc}") from exc
    if not media_id:
        raise BatchExecuteError("video_submit_response_missing_media_id")
    return str(media_id), str(generation_id or "")


async def _submit_video_request(
    *,
    profile_id: str,
    project_id: str | None,
    rpcid: str,
    build_params: Any,
) -> str:
    """Shared submit path for every video rpcid: resolve the cached session,
    mint a reCAPTCHA token, POST, and extract the resulting media id.
    `build_params(effective_project_id, recaptcha_token) -> params_json` lets
    each video type supply its own item shape while sharing everything else.
    """
    from flow2api.services.flow_captcha_center import mint_captcha_token
    from flow2api.services.flow_profile_service import get_batchexecute_session

    cached = get_batchexecute_session(profile_id)
    if not cached:
        raise BatchExecuteError(
            f"no_cached_session: {profile_id} — chạy capture_batchexecute_session trước."
        )
    cookie_header = get_stored_cookie_header(profile_id)
    if not cookie_header:
        raise BatchExecuteError(f"no_stored_cookies: {profile_id}")

    effective_project_id = project_id or cached["project_id"]
    recaptcha_token = await mint_captcha_token(action=CAPTCHA_ACTION_VIDEO)
    params_json = build_params(effective_project_id, recaptcha_token)

    status, body_text = await _post_batchexecute_http(
        profile_id=profile_id,
        cookie_header=cookie_header,
        rpcid=rpcid,
        project_id=effective_project_id,
        session=cached,
        params_json=params_json,
    )
    if status != 200:
        raise BatchExecuteError(f"http_{status}: {body_text[:300]}")

    decoded = _decode_batchexecute_response(body_text, rpcid)
    if not decoded.get("ok"):
        raise BatchExecuteError(f"rpc_error: {decoded.get('error')}")

    media_id, _generation_id = _decode_video_submit_response(decoded["data"])
    return media_id


async def gen_r2v_video_via_batchexecute(
    *,
    profile_id: str,
    project_id: str | None,
    prompt: str,
    reference_media_ids: list[str],
    aspect_ratio: str = "16:9",
    duration_s: int | None = None,
    video_model_key: str | None = None,
    voice: str | None = None,
) -> str:
    """Submit a reference-to-video ("Video Thành Phần") generation request.
    Returns the media id to pass to poll_video_via_batchexecute.

    `duration_s` picks 4/5/6/7/8s via video_duration_model_key (8 is the
    default if omitted) — ignored if `video_model_key` is given explicitly.

    `voice`, when given, must be a Flow voice name lowercased (e.g. "alnilam")
    — same normalization REST's normalize_voice_media_id applies. The prompt
    should already contain the narration line the way Flow's own UI writes it
    (see _build_gen_r2v_video_params's docstring) — this function does not
    inject any "Thoại :" prefix itself, since callers may already be doing
    that consistently with the REST lane's own prompt conventions.
    """
    model_key = video_model_key or video_duration_model_key("r2v", duration_s)
    return await _submit_video_request(
        profile_id=profile_id,
        project_id=project_id,
        rpcid=RPCID_GEN_R2V_VIDEO,
        build_params=lambda pid, token: _build_gen_r2v_video_params(
            project_id=pid,
            prompt=prompt,
            reference_media_ids=reference_media_ids,
            model_key=model_key,
            aspect_ratio=aspect_ratio,
            recaptcha_token=token,
            voice=voice,
        ),
    )


def _build_gen_t2v_video_params(
    *,
    project_id: str,
    prompt: str,
    model_key: str,
    aspect_ratio: str,
    recaptcha_token: str,
) -> str:
    """Build params for RPCID_GEN_T2V_VIDEO (text only, no images). Field order
    verified from a live capture — identical to r2v's item shape minus the
    reference-images field:
    item = [promptWrapper, modelKey, aspectCode, null, [null,null,null,null,uuid1,uuid2]]
    params = [[item], project_ctx, [uuid3, 2]]
    aspectCode confirmed from two live captures: 16:9 -> 2, 9:16 -> 1 (see
    VIDEO_ASPECT_CODE) — previously hard-coded to 2, which silently forced
    every batchexecute video to 16:9 regardless of the requested aspect ratio.
    """
    uuid1 = str(uuid.uuid4()).upper()
    uuid2 = str(uuid.uuid4()).upper()
    uuid3 = str(uuid.uuid4()).upper()
    aspect_code = VIDEO_ASPECT_CODE.get(aspect_ratio, VIDEO_ASPECT_CODE["16:9"])

    project_ctx = [
        None, 22, None, None, None, project_id,
        None, None, None, None, [recaptcha_token, 1],
    ]
    item = [
        [None, None, [[[prompt]]]],
        model_key,
        aspect_code,
        None,
        [None, None, None, None, uuid1, uuid2],
    ]
    params = [[item], project_ctx, [uuid3, 2]]
    return json.dumps(params, separators=(",", ":"), ensure_ascii=False)


async def gen_t2v_video_via_batchexecute(
    *,
    profile_id: str,
    project_id: str | None,
    prompt: str,
    aspect_ratio: str = "16:9",
    duration_s: int | None = None,
    video_model_key: str | None = None,
) -> str:
    """Submit a text-to-video generation request (no reference images).
    Returns the media id to pass to poll_video_via_batchexecute.

    `duration_s` picks 4/5/6/7/8s via video_duration_model_key (8 is the
    default if omitted) — ignored if `video_model_key` is given explicitly.
    """
    model_key = video_model_key or video_duration_model_key("t2v", duration_s)
    return await _submit_video_request(
        profile_id=profile_id,
        project_id=project_id,
        rpcid=RPCID_GEN_T2V_VIDEO,
        build_params=lambda pid, token: _build_gen_t2v_video_params(
            project_id=pid,
            prompt=prompt,
            model_key=model_key,
            aspect_ratio=aspect_ratio,
            recaptcha_token=token,
        ),
    )


def _build_gen_i2v_video_params(
    *,
    project_id: str,
    prompt: str,
    start_media_id: str,
    model_key: str,
    aspect_ratio: str,
    recaptcha_token: str,
) -> str:
    """Build params for RPCID_GEN_I2V_VIDEO (single start-frame image). Field
    order verified from a live capture:
    item = [promptWrapper, modelKey, aspectCode, null,
            [null, startMediaId, null, null, null, cropBox],
            [null,null,null,null, uuid1, uuid2]]
    params = [[item], project_ctx, [uuid3, 2]]
    cropBox is [x0, null, x1, y1]-ish normalized coordinates the UI sends when
    the user crops the start image — omitted here (null) to use the full image,
    same as REST's startImage:{mediaId} with no separate crop field.

    aspectCode uses the same VIDEO_ASPECT_CODE mapping as t2v (16:9 -> 2,
    9:16 -> 1) — previously hard-coded to 2, which silently forced every
    start-image video to 16:9 regardless of the requested aspect ratio.
    """
    uuid1 = str(uuid.uuid4()).upper()
    uuid2 = str(uuid.uuid4()).upper()
    uuid3 = str(uuid.uuid4()).upper()
    aspect_code = VIDEO_ASPECT_CODE.get(aspect_ratio, VIDEO_ASPECT_CODE["16:9"])

    project_ctx = [
        None, 22, None, None, None, project_id,
        None, None, None, None, [recaptcha_token, 1],
    ]
    item = [
        [None, None, [[[prompt]]]],
        model_key,
        aspect_code,
        None,
        [None, start_media_id, None, None, None, None],
        [None, None, None, None, uuid1, uuid2],
    ]
    params = [[item], project_ctx, [uuid3, 2]]
    return json.dumps(params, separators=(",", ":"), ensure_ascii=False)


async def gen_i2v_video_via_batchexecute(
    *,
    profile_id: str,
    project_id: str | None,
    prompt: str,
    start_media_id: str,
    aspect_ratio: str = "16:9",
    duration_s: int | None = None,
    video_model_key: str | None = None,
) -> str:
    """Submit an image-to-video generation request (single start-frame image).
    Returns the media id to pass to poll_video_via_batchexecute.

    `duration_s` picks 4/5/6/7/8s via video_duration_model_key (8 is the
    default if omitted) — ignored if `video_model_key` is given explicitly.
    """
    model_key = video_model_key or video_duration_model_key("i2v", duration_s)
    return await _submit_video_request(
        profile_id=profile_id,
        project_id=project_id,
        rpcid=RPCID_GEN_I2V_VIDEO,
        build_params=lambda pid, token: _build_gen_i2v_video_params(
            project_id=pid,
            prompt=prompt,
            start_media_id=start_media_id,
            model_key=model_key,
            aspect_ratio=aspect_ratio,
            recaptcha_token=token,
        ),
    )


def _build_gen_i2v_fl_video_params(
    *,
    project_id: str,
    prompt: str,
    start_media_id: str,
    end_media_id: str,
    model_key: str,
    aspect_ratio: str,
    recaptcha_token: str,
) -> str:
    """Build params for RPCID_GEN_I2V_FL_VIDEO (start + end frame images, aka
    "interpolation"). Field order verified from a live capture — identical to
    i2v's item shape with one extra field for the end image, in between the
    start-image field and the trailing uuid field:
    item = [promptWrapper, modelKey, aspectCode, null,
            [null, startMediaId, null, null, null, cropBox],
            [null, endMediaId, null, null, null, cropBox],
            [null,null,null,null, uuid1, uuid2]]

    aspectCode uses the same VIDEO_ASPECT_CODE mapping as t2v (16:9 -> 2,
    9:16 -> 1) — previously hard-coded to 2, which silently forced every
    start+end-frame video to 16:9 regardless of the requested aspect ratio.
    """
    uuid1 = str(uuid.uuid4()).upper()
    uuid2 = str(uuid.uuid4()).upper()
    uuid3 = str(uuid.uuid4()).upper()
    aspect_code = VIDEO_ASPECT_CODE.get(aspect_ratio, VIDEO_ASPECT_CODE["16:9"])

    project_ctx = [
        None, 22, None, None, None, project_id,
        None, None, None, None, [recaptcha_token, 1],
    ]
    item = [
        [None, None, [[[prompt]]]],
        model_key,
        aspect_code,
        None,
        [None, start_media_id, None, None, None, None],
        [None, end_media_id, None, None, None, None],
        [None, None, None, None, uuid1, uuid2],
    ]
    params = [[item], project_ctx, [uuid3, 2]]
    return json.dumps(params, separators=(",", ":"), ensure_ascii=False)


async def gen_i2v_fl_video_via_batchexecute(
    *,
    profile_id: str,
    project_id: str | None,
    prompt: str,
    start_media_id: str,
    end_media_id: str,
    aspect_ratio: str = "16:9",
    duration_s: int | None = None,
    video_model_key: str | None = None,
) -> str:
    """Submit a start+end frame ("interpolation") video generation request.
    Returns the media id to pass to poll_video_via_batchexecute.

    `duration_s` picks 4/5/6/7/8s via video_duration_model_key (8 is the
    default if omitted) — ignored if `video_model_key` is given explicitly.
    """
    model_key = video_model_key or video_duration_model_key("i2v_fl", duration_s)
    return await _submit_video_request(
        profile_id=profile_id,
        project_id=project_id,
        rpcid=RPCID_GEN_I2V_FL_VIDEO,
        build_params=lambda pid, token: _build_gen_i2v_fl_video_params(
            project_id=pid,
            prompt=prompt,
            start_media_id=start_media_id,
            end_media_id=end_media_id,
            model_key=model_key,
            aspect_ratio=aspect_ratio,
            recaptcha_token=token,
        ),
    )


async def upsample_video_via_batchexecute(
    *,
    profile_id: str,
    project_id: str | None,
    media_id: str,
    generation_id: str,
    aspect_ratio: str = "16:9",
) -> str:
    """Submit a request to upsample an existing video to 1080p.

    `generation_id` must be the generation id from the original generate
    call's response, not the media id — callers should keep both around after
    submitting (e.g. from gen_t2v_video_via_batchexecute's raw response) or
    look it up via poll_video_via_batchexecute if only the media id is on hand.
    Returns the upsampled media id (source id + "_upsampled" suffix) to pass to
    poll_video_via_batchexecute / resolve_media_url_via_batchexecute.

    `aspect_ratio` must match the source video's own aspect ratio (item[2] uses
    the same VIDEO_ASPECT_CODE as the gen_*_video RPCs) — it was previously
    hard-coded to 2 (16:9), which silently stretched every 9:16 source video
    to 16:9 when upsampled.
    """
    from flow2api.services.flow_captcha_center import mint_captcha_token
    from flow2api.services.flow_profile_service import get_batchexecute_session

    cached = get_batchexecute_session(profile_id)
    if not cached:
        raise BatchExecuteError(
            f"no_cached_session: {profile_id} — chạy capture_batchexecute_session trước."
        )
    cookie_header = get_stored_cookie_header(profile_id)
    if not cookie_header:
        raise BatchExecuteError(f"no_stored_cookies: {profile_id}")

    effective_project_id = project_id or cached["project_id"]
    recaptcha_token = await mint_captcha_token(action=CAPTCHA_ACTION_VIDEO)

    uuid1 = str(uuid.uuid4()).upper()
    uuid3 = str(uuid.uuid4()).upper()
    project_ctx = [
        None, 22, None, None, None, effective_project_id,
        None, None, None, None, [recaptcha_token, 1],
    ]
    aspect_code = VIDEO_ASPECT_CODE.get(aspect_ratio, VIDEO_ASPECT_CODE["16:9"])
    item: list[Any] = [None] * 32
    item[0] = [None, media_id]
    item[2] = aspect_code
    item[4] = [None, generation_id, None, None, uuid1]
    item[6] = 2
    item[31] = UPSAMPLE_MODEL_KEY
    params_json = json.dumps(
        [[item], project_ctx, [uuid3]], separators=(",", ":"), ensure_ascii=False
    )

    status, body_text = await _post_batchexecute_http(
        profile_id=profile_id,
        cookie_header=cookie_header,
        rpcid=RPCID_UPSAMPLE_VIDEO,
        project_id=effective_project_id,
        session=cached,
        params_json=params_json,
    )
    if status != 200:
        raise BatchExecuteError(f"http_{status}: {body_text[:300]}")

    decoded = _decode_batchexecute_response(body_text, RPCID_UPSAMPLE_VIDEO)
    if not decoded.get("ok"):
        raise BatchExecuteError(f"rpc_error: {decoded.get('error')}")

    upsampled_media_id, _generation_id = _decode_video_submit_response(decoded["data"])
    return upsampled_media_id


_VIDEO_STATUS_FAILED = 4


def _decode_poll_video_status(decoded: list[Any]) -> int | None:
    """Extract the video status code from a jwpduf poll response.

    Real shape (verified from live captures):
    [null, code, [[mediaId, projectId, genId, "CAE", null, [..., statusList@8, ...], ...]]]
    statusList is e.g. [2] (processing), [3] (done), or
    [4, [3, "PUBLIC_ERROR_..."], ["SOME_FAILURE_REASON"], [1]] (failed — see
    _decode_poll_video_failure_reason for the failure detail nested in here).
    """
    try:
        status_list = decoded[2][0][5][8]
        return int(status_list[0])
    except (IndexError, TypeError, KeyError, ValueError):
        return None


def _decode_poll_video_failure_reason(decoded: list[Any]) -> str:
    """Extract the human-readable failure reason from a failed (status=4) poll
    response. Verified shape from a live capture (a request filtered for
    disallowed audio content): statusList = [4, [3, "PUBLIC_ERROR_AUDIO_FILTERED"],
    ["AUDIO_GENERATION_FILTERED"], [1]] — index 1's second element is the
    public-facing error code; index 2 has a more specific internal reason."""
    try:
        status_list = decoded[2][0][5][8]
        public_error = status_list[1][1] if len(status_list) > 1 else None
        detail = status_list[2][0] if len(status_list) > 2 and status_list[2] else None
        parts = [str(p) for p in (public_error, detail) if p]
        return " / ".join(parts) if parts else "unknown_failure_reason"
    except (IndexError, TypeError, KeyError):
        return "unknown_failure_reason"


def _decode_poll_video_generation_id(decoded: list[Any]) -> str | None:
    """Extract the generation id from a jwpduf poll response — same shape as
    _decode_poll_video_status, generation id is decoded[2][0][2]. Needed by
    upsample_video_via_batchexecute, which takes a generation id rather than
    a media id (see its docstring)."""
    try:
        gen_id = decoded[2][0][2]
    except (IndexError, TypeError, KeyError):
        return None
    return str(gen_id) if gen_id else None


async def get_video_generation_id(*, profile_id: str, media_id: str) -> str:
    """Look up a video's generation id from its media id via a single poll
    call — needed to call upsample_video_via_batchexecute when only the media
    id was kept around (e.g. it was returned from a gen_*_video_via_batchexecute
    call some time ago and the generation id wasn't saved alongside it)."""
    from flow2api.services.flow_profile_service import get_batchexecute_session

    cached = get_batchexecute_session(profile_id)
    if not cached:
        raise BatchExecuteError(
            f"no_cached_session: {profile_id} — chạy capture_batchexecute_session trước."
        )
    cookie_header = get_stored_cookie_header(profile_id)
    if not cookie_header:
        raise BatchExecuteError(f"no_stored_cookies: {profile_id}")

    params_json = json.dumps([None, None, [[media_id]]], separators=(",", ":"), ensure_ascii=False)
    status, body_text = await _post_batchexecute_http(
        profile_id=profile_id,
        cookie_header=cookie_header,
        rpcid=RPCID_POLL_VIDEO,
        project_id=cached["project_id"],
        session=cached,
        params_json=params_json,
    )
    if status != 200:
        raise BatchExecuteError(f"http_{status}: {body_text[:300]}")

    decoded = _decode_batchexecute_response(body_text, RPCID_POLL_VIDEO)
    if not decoded.get("ok"):
        raise BatchExecuteError(f"rpc_error: {decoded.get('error')}")

    generation_id = _decode_poll_video_generation_id(decoded["data"])
    if not generation_id:
        raise BatchExecuteError("generation_id_not_found_in_poll_response")
    return generation_id


async def poll_video_via_batchexecute(
    *,
    profile_id: str,
    media_id: str,
    max_wait_s: float = 300.0,
    poll_interval_s: float = 5.0,
) -> str:
    """Poll a submitted video generation until done, then resolve its URL.

    Mirrors the real UI's behavior: repeatedly call RPCID_POLL_VIDEO with just
    the media id until its status reaches _VIDEO_STATUS_DONE, then call
    RPCID_GET_MEDIA_URL (same rpcid used for images) to get the final video URL.
    """
    import asyncio

    from flow2api.services.flow_profile_service import get_batchexecute_session

    cached = get_batchexecute_session(profile_id)
    if not cached:
        raise BatchExecuteError(
            f"no_cached_session: {profile_id} — chạy capture_batchexecute_session trước."
        )
    cookie_header = get_stored_cookie_header(profile_id)
    if not cookie_header:
        raise BatchExecuteError(f"no_stored_cookies: {profile_id}")

    params_json = json.dumps([None, None, [[media_id]]], separators=(",", ":"), ensure_ascii=False)

    deadline = time.monotonic() + max_wait_s
    last_error = "timeout_waiting_for_video"
    started = time.monotonic()
    polls = 0
    while time.monotonic() < deadline:
        polls += 1
        status, body_text = await _post_batchexecute_http(
            profile_id=profile_id,
            cookie_header=cookie_header,
            rpcid=RPCID_POLL_VIDEO,
            project_id=cached["project_id"],
            session=cached,
            params_json=params_json,
            log_each_call=False,
        )
        if status != 200:
            last_error = f"http_{status}: {body_text[:300]}"
        else:
            decoded = _decode_batchexecute_response(body_text, RPCID_POLL_VIDEO)
            if not decoded.get("ok"):
                last_error = f"rpc_error: {decoded.get('error')}"
            else:
                video_status = _decode_poll_video_status(decoded["data"])
                if video_status == _VIDEO_STATUS_DONE:
                    logger.info(
                        "batchexecute video-poll done sau %s lần (%.1fs)",
                        polls,
                        time.monotonic() - started,
                    )
                    return await resolve_media_url_via_batchexecute(
                        profile_id=profile_id, media_id=media_id
                    )
                if video_status == _VIDEO_STATUS_FAILED:
                    reason = _decode_poll_video_failure_reason(decoded["data"])
                    logger.info(
                        "batchexecute video-poll failed sau %s lần (%.1fs): %s",
                        polls,
                        time.monotonic() - started,
                        reason,
                    )
                    raise BatchExecuteError(f"video_generation_failed: {reason}")
                last_error = f"still_processing (status={video_status})"
        # Video hiếm khi xong trong vài giây đầu, nhưng khi xong sớm thì
        # thường xong nhanh — poll dồn dập (2s) trong ~30s đầu để bắt kịp
        # các job nhanh, sau đó giãn về poll_interval_s cho phần đuôi dài.
        elapsed = time.monotonic() - started
        sleep_s = min(2.0, poll_interval_s) if elapsed < 30.0 else poll_interval_s
        await asyncio.sleep(sleep_s)

    logger.info(
        "batchexecute video-poll timeout sau %s lần (%.1fs): %s",
        polls,
        time.monotonic() - started,
        last_error,
    )
    raise BatchExecuteError(f"poll_video_failed: {last_error}")


async def gen_image_via_batchexecute(
    *,
    profile_id: str,
    project_id: str | None,
    prompt: str,
    image_model_key: str = "GEM_PIX_2",
    aspect_ratio: str = "16:9",
    image_media_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Generate an image via batchexecute.

    Prefers the cached DB session (project id + f.sid/bl/at, no CDP needed) plus
    a reCAPTCHA token minted on the shared Captcha Center. Falls back to reading
    a live CDP tab directly (and minting locally) if no cached session exists yet
    — that path also caches the session for next time.

    `image_media_ids`, when given, are media ids returned by
    upload_image_via_batchexecute — the generated image will use them as
    reference images (verified from a live capture: passing them adds a
    [[mediaId,null,null,null,1], ...] field to the request item).
    """
    from flow2api.services.flow_profile_service import get_batchexecute_session

    cached = get_batchexecute_session(profile_id)
    if cached:
        cookie_header = get_stored_cookie_header(profile_id)
        if not cookie_header:
            raise BatchExecuteError(f"no_stored_cookies: {profile_id}")

        from flow2api.services.flow_captcha_center import mint_captcha_token

        recaptcha_token = await mint_captcha_token(action=CAPTCHA_ACTION_IMAGE)
        effective_project_id = project_id or cached["project_id"]
        params_json = _build_gen_image_params(
            project_id=effective_project_id,
            prompt=prompt,
            image_model_key=image_model_key,
            aspect_ratio=aspect_ratio,
            recaptcha_token=recaptcha_token,
            image_media_ids=image_media_ids,
        )
        status, body_text = await _post_batchexecute_http(
            profile_id=profile_id,
            cookie_header=cookie_header,
            rpcid=RPCID_GEN_IMAGE,
            project_id=effective_project_id,
            session=cached,
            params_json=params_json,
        )
        return _finish_gen_image_response(status, body_text)

    # No cached session yet — capture it from a live CDP tab (also persists it
    # for next time so subsequent calls skip this branch entirely).
    _require_playwright()
    pw = None
    try:
        pw, _browser, context, page = await _attach_flow_page(profile_id)

        page_project_id = _extract_project_id(page.url)
        if not page_project_id:
            raise BatchExecuteError(f"no_project_in_tab_url: {page.url}")
        effective_project_id = project_id or page_project_id

        session = await _read_page_session(page)
        from flow2api.services.cookie_service import save_profile_cookies
        from flow2api.services.flow_profile_service import save_batchexecute_session

        # Cookies and f.sid/at must be captured together — see
        # capture_batchexecute_session's docstring for why a stale cookie
        # snapshot paired with a fresh session triple gets HTTP 401.
        cookies = await context.cookies()
        if cookies:
            save_profile_cookies(profile_id, cookies)

        save_batchexecute_session(
            profile_id,
            project_id=page_project_id,
            fsid=session["fsid"],
            bl=session["bl"],
            at=session["at"],
        )

        recaptcha_token = await _mint_recaptcha_token_on_page(page, action=CAPTCHA_ACTION_IMAGE)
        params_json = _build_gen_image_params(
            project_id=effective_project_id,
            prompt=prompt,
            image_model_key=image_model_key,
            aspect_ratio=aspect_ratio,
            recaptcha_token=recaptcha_token,
            image_media_ids=image_media_ids,
        )
        resp = await page.evaluate(
            """async ({ url, body, timeoutMs }) => {
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), timeoutMs);
                try {
                    const r = await fetch(url, {
                        method: 'POST',
                        headers: { 'content-type': 'application/x-www-form-urlencoded;charset=UTF-8' },
                        body,
                        credentials: 'include',
                        signal: controller.signal,
                    });
                    const text = await r.text();
                    return { status: r.status, text };
                } finally {
                    clearTimeout(timer);
                }
            }""",
            {
                "url": _build_batchexecute_url(RPCID_GEN_IMAGE, effective_project_id, session),
                "body": _build_batchexecute_body(RPCID_GEN_IMAGE, params_json, session),
                "timeoutMs": 60_000,
            },
        )
        return _finish_gen_image_response(int(resp.get("status") or 0), str(resp.get("text") or ""))
    finally:
        if pw is not None:
            try:
                await pw.stop()
            except Exception:
                pass


async def _mint_recaptcha_token_on_page(page, *, action: str, timeout_ms: int = 20_000) -> str:
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
        {
            "siteKey": "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV",
            "action": action,
            "timeoutMs": timeout_ms,
        },
    )
    if not token:
        raise BatchExecuteError("recaptcha_mint_empty")
    return str(token)


def _build_batchexecute_url(rpcid: str, project_id: str, session: dict[str, str]) -> str:
    reqid = str(int(time.time() * 1000) % 9_000_000 + 1_000_000)
    return (
        f"{_BATCHEXECUTE_URL}?rpcids={rpcid}"
        f"&source-path={urllib.parse.quote('/project/' + project_id)}"
        f"&bl={urllib.parse.quote(session['bl'])}&f.sid={urllib.parse.quote(session['fsid'])}"
        f"&hl=en-US&_reqid={reqid}&rt=c"
    )


def _build_batchexecute_body(rpcid: str, params_json: str, session: dict[str, str]) -> str:
    freq = json.dumps(
        [[[rpcid, params_json, None, "generic"]]], separators=(",", ":"), ensure_ascii=False
    )
    return f"f.req={urllib.parse.quote(freq)}&at={urllib.parse.quote(session['at'])}&"


def _build_gen_image_params(
    *,
    project_id: str,
    prompt: str,
    image_model_key: str,
    aspect_ratio: str,
    recaptcha_token: str,
    image_media_ids: list[str] | None = None,
) -> str:
    ts = int(time.time() * 1000)
    seed = ts % 1_000_000
    aspect_code = IMAGE_ASPECT_CODE.get(aspect_ratio, IMAGE_ASPECT_CODE["16:9"])
    uuid1 = str(uuid.uuid4()).upper()
    uuid2 = str(uuid.uuid4()).upper()
    uuid3 = str(uuid.uuid4()).upper()

    project_ctx = [
        None, 22, None, None, None, project_id,
        None, None, None, None, [recaptcha_token, 1],
    ]
    # image_inputs field: [[mediaId, null, null, null, 1], ...] — verified from a
    # live capture of a reference-image generate call. `1` in the last slot is the
    # input type code (only "reference" has been observed so far).
    image_inputs = (
        [[mid, None, None, None, 1] for mid in image_media_ids] if image_media_ids else None
    )
    item = [
        None, None, image_inputs, seed, aspect_code, image_model_key, None, project_ctx,
        [[[prompt]]], None, None, None, uuid1, uuid2,
    ]
    params = [None, [item], 1, project_ctx, [uuid3]]
    return json.dumps(params, separators=(",", ":"), ensure_ascii=False)


def _finish_gen_image_response(status: int, body_text: str) -> dict[str, Any]:
    if status != 200:
        raise BatchExecuteError(f"http_{status}: {body_text[:300]}")

    decoded = _decode_batchexecute_response(body_text, RPCID_GEN_IMAGE)
    if not decoded.get("ok"):
        raise BatchExecuteError(f"rpc_error: {decoded.get('error')}")

    result = _image_result_from_ogiZ0b(decoded["data"])
    if not result.get("media"):
        raise BatchExecuteError(f"no_media_in_response: {body_text[:300]}")
    return result


def _decode_upsample_image_response(body_text: str) -> str:
    """Extract the upscaled image's raw base64 data from a SPrCad response.

    Unlike every other rpcid here, this one returns the image bytes directly
    in the response — verified shape: the wrb.fr entry's payload is itself a
    2-element JSON array [metadataObject, base64ImageString], not a single
    JSON string like the other rpcids. _decode_batchexecute_response's normal
    single-string parse still works (entry[2] is a JSON string encoding this
    2-element array), so this just picks out index 1 from its decoded data.
    """
    decoded = _decode_batchexecute_response(body_text, RPCID_UPSAMPLE_IMAGE)
    if not decoded.get("ok"):
        raise BatchExecuteError(f"rpc_error: {decoded.get('error')}")
    try:
        b64 = decoded["data"][1]
    except (IndexError, TypeError, KeyError) as exc:
        raise BatchExecuteError(f"upsample_image_response_shape_unexpected: {exc}") from exc
    if not b64:
        raise BatchExecuteError("upsample_image_response_missing_data")
    return str(b64)


async def upsample_image_via_batchexecute(
    *,
    profile_id: str,
    media_id: str,
    target: str = "2k",
) -> str:
    """Upsample an existing image (from upload_image_via_batchexecute or a
    gen_image_via_batchexecute result) to 2K or 4K. Returns the raw base64
    image data directly — no polling needed, the result comes back inline.

    `target` is "2k" or "4k" (case-insensitive). Unlike every other request
    here, this one does NOT put project_id in the request at all — Google
    looks the project up from the media id server-side.
    """
    from flow2api.services.flow_captcha_center import mint_captcha_token
    from flow2api.services.flow_profile_service import get_batchexecute_session

    factor = IMAGE_UPSAMPLE_FACTOR.get(str(target or "").strip().lower())
    if factor is None:
        raise BatchExecuteError(f"unsupported_upsample_target: {target!r} (expected '2k' or '4k')")

    cached = get_batchexecute_session(profile_id)
    if not cached:
        raise BatchExecuteError(
            f"no_cached_session: {profile_id} — chạy capture_batchexecute_session trước."
        )
    cookie_header = get_stored_cookie_header(profile_id)
    if not cookie_header:
        raise BatchExecuteError(f"no_stored_cookies: {profile_id}")

    recaptcha_token = await mint_captcha_token(action=CAPTCHA_ACTION_IMAGE)
    project_ctx = [
        None, 22, None, None, None, None,
        None, None, None, None, [recaptcha_token, 1],
    ]
    params_json = json.dumps(
        [media_id, factor, project_ctx], separators=(",", ":"), ensure_ascii=False
    )

    status, body_text = await _post_batchexecute_http(
        profile_id=profile_id,
        cookie_header=cookie_header,
        rpcid=RPCID_UPSAMPLE_IMAGE,
        project_id=cached["project_id"],
        session=cached,
        params_json=params_json,
        timeout_s=120.0,
    )
    if status != 200:
        raise BatchExecuteError(f"http_{status}: {body_text[:300]}")

    return _decode_upsample_image_response(body_text)


def _build_gen_text_image_part(image_base64: str) -> list[Any] | None:
    """One image element of the contents array: [null, [mimeType, rawBase64]]."""
    s = str(image_base64 or "").strip()
    if not s:
        return None
    mime = "image/jpeg"
    if s.startswith("data:"):
        head = s.split(",", 1)[0]
        part = head.split(";", 1)[0]
        if part.startswith("data:") and len(part) > 5:
            mime = part[5:] or mime
    data = _strip_data_url(s).strip()
    if not data:
        return None
    return [None, [mime, data]]


def _build_gen_text_params(
    *,
    prompt: str,
    system_instruction: str,
    model: str,
    applet_id: str,
    applet_version_id: str,
    recaptcha_token: str,
    image_base64s: list[str] | None = None,
) -> str:
    """Build params for RPCID_GEN_TEXT (Gemini text generation, the "Prop
    Writer" tool). Field order verified from a live capture — a flat
    16-element positional array, mostly null:
    [0] modelName
    [1..8] null (unused/reserved)
    [9] contents = [[[[promptText], imagePart, imagePart, ...], "user"]]
        — each imagePart is [null, [mimeType, rawBase64]], appended in order
        right after the prompt element (verified from a 3-image live capture).
    [10] null
    [11] systemInstruction = [[[sysText]]]
    [12] [null, null, thinkingLevelCode] — only level 2 observed so far
    [13] null
    [14] [null, null, [appletId, null, appletVersionId]]
    [15] [recaptchaToken, 1]
    """
    params: list[Any] = [None] * 16
    params[0] = model
    content_items: list[Any] = [[prompt]]
    for raw in image_base64s or []:
        part = _build_gen_text_image_part(raw)
        if part is not None:
            content_items.append(part)
    params[9] = [[content_items, "user"]]
    if system_instruction:
        params[11] = [[[system_instruction]]]
    params[12] = [None, None, 2]
    params[14] = [None, None, [applet_id, None, applet_version_id]]
    params[15] = [recaptcha_token, 1]
    return json.dumps(params, separators=(",", ":"), ensure_ascii=False)


def _decode_gen_text_response(decoded: list[Any]) -> dict[str, Any]:
    """Extract {"text", "thought_signature"} from an agJzFb response.

    Real shape (verified from a live capture):
    [null, null, null, [[0, [[[text, null, null, null, null, thoughtSignature]]]]]]
    """
    try:
        entry = decoded[3][0][1][0][0]
        text = entry[0]
        thought_signature = entry[5] if len(entry) > 5 else None
    except (IndexError, TypeError, KeyError) as exc:
        raise BatchExecuteError(f"gen_text_response_shape_unexpected: {exc}") from exc
    if text is None:
        raise BatchExecuteError("gen_text_response_missing_text")
    return {"text": str(text), "thought_signature": thought_signature}


async def gen_text_via_batchexecute(
    *,
    profile_id: str,
    prompt: str,
    system_instruction: str = "",
    model: str = DEFAULT_TEXT_MODEL_BE,
    applet_id: str,
    applet_version_id: str,
    image_base64s: list[str] | None = None,
) -> dict[str, Any]:
    """Generate text via Gemini (the "Prop Writer" tool's batchexecute rpc).

    Unlike the other batchexecute calls, this one is scoped to a specific
    applet/tool instance (`applet_id`/`applet_version_id`), not a project —
    verified from a live capture on a /project/<id>/tool/<appletId>?mode=APP
    page. Image input (`image_base64s`) is supported and verified from a
    live 3-image capture; audio input, JSON schema, or multi-turn contents
    still aren't (those would need their own captures to get the field
    shapes right).
    """
    from flow2api.services.flow_captcha_center import mint_captcha_token
    from flow2api.services.flow_profile_service import get_batchexecute_session

    cached = get_batchexecute_session(profile_id)
    if not cached:
        raise BatchExecuteError(
            f"no_cached_session: {profile_id} — chạy capture_batchexecute_session trước."
        )
    cookie_header = get_stored_cookie_header(profile_id)
    if not cookie_header:
        raise BatchExecuteError(f"no_stored_cookies: {profile_id}")

    recaptcha_token = await mint_captcha_token(action=CAPTCHA_ACTION_TEXT)
    params_json = _build_gen_text_params(
        prompt=prompt,
        system_instruction=system_instruction,
        model=model,
        applet_id=applet_id,
        applet_version_id=applet_version_id,
        recaptcha_token=recaptcha_token,
        image_base64s=image_base64s,
    )

    status, body_text = await _post_batchexecute_http(
        profile_id=profile_id,
        cookie_header=cookie_header,
        rpcid=RPCID_GEN_TEXT,
        project_id=cached["project_id"],
        session=cached,
        params_json=params_json,
        timeout_s=120.0,
        applet_id=applet_id,
    )
    if status != 200:
        raise BatchExecuteError(f"http_{status}: {body_text[:300]}")

    decoded = _decode_batchexecute_response(body_text, RPCID_GEN_TEXT)
    if not decoded.get("ok"):
        raise BatchExecuteError(f"rpc_error: {decoded.get('error')}")

    return _decode_gen_text_response(decoded["data"])
