"""ChatGPT UI automation via Playwright (type / upload / send + capture Network conversation)."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import random
import re
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from dataclasses import dataclass, field

from flow2api.config import STORAGE_DIR
from flow2api.services import system_ops

logger = logging.getLogger(__name__)

CHAT_URL = "https://chatgpt.com/"
IMAGES_URL = "https://chatgpt.com/images"

def _is_noise_image_url(url: str) -> bool:
    """Skip favicons, SVG assets, UI icons — not generated result images."""
    u = (url or "").lower().strip()
    if not u:
        return True
    if u.startswith("data:image/svg"):
        return True
    # data-url from a tiny icon still has no host — filter by explicit noise later via size
    noise = (
        "favicon",
        "/assets/",
        "assets/favicon",
        ".svg",
        "sprite",
        "logo",
        "avatar",
        "emoji",
        "icon-",
        "/icon",
        "apple-touch",
        "manifest",
        "spinner",
        "placeholder",
        "chatgpt.com/favicon",
        "static.xx.",
        "cdn.oaistatic.com/assets",
    )
    return any(x in u for x in noise)


def _is_result_image_url(url: str) -> bool:
    """Likely a ChatGPT-generated result image (strict — not library/favicon APIs)."""
    u = (url or "").lower().strip()
    if not u or _is_noise_image_url(u):
        return False
    # Reject API list endpoints mistaken as images
    if any(
        x in u
        for x in (
            "/files/library",
            "/files?",
            "/files/upload",
            "/files/process",
            "/conversation/",
            "/estuary/list",
        )
    ):
        return False
    # Primary: Images gallery content
    # https://chatgpt.com/backend-api/estuary/content?id=file_0000...
    if "/backend-api/estuary/content" in u and "id=file_" in u.replace("id=file-", "id=file_"):
        return True
    if "estuary/content?" in u and "id=file" in u:
        return True
    good = (
        "oaiusercontent.com",
        "filesystem.site",
        "oaidalle",
        "/files/download/",
        "images.openai",
        "blob.core.windows.net",
    )
    return any(x in u for x in good)


def _filter_result_images(images: list[Any] | None) -> list[dict[str, Any]]:
    """Keep only real result images — drop favicon/icon/svg/noise/broken URLs."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for img in images or []:
        if not isinstance(img, dict):
            continue
        raw_url = str(img.get("download_url") or img.get("url") or "")
        data = str(img.get("data") or "")
        # Hard-drop favicon / assets / svg (even if later converted oddly)
        if raw_url and _is_noise_image_url(raw_url):
            continue
        if data and _is_noise_image_url(data):
            continue
        if ".svg" in raw_url.lower() or "favicon" in raw_url.lower() or "/assets/" in raw_url.lower():
            continue

        w = int(img.get("width") or 0)
        h = int(img.get("height") or 0)
        # Tiny UI icons
        if (w and w < 96) or (h and h < 96):
            continue

        is_cdn = bool(raw_url) and _is_result_image_url(raw_url)
        is_data = data.startswith("data:image/") and "svg" not in data[:80].lower()
        # Prefer displayable payload for dashboard (cross-origin estuary URLs break in <img>)
        if is_data:
            key = data[:96]
        elif is_cdn:
            key = raw_url.split("&sig=")[0][:160]
        else:
            continue

        if key in seen:
            continue
        seen.add(key)
        entry = dict(img)
        if is_data:
            entry["data"] = data
            # Keep CDN url as secondary if present
            if is_cdn:
                entry["download_url"] = raw_url
            elif not entry.get("download_url"):
                entry["download_url"] = data
        else:
            entry["download_url"] = raw_url
        out.append(entry)
    return out


def _images_for_frontend(images: list[Any] | None) -> list[dict[str, Any]]:
    """Prefer data: URLs for dashboard; keep estuary URLs that still need proxy/hydration."""
    cleaned = _filter_result_images(images)
    out: list[dict[str, Any]] = []
    for img in cleaned:
        data = str(img.get("data") or "")
        url = str(img.get("download_url") or "")
        if data.startswith("data:image/") and "svg" not in data[:80].lower():
            out.append(
                {
                    **img,
                    "data": data,
                    "download_url": url if _is_result_image_url(url) else data,
                }
            )
            continue
        if _is_result_image_url(url) and url.startswith("http"):
            # Keep estuary/content — caller should hydrate; if still present, frontend
            # can use download_url only after hydration. Prefer not to drop real gens.
            if "estuary/content" in url.lower() or "content?id=file_" in url.lower():
                out.append({**img, "download_url": url})
                continue
            if any(
                x in url
                for x in (
                    "oaiusercontent",
                    "oaidalle",
                    "filesystem.site",
                    "blob.core.windows.net",
                )
            ):
                out.append(img)
    return out


_HYDRATE_IMAGE_JS = """
async (url) => {
  try {
    const r = await fetch(url, { credentials: 'include', cache: 'force-cache' });
    if (!r.ok) return { ok: false, error: 'http_' + r.status };
    const buf = await r.arrayBuffer();
    const n = buf.byteLength;
    if (n < 500) return { ok: false, error: 'too_small', bytes: n };
    if (n > 15 * 1024 * 1024) return { ok: false, error: 'too_big', bytes: n };
    const bytes = new Uint8Array(buf);
    let mime = (r.headers.get('content-type') || '').split(';')[0].trim().toLowerCase();
    if (!mime.startsWith('image/') || mime.includes('svg')) {
      if (bytes[0] === 0x89 && bytes[1] === 0x50) mime = 'image/png';
      else if (bytes[0] === 0xff && bytes[1] === 0xd8) mime = 'image/jpeg';
      else if (bytes[0] === 0x52 && bytes[1] === 0x49) mime = 'image/webp';
      else if (bytes[0] === 0x47 && bytes[1] === 0x49) mime = 'image/gif';
      else return { ok: false, error: 'not_image', mime: mime || '', bytes: n };
    }
    let bin = '';
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    }
    return { ok: true, data: 'data:' + mime + ';base64,' + btoa(bin), bytes: n, mime };
  } catch (e) {
    return { ok: false, error: String(e && e.message || e) };
  }
}
"""


async def _hydrate_image_url(page, url: str) -> str | None:
    """Fetch image in ChatGPT page context (cookies) → data URL."""
    if not url or _is_noise_image_url(url) or not _is_result_image_url(url):
        return None
    try:
        result = await page.evaluate(_HYDRATE_IMAGE_JS, url)
    except Exception as exc:
        logger.warning("hydrate image failed url=%s err=%s", url[:80], exc)
        return None
    if isinstance(result, dict) and result.get("ok") and isinstance(result.get("data"), str):
        logger.info(
            "hydrated image bytes=%s mime=%s",
            result.get("bytes"),
            result.get("mime"),
        )
        return result["data"]
    logger.warning("hydrate rejected url=%s result=%s", url[:100], result)
    return None


async def _hydrate_result_images(page, images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ensure each result image has data: for dashboard display."""
    out: list[dict[str, Any]] = []
    for img in images[:12]:
        existing = str(img.get("data") or "")
        url = str(img.get("download_url") or "")
        if existing.startswith("data:image/") and "svg" not in existing[:80].lower():
            if not _is_noise_image_url(url):
                out.append(img)
            continue
        if not url.startswith("http"):
            continue
        data_url = await _hydrate_image_url(page, url)
        if data_url:
            out.append({**img, "data": data_url, "download_url": url, "kind": "image"})
    return out

_PROMPT_SELECTORS = [
    "#prompt-textarea",
    '[data-testid="prompt-textarea"]',
    'div[contenteditable="true"]#prompt-textarea',
    'div.ProseMirror[contenteditable="true"]',
    '[contenteditable="true"][data-placeholder]',
    'textarea[name="prompt-textarea"]',
    "textarea#prompt-textarea",
]

_SEND_SELECTORS = [
    '[data-testid="send-button"]',
    'button[data-testid="send-button"]',
    'button[aria-label="Send prompt"]',
    'button[aria-label*="Send prompt" i]',
    'button[aria-label*="Send message" i]',
    'button[aria-label*="Send" i]',
    'button[aria-label*="Gửi" i]',
    'form[data-type="unified-composer"] button[data-testid="send-button"]',
    'form button[type="submit"]',
]

_FILE_INPUT_SELECTORS = [
    'input[type="file"][accept*="image"]',
    'form input[type="file"]',
    'input[type="file"]',
]

_ATTACH_SELECTORS = [
    'button[aria-label*="Attach" i]',
    'button[aria-label*="Upload" i]',
    'button[aria-label*="Add photos" i]',
    'button[data-testid="composer-plus-btn"]',
    'button[aria-label*="+" i]',
]

@dataclass
class _SlotRuntime:
    slot_id: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pw: Any = None
    browser: Any = None
    context: Any = None
    page: Any = None
    launch_mode: str | None = None
    cdp_url: str = ""


_slot_runtimes: dict[str, _SlotRuntime] = {}
# Legacy single-lock kept for reset-all coordination
_registry_lock = asyncio.Lock()


def _get_slot_runtime(slot_id: str) -> _SlotRuntime:
    sid = (slot_id or "").strip() or "pw1"
    rt = _slot_runtimes.get(sid)
    if rt is None:
        rt = _SlotRuntime(slot_id=sid)
        _slot_runtimes[sid] = rt
    return rt


async def _close_slot_runtime(rt: _SlotRuntime) -> None:
    try:
        if rt.context is not None and rt.launch_mode == "persistent":
            await rt.context.close()
    except Exception:
        pass
    try:
        if rt.browser is not None and rt.launch_mode == "cdp":
            # connected — do not kill user's Chrome
            pass
        elif rt.browser is not None and rt.launch_mode != "cdp":
            await rt.browser.close()
    except Exception:
        pass
    try:
        if rt.pw is not None:
            await rt.pw.stop()
    except Exception:
        pass
    rt.pw = None
    rt.browser = None
    rt.context = None
    rt.page = None
    rt.launch_mode = None


async def _ensure_slot_page(rt: _SlotRuntime, *, cdp_url: str, user_data_dir: str | None = None):
    if rt.page is not None and not rt.page.is_closed():
        return rt.page

    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(
            "playwright_not_installed — pip install playwright && playwright install chrome"
        ) from exc

    await _close_slot_runtime(rt)
    rt.pw = await async_playwright().start()
    cdp = (cdp_url or "").strip()
    if not cdp:
        raise RuntimeError(
            f"cdp_missing — Slot {rt.slot_id} chưa có CDP. Bấm Mở CDP cho slot này."
        )
    if not system_ops.cdp_endpoint_alive(cdp):
        raise RuntimeError(
            f"cdp_unreachable — Không kết nối được {cdp} (slot {rt.slot_id}). "
            "Bấm 'Mở CDP' trên chip slot, đăng nhập chatgpt.com trong cửa sổ Chrome đó."
        )
    rt.browser = await rt.pw.chromium.connect_over_cdp(cdp)
    rt.launch_mode = "cdp"
    rt.cdp_url = cdp
    contexts = rt.browser.contexts
    rt.context = contexts[0] if contexts else await rt.browser.new_context()
    pages = rt.context.pages
    rt.page = pages[0] if pages else await rt.context.new_page()
    return rt.page


# Back-compat aliases used by older call sites
_playwright_lock = asyncio.Lock()
_pw = None
_browser = None
_context = None
_page = None
_launch_mode: str | None = None


async def _close_browser() -> None:
    """Close legacy singleton + all slot runtimes."""
    global _pw, _browser, _context, _page, _launch_mode
    for rt in list(_slot_runtimes.values()):
        await _close_slot_runtime(rt)
    _slot_runtimes.clear()
    _pw = None
    _browser = None
    _context = None
    _page = None
    _launch_mode = None


async def _ensure_page(cfg: dict[str, Any]):
    """Legacy: attach to first slot or global cdp_url."""
    from flow2api.services.chatgpt_pool_settings import list_playwright_slots

    slots = list_playwright_slots()
    slot = slots[0] if slots else None
    if slot:
        rt = _get_slot_runtime(slot.id)
        return await _ensure_slot_page(rt, cdp_url=slot.cdp_url(), user_data_dir=slot.user_data_dir())
    cdp = str(cfg.get("cdp_url") or "").strip() or "http://127.0.0.1:9222"
    rt = _get_slot_runtime("pw1")
    return await _ensure_slot_page(rt, cdp_url=cdp)


def _decode_data_url(data: str) -> tuple[bytes, str]:
    raw = (data or "").strip()
    mime = "image/jpeg"
    if raw.startswith("data:") and "," in raw:
        header, b64 = raw.split(",", 1)
        m = re.search(r"data:([^;]+)", header)
        if m:
            mime = m.group(1).strip() or mime
        raw = b64
    return base64.b64decode(raw), mime


def _ext_for_mime(mime: str) -> str:
    m = (mime or "").lower()
    if "png" in m:
        return ".png"
    if "webp" in m:
        return ".webp"
    if "gif" in m:
        return ".gif"
    return ".jpg"


def _is_conversation_response(url: str, method: str = "POST") -> bool:
    if (method or "").upper() != "POST":
        return False
    u = (url or "").lower()
    if "prepare" in u:
        return False
    path = urlparse(url).path.lower().rstrip("/")
    if path.endswith("/backend-api/f/conversation"):
        return True
    if path.endswith("/backend-api/conversation"):
        return True
    # DevTools Name column often shows just "conversation"
    return path.split("/")[-1] == "conversation"


_SSE_OP_NAMES = frozenset(
    {
        "append",
        "patch",
        "replace",
        "truncate",
        "add",
        "remove",
        "insert",
        "delete",
    }
)

_STATUS_PLACEHOLDER_RE = re.compile(
    r"^\s*("
    r"thinking(\s*\.{0,3}|\s+for\s+a\s+(?:few\s+)?seconds?)?|"
    r"analyzing(\s+image|\s+images|\s+the\s+image)?|"
    r"đang\s*suy\s*nghĩ(\s+trong\s*.+)?|"
    r"đang\s*phân\s*tích(\s+ảnh)?|"
    r"working(\s+on\s+it)?|"
    r"searching|"
    r"reasoning"
    r")\s*\.?\s*$",
    re.IGNORECASE | re.DOTALL,
)

_THOUGHT_CHANNELS = frozenset(
    {"thoughts", "thought", "reasoning", "analysis", "chain_of_thought", "cot"}
)


def _is_status_placeholder(text: str) -> bool:
    """True for Thinking / Analyzing image / … — chưa phải câu trả lời cuối."""
    s = str(text or "").strip()
    if not s:
        return False
    if _STATUS_PLACEHOLDER_RE.match(s):
        return True
    if len(s) <= 40 and re.search(
        r"(?i)^(thinking|analyzing|đang\s*suy\s*nghĩ|đang\s*phân\s*tích)\b", s
    ):
        return True
    return False


def _is_usable_answer_text(text: str) -> bool:
    s = str(text or "").strip()
    if not s or _is_status_placeholder(s):
        return False
    # JSON (có thể trong fence)
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", s, re.I)
    if fence and fence.group(1).strip():
        return True
    if s.startswith("{") or s.startswith("["):
        return len(s) >= 2
    return len(s) >= 12


def _extract_json_if_any(text: str) -> str:
    s = str(text or "").strip()
    if not s:
        return ""
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", s, re.I)
    if fence:
        inner = fence.group(1).strip()
        if inner:
            return inner
    if s.startswith("{") or s.startswith("["):
        return s
    for opener, closer in (("{", "}"), ("[", "]")):
        a, b = s.find(opener), s.rfind(closer)
        if a >= 0 and b > a:
            return s[a : b + 1]
    return s


def _clean_assistant_text(text: str) -> str:
    """Strip SSE op-name leakage (e.g. 'tappend', '...✅append') from assistant text."""
    s = str(text or "")
    if not s:
        return ""
    # Primary leak from ChatGPT SSE ops — remove every occurrence of "append"
    s = s.replace("append", "")
    # Rare whole-token leaks (word boundary only — avoid breaking normal words)
    s = re.sub(r"(?i)\b(patch|truncate)\b", "", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    lines = [ln.rstrip() for ln in s.splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    s = "\n".join(lines).strip()
    # Bỏ dòng status Thinking / Analyzing image ở đầu
    cleaned: list[str] = []
    for ln in s.splitlines():
        if _is_status_placeholder(ln.strip()) and not cleaned:
            continue
        cleaned.append(ln)
    return "\n".join(cleaned).strip()


def _normalize_answer_text(text: str) -> str:
    cleaned = _clean_assistant_text(text)
    if not cleaned or _is_status_placeholder(cleaned):
        return ""
    extracted = _extract_json_if_any(cleaned)
    return extracted if _is_usable_answer_text(extracted) else cleaned


def _is_thought_message(msg: dict[str, Any] | None) -> bool:
    if not isinstance(msg, dict):
        return False
    ch = str(msg.get("channel") or "").strip().lower()
    if ch in _THOUGHT_CHANNELS:
        return True
    content = msg.get("content") if isinstance(msg.get("content"), dict) else {}
    ct = str((content or {}).get("content_type") or "").strip().lower()
    return ct in _THOUGHT_CHANNELS


async def _wait_uploads_ready(page, expected: int, *, max_wait_s: float = 60.0) -> None:
    """Wait until composer finishes uploading attached images."""
    if expected <= 0:
        return
    deadline = time.time() + max_wait_s
    while time.time() < deadline:
        try:
            info = await page.evaluate(
                """(expected) => {
                  const uploading = !!document.querySelector(
                    '[data-testid*="upload"][aria-busy="true"], .uploading, [class*="uploading"]'
                  );
                  const thumbs = [
                    ...document.querySelectorAll(
                      'form img, [data-testid*="composer"] img, [class*="thumbnail"] img, ' +
                      '[class*="attachment"] img, button img'
                    ),
                  ].filter((img) => {
                    const s = img.currentSrc || img.src || '';
                    return s && !/avatar|icon|emoji|logo|svg/i.test(s);
                  });
                  return { uploading, thumbs: thumbs.length, expected };
                }""",
                expected,
            )
        except Exception:
            info = {"uploading": False, "thumbs": 0}
        if not info.get("uploading") and int(info.get("thumbs") or 0) >= expected:
            await asyncio.sleep(0.6)
            return
        await asyncio.sleep(0.4)
    logger.warning("upload wait timed out expected=%s", expected)


async def _page_result_stats(page) -> dict[str, Any]:
    try:
        return await page.evaluate(
            """() => {
              const assistantNodes = [
                ...document.querySelectorAll('[data-message-author-role="assistant"]'),
              ];
              const last = assistantNodes[assistantNodes.length - 1] || null;
              const roots = [];
              if (last) roots.push(last);
              // /images gallery often sits outside classic assistant bubble
              const main = document.querySelector('main') || document.body;
              roots.push(main);

              let text = '';
              if (last) {
                const md = last.querySelector('.markdown, [class*="markdown"]') || last;
                text = (md.innerText || '').trim();
              } else {
                // fallback: largest text block in main
                text = (main.innerText || '').trim().slice(0, 20000);
              }

              const imgs = [];
              const seen = new Set();
              for (const root of roots) {
                for (const img of root.querySelectorAll('img')) {
                  const s = img.currentSrc || img.src || '';
                  if (!s || seen.has(s)) continue;
                  if (/avatar|icon|emoji|logo|spinner|profile|favicon|\\/assets\\/|\\.svg($|\\?)/i.test(s)) continue;
                  // skip tiny UI icons
                  const w = img.naturalWidth || img.width || 0;
                  const h = img.naturalHeight || img.height || 0;
                  if ((w && w < 64) || (h && h < 64)) continue;
                  // Prefer real result images
                  if (!/oaiusercontent|filesystem\\.site|oaidalle|blob\\.core\\.windows\\.net|\\/files\\/download\\/|estuary\\/content|content\\?id=file_|^blob:/i.test(s)) continue;
                  seen.add(s);
                  imgs.push(s);
                }
              }
              const busy = !!document.querySelector(
                'button[aria-label*="Stop" i], button[data-testid="stop-button"]'
              );
              return { textLen: text.length, imgCount: imgs.length, busy, sample: text.slice(0, 80) };
            }"""
        )
    except Exception:
        return {"textLen": 0, "imgCount": 0, "busy": False}


async def _wait_generation_done(
    page,
    *,
    max_wait_s: float = 120.0,
    want_images: bool = False,
    min_images: int = 1,
) -> None:
    """Wait until ChatGPT finishes (Stop gone + text/images stable)."""
    deadline = time.time() + max_wait_s
    last_sig = None
    stable = 0
    saw_busy = False
    while time.time() < deadline:
        info = await _page_result_stats(page)
        busy = bool(info.get("busy"))
        if busy:
            saw_busy = True
        text_len = int(info.get("textLen") or 0)
        img_count = int(info.get("imgCount") or 0)
        sig = (text_len, img_count, busy)
        has_payload = text_len > 20 or img_count >= min_images
        if want_images:
            has_payload = img_count >= min_images or text_len > 40

        if not busy and has_payload:
            if sig == last_sig:
                stable += 1
                # images: need a bit more settle time
                need = 4 if want_images else 3
                if stable >= need:
                    await asyncio.sleep(1.2)
                    return
            else:
                stable = 0
        else:
            stable = 0
            # If never saw Stop and already have images, don't spin forever
            if (
                want_images
                and not busy
                and img_count >= min_images
                and (saw_busy or time.time() + 25 > deadline)
            ):
                stable += 1
                if stable >= 2:
                    return
        last_sig = sig
        await asyncio.sleep(0.7)


async def _scrape_result_media(page) -> dict[str, Any]:
    """Scrape assistant text + result images (chat bubble or /images gallery)."""
    try:
        raw = await page.evaluate(
            """async () => {
              const assistantNodes = [
                ...document.querySelectorAll('[data-message-author-role="assistant"]'),
              ];
              const last = assistantNodes[assistantNodes.length - 1] || null;
              const main = document.querySelector('main') || document.body;

              let text = '';
              if (last) {
                // Prefer longest usable assistant message (skip Thinking/Analyzing-only)
                let best = '';
                for (const node of assistantNodes) {
                  const clone = node.cloneNode(true);
                  clone.querySelectorAll(
                    'button, svg, nav, [class*="trailing"], [data-testid*="copy"], script, style, ' +
                    '[class*="thinking"], [data-testid*="thinking"], [aria-label*="Thinking" i]'
                  ).forEach((el) => el.remove());
                  const md = clone.querySelector('.markdown, [class*="markdown"]') || clone;
                  const t = (md.innerText || md.textContent || '').trim();
                  if (!t) continue;
                  if (/^(thinking|analyzing|đang\\s*suy\\s*nghĩ|đang\\s*phân\\s*tích)\\b/i.test(t) && t.length < 80) continue;
                  if (t.length > best.length) best = t;
                }
                text = best || (() => {
                  const clone = last.cloneNode(true);
                  clone.querySelectorAll(
                    'button, svg, nav, [class*="trailing"], [data-testid*="copy"], script, style'
                  ).forEach((el) => el.remove());
                  const md = clone.querySelector('.markdown, [class*="markdown"]') || clone;
                  return (md.innerText || md.textContent || '').trim();
                })();
              }

              const imgs = [];
              const seen = new Set();
              const roots = last ? [last, main] : [main];
              for (const root of roots) {
                for (const img of root.querySelectorAll('img')) {
                  let src = img.currentSrc || img.src || img.getAttribute('src') || '';
                  if (!src || seen.has(src)) continue;
                  if (/avatar|icon|emoji|logo|spinner|profile|favicon|\\/assets\\/|\\.svg($|\\?)/i.test(src)) continue;
                  if (src.startsWith('data:image/svg')) continue;
                  const w = img.naturalWidth || img.width || 0;
                  const h = img.naturalHeight || img.height || 0;
                  if ((w && w < 80) || (h && h < 80)) continue;
                  // ONLY real generated images (never favicon/assets/svg/icons)
                  if (/favicon|\\/assets\\/|\\.svg($|\\?)/i.test(src)) continue;
                  const interesting =
                    /oaiusercontent|filesystem\\.site|oaidalle|blob\\.core\\.windows\\.net|\\/files\\/download\\/|estuary\\/content|content\\?id=file_|^blob:/i.test(src);
                  if (!interesting) continue;
                  if (w && h && (w < 128 || h < 128)) continue;
                  seen.add(src);

                  if (src.startsWith('blob:')) {
                    try {
                      const buf = await fetch(src).then((r) => r.arrayBuffer());
                      const bytes = new Uint8Array(buf);
                      let bin = '';
                      const chunk = 0x8000;
                      for (let i = 0; i < bytes.length; i += chunk) {
                        bin += String.fromCharCode(...bytes.subarray(i, i + chunk));
                      }
                      src = 'data:image/png;base64,' + btoa(bin);
                    } catch (e) {
                      continue;
                    }
                  }
                  imgs.push({
                    kind: 'image',
                    download_url: src.startsWith('data:') ? null : src,
                    data: src.startsWith('data:') ? src : null,
                    width: w,
                    height: h,
                  });
                }
              }
              return { text, images: imgs };
            }"""
        )
    except Exception as exc:
        logger.debug("scrape result media failed: %s", exc)
        return {"text": "", "images": []}

    if not isinstance(raw, dict):
        return {"text": "", "images": []}
    images = []
    for img in raw.get("images") or []:
        if not isinstance(img, dict):
            continue
        url = img.get("download_url") or img.get("data")
        if not url or _is_noise_image_url(str(url)):
            continue
        if not _is_result_image_url(str(url)) and not str(url).startswith(("data:", "blob:")):
            continue
        images.append(
            {
                "kind": "image",
                "download_url": img.get("download_url") or (url if not str(url).startswith("data:") else None),
                "data": img.get("data") or (url if str(url).startswith("data:") else None),
                "width": img.get("width") or 0,
                "height": img.get("height") or 0,
            }
        )
    return {
        "text": _clean_assistant_text(str(raw.get("text") or "")),
        "images": _filter_result_images(images),
    }


def _merge_images(*lists: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for lst in lists:
        for img in lst or []:
            if not isinstance(img, dict):
                continue
            key = str(
                img.get("download_url")
                or img.get("data")
                or img.get("asset_pointer")
                or img.get("file_id")
                or ""
            )
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(img)
    return out


def _needs_wait_for_images(
    *,
    picture_mode: bool,
    page_url: str,
    body: str,
    uploaded_count: int = 0,
) -> bool:
    """True when EventStream [DONE] arrives before generated images exist."""
    if picture_mode:
        return True
    url = (page_url or "").lower()
    if "/images" in url:
        return True
    raw = body or ""
    markers = (
        "image_asset_pointer",
        "dalle",
        "ghostrider",
        "gen_image",
        "picture_v2",
        '"turn_use_case":"image',
        '"turn_use_case": "image',
        '"is_multimodal": true',
        '"is_multimodal":true',
        'content_type":"multimodal',
        'content_type": "multimodal',
    )
    if any(m in raw for m in markers):
        return True
    if uploaded_count > 0 and "/images" in url:
        return True
    return False


async def _collect_estuary_urls_from_dom(page) -> list[dict[str, Any]]:
    """Read estuary/content image URLs currently rendered on the page."""
    try:
        urls = await page.evaluate(
            """() => {
              const out = [];
              const seen = new Set();
              const add = (s) => {
                if (!s || seen.has(s)) return;
                if (!/estuary\\/content/i.test(s) || !/id=file/i.test(s)) return;
                if (/favicon|\\.svg($|\\?)|\\/assets\\//i.test(s)) return;
                seen.add(s);
                out.push(s);
              };
              for (const img of document.querySelectorAll('img')) {
                add(img.currentSrc || img.src || '');
                add(img.getAttribute('src') || '');
              }
              for (const el of document.querySelectorAll('[style*="estuary/content"]')) {
                const m = String(el.getAttribute('style') || '').match(/url\\([\"']?([^\"')]+)/);
                if (m) add(m[1]);
              }
              return out;
            }"""
        )
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for u in urls or []:
        if not isinstance(u, str) or not _is_result_image_url(u):
            continue
        entry: dict[str, Any] = {"kind": "image", "download_url": u}
        try:
            q = parse_qs(urlparse(u).query)
            fid = (q.get("id") or [None])[0]
            if fid:
                entry["file_id"] = fid
        except Exception:
            pass
        out.append(entry)
    return out


async def _wait_images_after_stream_done(
    page,
    *,
    conversation_id: str | None,
    seed_images: list[dict[str, Any]] | None = None,
    live_net_images: list[dict[str, Any]] | None = None,
    max_wait_s: float = 120.0,
    interval_s: float = 1.5,
) -> dict[str, Any]:
    """After [DONE], keep polling until generated images are ready (async gen)."""
    deadline = time.time() + max_wait_s
    text = ""
    images: list[dict[str, Any]] = list(seed_images or [])
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        # Pick up estuary/content responses that arrive after DONE
        if live_net_images:
            images = _merge_images(images, live_net_images)
        images = _merge_images(images, await _collect_estuary_urls_from_dom(page))

        scraped = await _scrape_result_media(page)
        if scraped.get("text"):
            text = scraped["text"]
        images = _merge_images(images, scraped.get("images") or [])

        if conversation_id:
            detail = await _poll_conversation_detail(
                page,
                str(conversation_id),
                max_attempts=1,
                interval_s=0,
                want_images=True,
            )
            if detail.get("text") and len(detail["text"]) >= len(text):
                text = detail["text"]
            images = _merge_images(images, detail.get("images") or [])

        ready = _filter_result_images(images)
        # estuary/content URL alone is enough to stop waiting — hydrate next
        if ready:
            logger.info("images ready after DONE attempt=%s count=%s", attempt, len(ready))
            return {"text": text, "images": ready}

        await asyncio.sleep(interval_s)

    logger.warning("images still missing after DONE wait=%.0fs attempts=%s", max_wait_s, attempt)
    if live_net_images:
        images = _merge_images(images, live_net_images)
    return {"text": text, "images": _filter_result_images(images)}


async def _poll_conversation_detail(
    page,
    conversation_id: str,
    *,
    max_attempts: int = 24,
    interval_s: float = 2.0,
    want_images: bool = True,
) -> dict[str, Any]:
    """Fetch /backend-api/conversation/{id} until text (+ images) look complete."""
    images: list[dict[str, Any]] = []
    text = ""
    for attempt in range(max_attempts):
        if attempt:
            await asyncio.sleep(interval_s)
        try:
            data = await page.evaluate(
                """async (cid) => {
                  const r = await fetch('/backend-api/conversation/' + cid, {
                    credentials: 'include',
                    headers: { 'accept': 'application/json' },
                  });
                  if (!r.ok) return { ok: false, status: r.status };
                  return { ok: true, data: await r.json() };
                }""",
                conversation_id,
            )
        except Exception:
            data = None
        if not (isinstance(data, dict) and data.get("ok") and isinstance(data.get("data"), dict)):
            continue

        mapping = data["data"].get("mapping") or {}
        messages: list[dict[str, Any]] = []
        for node in mapping.values() if isinstance(mapping, dict) else []:
            if not isinstance(node, dict):
                continue
            msg = node.get("message") if isinstance(node.get("message"), dict) else None
            if msg:
                messages.append(msg)
        messages.sort(key=lambda m: float(m.get("create_time") or 0))

        last_user = -1
        for i, msg in enumerate(messages):
            role = ((msg.get("author") or {}) if isinstance(msg.get("author"), dict) else {}).get(
                "role"
            )
            if role == "user":
                last_user = i
        slice_msgs = messages[last_user + 1 :] if last_user >= 0 else messages[-6:]

        turn_text = ""
        turn_images: list[dict[str, Any]] = []
        for msg in slice_msgs:
            role = ((msg.get("author") or {}) if isinstance(msg.get("author"), dict) else {}).get(
                "role"
            )
            if role not in ("assistant", "tool"):
                continue
            if _is_thought_message(msg):
                continue
            content = msg.get("content") if isinstance(msg.get("content"), dict) else {}
            parts = content.get("parts") if isinstance(content, dict) else None
            texts: list[str] = []
            if isinstance(parts, list):
                texts = [p for p in parts if isinstance(p, str) and p not in _SSE_OP_NAMES]
            elif isinstance(content.get("text"), str):
                texts = [str(content["text"])]
            if texts:
                candidate = "".join(texts)
                if not _is_status_placeholder(candidate):
                    turn_text = candidate
            if not isinstance(parts, list):
                continue
            for p in parts:
                if not isinstance(p, dict):
                    continue
                ptr = p.get("asset_pointer")
                file_id = p.get("file_id") or p.get("id")
                if isinstance(ptr, str) and "://" in ptr:
                    file_id = file_id or ptr.split("://", 1)[-1].split("?", 1)[0]
                url = p.get("url")
                image_url = p.get("image_url")
                if isinstance(image_url, dict):
                    url = url or image_url.get("url")
                elif isinstance(image_url, str):
                    url = url or image_url
                if isinstance(url, str) and url.strip().startswith(("http://", "https://", "data:")):
                    turn_images.append(
                        {
                            "kind": "image",
                            "download_url": url.strip(),
                            "asset_pointer": ptr,
                            "file_id": file_id,
                            "width": p.get("width") or 0,
                            "height": p.get("height") or 0,
                        }
                    )
                elif file_id:
                    turn_images.append(
                        {
                            "kind": "image",
                            "file_id": file_id,
                            "asset_pointer": ptr,
                            "width": p.get("width") or 0,
                            "height": p.get("height") or 0,
                        }
                    )

        if turn_text:
            text = _normalize_answer_text(turn_text)
        images = _merge_images(images, turn_images)

        # Resolve file_id → download URL
        for img in images:
            if img.get("download_url") or img.get("data") or not img.get("file_id"):
                continue
            fid = str(img["file_id"])
            try:
                meta = await page.evaluate(
                    """async (fileId) => {
                      const r = await fetch('/backend-api/files/download/' + encodeURIComponent(fileId), {
                        credentials: 'include',
                        headers: { 'accept': 'application/json' },
                      });
                      if (!r.ok) return null;
                      return await r.json();
                    }""",
                    fid,
                )
            except Exception:
                meta = None
            if isinstance(meta, dict):
                dl = meta.get("download_url") or meta.get("url") or meta.get("downloadUrl")
                if dl:
                    img["download_url"] = dl
                    img["file_name"] = meta.get("file_name") or meta.get("fileName")

        ready_imgs = [
            i
            for i in images
            if i.get("download_url") or i.get("data")
        ]
        # Image-only turns (picture_v2) often have empty assistant text
        if want_images:
            if ready_imgs:
                return {"text": text, "images": ready_imgs, "messageId": None}
            # keep polling for images; allow early exit only near the end with text
            if attempt >= max_attempts - 1:
                return {"text": text, "images": images, "messageId": None}
            continue

        if _is_usable_answer_text(text):
            mid = None
            for msg in reversed(slice_msgs):
                if _is_thought_message(msg):
                    continue
                role = ((msg.get("author") or {}) if isinstance(msg.get("author"), dict) else {}).get(
                    "role"
                )
                if role == "assistant" and msg.get("id"):
                    mid = str(msg["id"])
                    break
            return {"text": text, "images": ready_imgs or images, "messageId": mid}

    return {"text": text, "images": images, "messageId": None}


async def _try_resume_sse(
    page,
    *,
    topic_id: str | None,
    resume_token: str | None,
    conversation_id: str | None,
    timeout_s: float = 90.0,
) -> dict[str, Any]:
    """Follow resume_sse_endpoint after stream_handoff (best-effort)."""
    if not topic_id and not resume_token:
        return {}
    try:
        raw = await page.evaluate(
            """async ({ topicId, token, conversationId, timeoutMs }) => {
              const headers = {
                'accept': 'text/event-stream',
                'cache-control': 'no-cache',
              };
              if (token) headers['x-conduit-token'] = token;
              const paths = [];
              if (topicId) {
                paths.push('/backend-api/lat/r?topic_id=' + encodeURIComponent(topicId));
                paths.push('/backend-api/f/conversation/resume?topic_id=' + encodeURIComponent(topicId));
                paths.push('/backend-api/conversation/resume?topic_id=' + encodeURIComponent(topicId));
                paths.push('/backend-api/conversation/turn/' + encodeURIComponent(topicId));
              }
              if (conversationId && topicId) {
                paths.push(
                  '/backend-api/f/conversation?conversation_id=' + encodeURIComponent(conversationId) +
                  '&topic_id=' + encodeURIComponent(topicId)
                );
              }
              const ctrl = new AbortController();
              const timer = setTimeout(() => ctrl.abort(), timeoutMs);
              let body = '';
              let url = '';
              try {
                for (const p of paths) {
                  try {
                    const r = await fetch(p, {
                      method: 'GET',
                      credentials: 'include',
                      headers,
                      signal: ctrl.signal,
                    });
                    if (!r.ok) continue;
                    const ct = (r.headers.get('content-type') || '').toLowerCase();
                    body = await r.text();
                    url = p;
                    if (body && (ct.includes('event-stream') || body.includes('data:') || body.includes('[DONE]'))) {
                      break;
                    }
                    if (body && body.trim().startsWith('{')) break;
                  } catch (e) {
                    continue;
                  }
                }
              } finally {
                clearTimeout(timer);
              }
              return { body, url };
            }""",
            {
                "topicId": topic_id or "",
                "token": resume_token or "",
                "conversationId": conversation_id or "",
                "timeoutMs": int(max(5.0, timeout_s) * 1000),
            },
        )
    except Exception as exc:
        logger.debug("resume sse failed: %s", exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    body = str(raw.get("body") or "")
    if not body or body.strip().startswith("{"):
        return {"raw_url": raw.get("url")}
    parsed = _parse_sse_body(body)
    return {
        "text": _normalize_answer_text(parsed.get("text") or ""),
        "messageId": parsed.get("messageId"),
        "conversationId": parsed.get("conversationId") or conversation_id,
        "images": parsed.get("images") or [],
        "handoff": bool(parsed.get("handoff")),
        "stream_done": bool(parsed.get("stream_done")),
        "raw_url": raw.get("url"),
    }


async def _await_after_handoff(
    page,
    *,
    conversation_id: str | None,
    topic_id: str | None = None,
    resume_token: str | None = None,
    seed_text: str = "",
    seed_images: list[dict[str, Any]] | None = None,
    seed_message_id: str | None = None,
    max_wait_s: float = 180.0,
    want_images: bool = False,
) -> dict[str, Any]:
    """Sau stream_handoff/[DONE] bootstrap — chờ final answer (poll + DOM + resume SSE)."""
    text = _normalize_answer_text(seed_text)
    images = list(seed_images or [])
    message_id = seed_message_id
    deadline = time.time() + max_wait_s

    if topic_id or resume_token:
        resumed = await _try_resume_sse(
            page,
            topic_id=topic_id,
            resume_token=resume_token,
            conversation_id=conversation_id,
            timeout_s=min(45.0, max_wait_s),
        )
        if resumed.get("text") and _is_usable_answer_text(str(resumed["text"])):
            text = str(resumed["text"])
        if resumed.get("messageId"):
            message_id = resumed.get("messageId") or message_id
        images = _merge_images(images, resumed.get("images") or [])
        if resumed.get("conversationId"):
            conversation_id = resumed.get("conversationId") or conversation_id
        if _is_usable_answer_text(text) and not want_images:
            return {
                "text": text,
                "images": images,
                "messageId": message_id,
                "conversationId": conversation_id,
            }

    while time.time() < deadline:
        try:
            await _wait_generation_done(
                page,
                max_wait_s=min(8.0, max(2.0, deadline - time.time())),
                want_images=want_images,
            )
        except Exception:
            pass

        scraped = await _scrape_result_media(page)
        scraped_text = _normalize_answer_text(scraped.get("text") or "")
        if _is_usable_answer_text(scraped_text) and (
            len(scraped_text) >= len(text) or not _is_usable_answer_text(text)
        ):
            text = scraped_text
        images = _merge_images(images, scraped.get("images") or [])

        if conversation_id:
            detail = await _poll_conversation_detail(
                page,
                conversation_id,
                max_attempts=4,
                interval_s=1.5,
                want_images=want_images,
            )
            detail_text = _normalize_answer_text(detail.get("text") or "")
            if _is_usable_answer_text(detail_text) and (
                len(detail_text) >= len(text) or not _is_usable_answer_text(text)
            ):
                text = detail_text
            if detail.get("messageId"):
                message_id = detail.get("messageId") or message_id
            images = _merge_images(images, detail.get("images") or [])

        if _is_usable_answer_text(text) and (not want_images or images):
            break
        await asyncio.sleep(1.2)

    return {
        "text": text if _is_usable_answer_text(text) else "",
        "images": images,
        "messageId": message_id,
        "conversationId": conversation_id,
    }


def _extract_text_from_delta_ops(ops: Any) -> str:
    if not isinstance(ops, list):
        return ""
    out = ""
    for op in ops:
        if not isinstance(op, dict):
            continue
        if op.get("o") == "append" and isinstance(op.get("v"), str):
            chunk = op["v"]
            if chunk in _SSE_OP_NAMES:
                continue
            p = str(op.get("p") or "")
            if (not p) or ("/content/parts/" in p) or p == "/message/content/parts/0":
                out += chunk
        elif op.get("o") == "patch" and isinstance(op.get("v"), list):
            out += _extract_text_from_delta_ops(op["v"])
    return out


def _extract_text_from_event(data: dict[str, Any]) -> str:
    """Port of extension extractTextFromEventData — never treat op name 'append' as text."""
    if not isinstance(data, dict):
        return ""

    typ = data.get("type")
    if typ in (
        "message_marker",
        "server_ste_metadata",
        "message_stream_complete",
        "conversation_detail_metadata",
        "stream_handoff",
        "resume_conversation_token",
    ):
        return ""

    # Never use operation name fields as content
    if data.get("o") in _SSE_OP_NAMES and not isinstance(data.get("v"), (str, list)):
        return ""

    # {"o":"patch","v":[...]} or {"o":"append","v":"chunk"}
    if data.get("o") == "patch" and isinstance(data.get("v"), list):
        return _extract_text_from_delta_ops(data["v"])
    if data.get("o") == "append" and isinstance(data.get("v"), str):
        chunk = data["v"]
        return "" if chunk in _SSE_OP_NAMES else chunk

    # Compact append: {"v":"chunk"} without o/p — skip bare op names
    if (
        isinstance(data.get("v"), str)
        and data.get("o") is None
        and data.get("p") is None
        and not data.get("message")
    ):
        chunk = data["v"]
        return "" if chunk in _SSE_OP_NAMES else chunk

    if (
        isinstance(data.get("v"), str)
        and isinstance(data.get("p"), str)
        and "/content/parts/" in data["p"]
    ):
        chunk = data["v"]
        return "" if chunk in _SSE_OP_NAMES else chunk

    # Nested ops without top-level o=patch: {"v":[{o,p,v}, ...]}
    if isinstance(data.get("v"), list) and data.get("o") is None:
        return _extract_text_from_delta_ops(data["v"])

    msg = data.get("message") if isinstance(data.get("message"), dict) else None
    if msg:
        role = ((msg.get("author") or {}) if isinstance(msg.get("author"), dict) else {}).get("role")
        if role in ("assistant", "tool"):
            content = msg.get("content") if isinstance(msg.get("content"), dict) else None
            parts = content.get("parts") if content else None
            if isinstance(parts, list):
                return "".join(p for p in parts if isinstance(p, str) and p not in _SSE_OP_NAMES)

    if isinstance(data.get("delta"), str) and data["delta"] not in _SSE_OP_NAMES:
        return data["delta"]
    if isinstance(data.get("text"), str) and data["text"] not in _SSE_OP_NAMES:
        return data["text"]
    return ""


def _collect_images_from_parts(parts: list[Any], into: list[dict[str, Any]]) -> None:
    for p in parts:
        if not isinstance(p, dict):
            continue
        url = p.get("asset_pointer") or p.get("url")
        image_url = p.get("image_url")
        if isinstance(image_url, dict):
            url = url or image_url.get("url")
        elif isinstance(image_url, str):
            url = url or image_url
        if isinstance(url, str) and url.strip():
            into.append(
                {
                    "kind": "image",
                    "download_url": url.strip(),
                    "asset_pointer": p.get("asset_pointer"),
                    "file_id": p.get("file_id") or p.get("id"),
                    "width": p.get("width") or 0,
                    "height": p.get("height") or 0,
                }
            )


def _parse_sse_body(body: str) -> dict[str, Any]:
    assistant_text = ""
    conversation_id: str | None = None
    message_id: str | None = None
    events: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    stream_done = False
    handoff = False
    handoff_topic_id: str | None = None
    resume_token: str | None = None
    turn_exchange_id: str | None = None
    raw = body or ""
    if "[DONE]" in raw or "message_stream_complete" in raw:
        stream_done = True

    for block in raw.split("\n\n"):
        event_name = ""
        for line in block.split("\n"):
            if line.startswith("event:"):
                event_name = line[6:].strip()
                continue
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload:
                continue
            if payload == "[DONE]":
                stream_done = True
                continue
            try:
                data = json.loads(payload)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            events.append(data)

            typ = str(data.get("type") or "")
            if typ == "message_stream_complete":
                stream_done = True

            # ChatGPT handoff: [DONE] lần 1 chỉ kết thúc bootstrap SSE —
            # câu trả lời thật đi qua WS topic / resume SSE.
            if typ == "resume_conversation_token":
                handoff = True
                resume_token = str(data.get("token") or resume_token or "") or None
                if data.get("conversation_id"):
                    conversation_id = str(data["conversation_id"])
                continue

            if typ == "stream_handoff":
                handoff = True
                turn_exchange_id = str(
                    data.get("turn_exchange_id") or turn_exchange_id or ""
                ) or None
                if data.get("conversation_id"):
                    conversation_id = str(data["conversation_id"])
                for opt in data.get("options") or []:
                    if not isinstance(opt, dict):
                        continue
                    tid = str(opt.get("topic_id") or "").strip()
                    if tid and opt.get("type") in (
                        "resume_sse_endpoint",
                        "subscribe_ws_topic",
                    ):
                        handoff_topic_id = tid
                continue

            if data.get("conversation_id"):
                conversation_id = str(data["conversation_id"])
            if data.get("message_id"):
                message_id = str(data["message_id"])

            # Nested envelope: {"v":{"message":{...},"conversation_id":"..."}}
            nested = data.get("v") if isinstance(data.get("v"), dict) else None
            msg = data.get("message") if isinstance(data.get("message"), dict) else None
            if not msg and nested and isinstance(nested.get("message"), dict):
                msg = nested["message"]
                if nested.get("conversation_id") and not conversation_id:
                    conversation_id = str(nested["conversation_id"])

            if msg and _is_thought_message(msg):
                continue

            if msg and msg.get("id"):
                # Prefer final-channel assistant message id
                channel = msg.get("channel")
                role = ((msg.get("author") or {}) if isinstance(msg.get("author"), dict) else {}).get("role")
                if role == "assistant" and (channel == "final" or message_id is None):
                    message_id = str(msg["id"])

            # Full message parts — only REPLACE when non-empty (empty in_progress must not wipe)
            if msg and isinstance(msg.get("content"), dict):
                role = ((msg.get("author") or {}) if isinstance(msg.get("author"), dict) else {}).get("role")
                hidden = bool((msg.get("metadata") or {}).get("is_visually_hidden_from_conversation"))
                parts = msg["content"].get("parts")
                if role in ("assistant", "tool") and not hidden and isinstance(parts, list):
                    texts = [
                        p
                        for p in parts
                        if isinstance(p, str) and p not in _SSE_OP_NAMES and p.strip()
                    ]
                    if texts:
                        candidate = "".join(texts)
                        if not _is_status_placeholder(candidate):
                            assistant_text = candidate
                            _collect_images_from_parts(parts, images)
                        if data.get("o") not in ("append", "patch") and not (
                            isinstance(data.get("v"), str)
                        ):
                            continue

            piece = _extract_text_from_event(data)
            if piece and piece not in _SSE_OP_NAMES and not _is_status_placeholder(piece):
                assistant_text += piece

            # Images via patch ops
            if data.get("o") == "patch" and isinstance(data.get("v"), list):
                for op in data["v"]:
                    if isinstance(op, dict) and isinstance(op.get("v"), dict):
                        # finish_details / metadata — skip
                        if op.get("p") == "/message/metadata":
                            continue
                        _collect_images_from_parts([op["v"]], images)

    seen: set[str] = set()
    uniq_images: list[dict[str, Any]] = []
    for img in images:
        key = str(img.get("download_url") or img.get("asset_pointer") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        uniq_images.append(img)

    # Handoff [DONE] ≠ final answer done
    final_done = bool(stream_done) and not handoff

    return {
        "text": _clean_assistant_text(assistant_text),
        "conversationId": conversation_id,
        "messageId": message_id,
        "images": uniq_images,
        "events": events,
        "stream_done": final_done,
        "raw_stream_done": stream_done,
        "handoff": handoff,
        "handoff_topic_id": handoff_topic_id,
        "resume_token": resume_token,
        "turn_exchange_id": turn_exchange_id,
    }


async def _first_visible(page, selectors: list[str], timeout_ms: int = 15000):
    last_err: Exception | None = None
    per = max(800, timeout_ms // max(1, len(selectors)))
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=per)
            return loc
        except Exception as exc:
            last_err = exc
            continue
    raise TimeoutError(f"selector_not_found: {selectors[0]}… ({last_err})")


async def _read_composer_text(page) -> str:
    try:
        return await page.evaluate(
            """() => {
              const el = document.querySelector('#prompt-textarea')
                || document.querySelector('[data-testid="prompt-textarea"]')
                || document.querySelector('div.ProseMirror[contenteditable="true"]');
              if (!el) return '';
              return (el.innerText || el.textContent || '').replace(/\\u00a0/g, ' ').trim();
            }"""
        )
    except Exception:
        return ""


async def _set_composer_text(page, prompt: str) -> bool:
    """Set ProseMirror/contenteditable text in a way ChatGPT React state accepts."""
    text = prompt or ""
    try:
        ok = await page.evaluate(
            """(value) => {
              const el = document.querySelector('#prompt-textarea')
                || document.querySelector('[data-testid="prompt-textarea"]')
                || document.querySelector('div.ProseMirror[contenteditable="true"]');
              if (!el) return false;
              el.focus();
              try {
                document.execCommand('selectAll', false, null);
                document.execCommand('delete', false, null);
              } catch (_) {}
              // Prefer insertText so ProseMirror/React see a real input event
              let inserted = false;
              try {
                inserted = document.execCommand('insertText', false, value);
              } catch (_) {}
              if (!inserted) {
                el.textContent = '';
                if (el.isContentEditable) {
                  const p = document.createElement('p');
                  p.textContent = value;
                  el.appendChild(p);
                } else if ('value' in el) {
                  el.value = value;
                } else {
                  el.textContent = value;
                }
                el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: value}));
              }
              el.dispatchEvent(new Event('change', {bubbles: true}));
              return true;
            }""",
            text,
        )
        return bool(ok)
    except Exception:
        return False


async def _clear_composer_prefixes(page, box) -> None:
    """Remove New-chat tool chips like 'Web search' before typing the prompt.

    ChatGPT often inserts a prefix token in the composer; Backspace ~3 times
    clears it when the caret is at the start of the input.
    """
    try:
        await box.click(timeout=5000)
    except Exception:
        pass
    # Prefer focusing start of composer so Backspace hits the chip
    try:
        await page.keyboard.press("Home")
        await asyncio.sleep(0.08)
        await page.keyboard.press("Control+Home")
        await asyncio.sleep(0.08)
    except Exception:
        pass
    # Also try removing chip via its close/remove control if present
    try:
        await page.evaluate(
            """() => {
              const root = document.querySelector('form[data-type="unified-composer"]')
                || document.querySelector('form')
                || document.body;
              const chips = [...root.querySelectorAll(
                '[data-testid*="tool"], [class*="chip"], [class*="pill"], button, span'
              )];
              for (const el of chips) {
                const t = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                if (!t) continue;
                if (!/(web search|tìm kiếm|search|deep research|study)/.test(t)) continue;
                // click chip or a child close button
                const close = el.querySelector('button, [aria-label*="Remove" i], [aria-label*="Close" i], svg');
                (close || el).click();
                return true;
              }
              return false;
            }"""
        )
        await asyncio.sleep(0.15)
    except Exception:
        pass
    for _ in range(3):
        try:
            await page.keyboard.press("Backspace")
            await asyncio.sleep(0.06)
        except Exception:
            break
    logger.info("cleared composer prefixes with Backspace x3")


async def _fill_prompt(page, prompt: str) -> None:
    box = await _first_visible(page, _PROMPT_SELECTORS, timeout_ms=45000)
    await box.click(timeout=5000)
    # New Chat often has a tool prefix chip (e.g. "Web search") — clear it first
    await _clear_composer_prefixes(page, box)
    # Prefer keyboard insert_text so ProseMirror/React enables the ↑ send button
    try:
        await page.keyboard.press("Control+A")
        await asyncio.sleep(0.05)
        await page.keyboard.press("Backspace")
        await asyncio.sleep(0.05)
        await page.keyboard.insert_text(prompt or "")
    except Exception:
        filled = False
        try:
            await box.fill(prompt or "")
            filled = True
        except Exception:
            filled = False
        if not filled or len(prompt or "") > 200:
            await _set_composer_text(page, prompt or "")
    # Verify React actually accepted text
    current = await _read_composer_text(page)
    want = (prompt or "").strip()
    if want and (not current or (len(want) > 40 and want[:40] not in current and current[:40] not in want)):
        try:
            await box.click(timeout=3000)
            await _clear_composer_prefixes(page, box)
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
            await page.keyboard.insert_text(prompt or "")
        except Exception:
            if len(prompt or "") <= 500:
                await page.keyboard.type(prompt or "", delay=5)
            else:
                await _set_composer_text(page, prompt or "")
    # Blur/focus cycle helps ChatGPT enable send after long paste
    try:
        await page.evaluate(
            """() => {
              const el = document.querySelector('#prompt-textarea')
                || document.querySelector('[data-testid="prompt-textarea"]')
                || document.querySelector('div.ProseMirror[contenteditable="true"]');
              if (!el) return;
              el.dispatchEvent(new Event('input', { bubbles: true }));
              el.dispatchEvent(new Event('change', { bubbles: true }));
              el.blur();
              el.focus();
            }"""
        )
    except Exception:
        pass
    await asyncio.sleep(0.5)
    current = await _read_composer_text(page)
    if want and not current:
        raise TimeoutError("prompt_not_accepted — ChatGPT composer không nhận text (ProseMirror)")
    # Wait briefly for ↑ to enable
    for _ in range(20):
        info = await _probe_send_button(page)
        if info.get("found") and not info.get("disabled"):
            logger.info("send button enabled after fill label=%r", info.get("label"))
            return
        await asyncio.sleep(0.25)
    logger.warning("send button still disabled after fill probe=%s", await _probe_send_button(page))


_SEND_FIND_JS = """() => {
  const form = document.querySelector('form[data-type="unified-composer"]')
    || document.querySelector('form:has(#prompt-textarea)')
    || document.querySelector('form:has([data-testid="prompt-textarea"])')
    || document.querySelector('form:has(div.ProseMirror)')
    || document.querySelector('main form')
    || document.querySelector('form');
  if (!form) return { found: false, reason: 'no_form' };

  const labelOf = (el) =>
    ((el.getAttribute('aria-label') || '') + ' '
      + (el.getAttribute('data-testid') || '') + ' '
      + (el.getAttribute('title') || '') + ' '
      + (el.innerText || '').slice(0, 40)).toLowerCase().trim();

  const isDisabled = (el) =>
    !el
    || el.disabled
    || el.getAttribute('aria-disabled') === 'true'
    || el.getAttribute('data-disabled') === 'true'
    || el.classList.contains('disabled');

  const isBad = (el) => {
    const t = labelOf(el);
    return /dictat|microphone|voice|speech|attach|upload|plus|file|photo|image|add photos|high\\b|model|reason|project/.test(t);
  };

  const isSend = (el) => {
    const t = labelOf(el);
    return el.getAttribute('data-testid') === 'send-button'
      || /send prompt|send message|^send$|gửi( tin)?|submit/.test(t);
  };

  // Prefer explicit send-button (even if temporarily disabled — caller waits)
  let hit = form.querySelector('[data-testid="send-button"]')
    || [...form.querySelectorAll('button, [role="button"]')].find(b => isSend(b));

  if (!hit) {
    // Rightmost round control in composer footer (black ↑), skip mic / High
    const formRect = form.getBoundingClientRect();
    const cands = [...form.querySelectorAll('button, [role="button"]')]
      .filter(b => !isBad(b))
      .map(b => {
        const r = b.getBoundingClientRect();
        return { b, r, score: r.right + r.bottom * 0.01 };
      })
      .filter(x =>
        x.r.width >= 26 && x.r.width <= 72
        && x.r.height >= 26 && x.r.height <= 72
        && x.r.bottom > formRect.top
        && (formRect.right - x.r.right) <= 140
        && (formRect.bottom - x.r.bottom) <= 100
      )
      .sort((a, c) => c.score - a.score);
    hit = cands[0] && cands[0].b;
  }

  if (!hit) {
    return {
      found: false,
      reason: 'no_send_btn',
      buttons: [...form.querySelectorAll('button, [role="button"]')].slice(0, 16).map(b => {
        const r = b.getBoundingClientRect();
        return {
          label: labelOf(b).slice(0, 50),
          testid: b.getAttribute('data-testid'),
          disabled: isDisabled(b),
          w: Math.round(r.width), h: Math.round(r.height),
          x: Math.round(r.left), y: Math.round(r.top),
        };
      }),
    };
  }

  const r = hit.getBoundingClientRect();
  return {
    found: true,
    disabled: isDisabled(hit),
    label: labelOf(hit).slice(0, 80),
    testid: hit.getAttribute('data-testid'),
    x: r.left + r.width / 2,
    y: r.top + r.height / 2,
    w: Math.round(r.width),
    h: Math.round(r.height),
    left: Math.round(r.left),
    top: Math.round(r.top),
  };
}"""


async def _dismiss_add_to_project(page) -> None:
    """Close 'Add to Project: …' chip under composer — can steal clicks near send."""
    try:
        closed = await page.evaluate(
            """() => {
              const texts = ['add to project', 'thêm vào dự án'];
              const nodes = [...document.querySelectorAll('button, [role="button"], a, div')];
              for (const el of nodes) {
                const t = ((el.innerText || el.textContent || '') + ' '
                  + (el.getAttribute('aria-label') || '')).toLowerCase().replace(/\\s+/g, ' ');
                if (!texts.some(x => t.includes(x))) continue;
                // Prefer explicit close on the chip / nearby sibling
                const close = el.querySelector('button, [aria-label*="close" i], [aria-label*="remove" i], [aria-label*="dismiss" i]')
                  || el.parentElement?.querySelector('button[aria-label*="close" i], button[aria-label*="remove" i]');
                if (close) { close.click(); return 'close'; }
                // X-only tiny button next to the label
                const sib = el.parentElement && [...el.parentElement.querySelectorAll('button')].find(b => {
                  const lab = (b.getAttribute('aria-label') || b.innerText || '').toLowerCase();
                  return /close|remove|dismiss|×|x/.test(lab) || b.innerText.trim() === '×';
                });
                if (sib) { sib.click(); return 'sibling'; }
              }
              return '';
            }"""
        )
        if closed:
            logger.info("dismissed Add to Project chip via=%s", closed)
            await asyncio.sleep(0.25)
    except Exception:
        pass


async def _probe_send_button(page) -> dict[str, Any]:
    try:
        info = await page.evaluate(_SEND_FIND_JS)
        return info if isinstance(info, dict) else {"found": False}
    except Exception as exc:
        return {"found": False, "reason": str(exc)}


async def _send_started(page, *, before_text: str = "") -> bool:
    """True when ChatGPT accepted the send (stop btn / composer cleared / streaming)."""
    try:
        return bool(
            await page.evaluate(
                """(before) => {
                  if (document.querySelector('[data-testid="stop-button"], button[aria-label*="Stop" i]'))
                    return true;
                  const el = document.querySelector('#prompt-textarea')
                    || document.querySelector('[data-testid="prompt-textarea"]')
                    || document.querySelector('div.ProseMirror[contenteditable="true"]');
                  const now = ((el && (el.innerText || el.textContent)) || '').replace(/\\u00a0/g, ' ').trim();
                  if (before && before.length > 20 && now.length < Math.max(8, before.length * 0.35))
                    return true;
                  // Speech button back = send consumed
                  if (document.querySelector('[data-testid="composer-speech-button"]')
                      && !document.querySelector('[data-testid="send-button"]:not([disabled])'))
                    return true;
                  return false;
                }""",
                (before_text or "")[:500],
            )
        )
    except Exception:
        return False


async def _real_mouse_click_send(page, info: dict[str, Any]) -> bool:
    """Physical mouse click at send-button center (React listens to real pointer events)."""
    try:
        x = float(info["x"])
        y = float(info["y"])
        if x <= 0 or y <= 0:
            return False
        await page.mouse.move(x, y, steps=4)
        await asyncio.sleep(0.05)
        await page.mouse.down()
        await asyncio.sleep(0.04)
        await page.mouse.up()
        logger.info(
            "send mouse click at (%.0f,%.0f) label=%r testid=%s",
            x, y, info.get("label"), info.get("testid"),
        )
        return True
    except Exception as exc:
        logger.warning("send mouse click failed: %s", exc)
        return False


async def _js_force_click_send(page) -> dict[str, Any]:
    """DOM click + PointerEvent sequence on send-button."""
    try:
        return await page.evaluate(
            """() => {
              const form = document.querySelector('form[data-type="unified-composer"]')
                || document.querySelector('form:has(div.ProseMirror)')
                || document.querySelector('form');
              if (!form) return { ok: false, reason: 'no_form' };
              let hit = form.querySelector('[data-testid="send-button"]');
              if (!hit) {
                hit = [...form.querySelectorAll('button')].find(b =>
                  /send prompt|send message|^send$/i.test(b.getAttribute('aria-label') || '')
                );
              }
              if (!hit) {
                const fr = form.getBoundingClientRect();
                const cands = [...form.querySelectorAll('button, [role="button"]')]
                  .map(b => ({ b, r: b.getBoundingClientRect() }))
                  .filter(x => x.r.width >= 26 && x.r.width <= 72 && x.r.height >= 26 && x.r.height <= 72)
                  .filter(x => fr.right - x.r.right <= 140)
                  .filter(x => {
                    const t = ((x.b.getAttribute('aria-label')||'') + (x.b.getAttribute('data-testid')||'')).toLowerCase();
                    return !/dictat|mic|voice|speech|attach|plus|high/.test(t);
                  })
                  .sort((a, c) => c.r.right - a.r.right);
                hit = cands[0] && cands[0].b;
              }
              if (!hit) return { ok: false, reason: 'no_hit' };
              if (hit.disabled) hit.removeAttribute('disabled');
              if (hit.getAttribute('aria-disabled') === 'true') hit.setAttribute('aria-disabled', 'false');
              hit.focus();
              const r = hit.getBoundingClientRect();
              const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
              const common = { bubbles: true, cancelable: true, view: window, clientX: cx, clientY: cy };
              try {
                hit.dispatchEvent(new PointerEvent('pointerdown', { ...common, pointerId: 1, pointerType: 'mouse', buttons: 1 }));
                hit.dispatchEvent(new MouseEvent('mousedown', { ...common, buttons: 1 }));
                hit.dispatchEvent(new PointerEvent('pointerup', { ...common, pointerId: 1, pointerType: 'mouse' }));
                hit.dispatchEvent(new MouseEvent('mouseup', common));
              } catch (_) {}
              hit.click();
              // Also try native form submit as backup
              try { if (typeof form.requestSubmit === 'function') form.requestSubmit(hit); } catch (_) {}
              return {
                ok: true,
                label: (hit.getAttribute('aria-label') || hit.getAttribute('data-testid') || '').slice(0, 60),
                x: Math.round(cx), y: Math.round(cy),
              };
            }"""
        )
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


async def _keyboard_send(page) -> None:
    """Focus composer via JS (no caret click) then Ctrl/Meta+Enter."""
    try:
        await page.evaluate(
            """() => {
              const el = document.querySelector('#prompt-textarea')
                || document.querySelector('[data-testid="prompt-textarea"]')
                || document.querySelector('div.ProseMirror[contenteditable="true"]');
              if (el) el.focus();
            }"""
        )
        await asyncio.sleep(0.1)
        # Windows ChatGPT: Ctrl+Enter sends; plain Enter = newline in multiline
        for combo in ("Control+Enter", "Meta+Enter"):
            try:
                await page.keyboard.press(combo)
                await asyncio.sleep(0.15)
            except Exception:
                pass
    except Exception as exc:
        logger.warning("keyboard send failed: %s", exc)


async def _click_send(page, *, settle_s: float = 5.0, has_images: bool = False) -> None:
    """Click the black ↑ send button; verify ChatGPT actually accepted the send."""
    wait_s = max(3.0, float(settle_s or 0)) + (2.0 if has_images else 0.0)
    logger.info("waiting %.1fs before send arrow (images=%s)", wait_s, has_images)

    deadline_wait = time.time() + wait_s
    while time.time() < deadline_wait:
        await _dismiss_rate_limit_modal(page)
        await _dismiss_add_to_project(page)
        remaining = deadline_wait - time.time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(remaining, 0.7))

    await _dismiss_rate_limit_modal(page)
    await _dismiss_add_to_project(page)

    if has_images:
        try:
            await _wait_uploads_ready(page, 1, max_wait_s=45.0)
        except Exception:
            pass

    # Wait until send control is present & enabled — do NOT click into ProseMirror
    # (clicking text puts caret mid-prompt; Enter then inserts newline instead of send).
    info: dict[str, Any] = {"found": False}
    deadline = time.time() + 30
    while time.time() < deadline:
        await _dismiss_add_to_project(page)
        info = await _probe_send_button(page)
        if info.get("found") and not info.get("disabled"):
            break
        await asyncio.sleep(0.3)

    before = ""
    try:
        before = await _read_composer_text(page)
    except Exception:
        pass
    logger.info(
        "clicking send arrow… found=%s disabled=%s label=%r testid=%s rect=(%s,%s %sx%s) composer=%r",
        info.get("found"),
        info.get("disabled"),
        info.get("label"),
        info.get("testid"),
        info.get("left"),
        info.get("top"),
        info.get("w"),
        info.get("h"),
        before[:80],
    )
    if not info.get("found"):
        logger.warning("send probe miss: %s", info)

    async def _attempt_and_verify(name: str, coro) -> bool:
        try:
            ok = await coro
        except Exception as exc:
            logger.warning("send attempt %s error: %s", name, exc)
            return False
        if ok is False:
            return False
        for _ in range(10):
            if await _send_started(page, before_text=before):
                logger.info("send accepted after %s", name)
                return True
            await asyncio.sleep(0.25)
        logger.warning("send attempt %s did not start reply", name)
        return False

    # 1) Real mouse at computed center (best for React)
    if info.get("found") and not info.get("disabled"):
        if await _attempt_and_verify("mouse", _real_mouse_click_send(page, info)):
            return

    # 2) Playwright get_by_test_id / aria
    async def _pw_click() -> bool:
        for sel in _SEND_SELECTORS:
            loc = page.locator(sel).first
            try:
                if await loc.count() == 0:
                    continue
                await loc.click(timeout=4000, force=True)
                logger.info("send playwright click sel=%s", sel)
                return True
            except Exception:
                continue
        for name in ("Send prompt", "Send message", "Send"):
            try:
                loc = page.get_by_role("button", name=re.compile(rf"^{re.escape(name)}$", re.I)).first
                if await loc.count() > 0:
                    await loc.click(timeout=4000, force=True)
                    logger.info("send playwright role=%s", name)
                    return True
            except Exception:
                pass
        return False

    if await _attempt_and_verify("playwright", _pw_click()):
        return

    # 3) JS PointerEvent + click + requestSubmit
    async def _js_click() -> bool:
        result = await _js_force_click_send(page)
        logger.info("send js force: %s", result)
        return bool(result and result.get("ok"))

    if await _attempt_and_verify("js", _js_click()):
        return

    # Re-probe (layout may have shifted) and mouse again
    info2 = await _probe_send_button(page)
    if info2.get("found") and not info2.get("disabled"):
        if await _attempt_and_verify("mouse-retry", _real_mouse_click_send(page, info2)):
            return

    # 4) Keyboard Ctrl+Enter (no ProseMirror click — Enter alone = newline)
    async def _kb_send() -> bool:
        await _keyboard_send(page)
        return True

    if await _attempt_and_verify("keyboard", _kb_send()):
        return

    raise TimeoutError(
        "send_button_not_found — Không nhấn được nút mũi tên gửi (góc dưới phải). "
        f"probe={info!r} composer={before[:80]!r}"
    )


async def _upload_images(page, images: list[dict[str, Any]], tmp_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for i, img in enumerate(images or []):
        data = str(img.get("data") or img.get("base64") or "").strip()
        if not data:
            continue
        raw, mime = _decode_data_url(data)
        name = str(img.get("fileName") or img.get("file_name") or f"upload_{i}{_ext_for_mime(mime)}")
        if not Path(name).suffix:
            name += _ext_for_mime(mime)
        path = tmp_dir / name
        path.write_bytes(raw)
        paths.append(path)

    if not paths:
        return []

    file_input = None
    for sel in _FILE_INPUT_SELECTORS:
        loc = page.locator(sel).first
        try:
            if await loc.count() > 0:
                file_input = loc
                break
        except Exception:
            continue

    if file_input is None:
        # open attach menu then retry
        for sel in _ATTACH_SELECTORS:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0:
                    await btn.click(timeout=3000)
                    await asyncio.sleep(0.4)
                    break
            except Exception:
                continue
        for sel in _FILE_INPUT_SELECTORS:
            loc = page.locator(sel).first
            try:
                if await loc.count() > 0:
                    file_input = loc
                    break
            except Exception:
                continue

    if file_input is None:
        raise RuntimeError("chatgpt_file_input_not_found")

    await file_input.set_input_files([str(p) for p in paths])
    await asyncio.sleep(1.2)
    return paths


async def _idle_plain(page, seconds: float) -> None:
    """Wait without scrolling (scroll was interfering with composer / send arrow)."""
    deadline = time.time() + max(0.0, float(seconds or 0))
    while time.time() < deadline:
        await _dismiss_rate_limit_modal(page)
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(remaining, 0.8))


async def _click_new_chat(page) -> bool:
    """Click sidebar 'New chat' so the next job starts on a fresh conversation."""
    await _dismiss_rate_limit_modal(page)
    # Role / text first (matches current ChatGPT sidebar)
    for name in ("New chat", "Đoạn chat mới", "Chat mới"):
        for role in ("link", "button"):
            try:
                loc = page.get_by_role(role, name=re.compile(rf"^{re.escape(name)}$", re.I)).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click(timeout=5000)
                    logger.info("clicked New chat (%s/%s)", role, name)
                    await asyncio.sleep(0.8)
                    return True
            except Exception:
                pass
    for sel in (
        'a:has-text("New chat")',
        'button:has-text("New chat")',
        '[data-testid="create-new-chat-button"]',
        'nav a:has-text("New chat")',
        'a:has-text("Đoạn chat mới")',
        'button:has-text("Đoạn chat mới")',
    ):
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click(timeout=5000)
                logger.info("clicked New chat via %s", sel)
                await asyncio.sleep(0.8)
                return True
        except Exception:
            continue
    # JS fallback: find sidebar item by text
    try:
        ok = await page.evaluate(
            """() => {
              const want = ['new chat', 'đoạn chat mới', 'chat mới'];
              const nodes = [...document.querySelectorAll('a, button, [role="button"], [role="link"]')];
              for (const el of nodes) {
                const t = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                if (!want.some(w => t === w || t.startsWith(w))) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 8 || r.height < 8) continue;
                el.click();
                return true;
              }
              return false;
            }"""
        )
        if ok:
            logger.info("clicked New chat (js)")
            await asyncio.sleep(0.8)
            return True
    except Exception as exc:
        logger.warning("New chat click failed: %s", exc)
    return False


async def _ready_for_next_task(page) -> None:
    """After a job finishes: open New chat and idle briefly (no scroll)."""
    try:
        clicked = await _click_new_chat(page)
        if not clicked:
            # Fallback: navigate home composer
            try:
                await page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(0.8)
            except Exception:
                pass
        await _idle_plain(page, random.uniform(1.2, 2.5))
    except Exception as exc:
        logger.warning("ready_for_next_task failed: %s", exc)


async def _dismiss_rate_limit_modal(page) -> bool:
    """Click 'Got it' on ChatGPT 'Too many requests' (and similar) modals."""
    try:
        clicked = await page.evaluate(
            """() => {
              const texts = ['got it', 'okay', 'ok', 'accept', 'được rồi', 'đã hiểu'];
              const nodes = [...document.querySelectorAll('button, [role="button"], a')];
              for (const el of nodes) {
                const t = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                if (!t) continue;
                if (!texts.some(x => t === x || t.includes(x))) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 8 || r.height < 8) continue;
                // Prefer when modal title mentions rate limit
                const dialog = el.closest('[role="dialog"], [data-testid*="modal"], .modal, [class*="modal"]')
                  || document.body;
                const blob = ((dialog && dialog.innerText) || '').toLowerCase();
                const isRate = /too many requests|making requests too quickly|temporarily limited|rate limit|unusual activity/.test(blob);
                if (isRate || t === 'got it' || t === 'okay') {
                  el.click();
                  return true;
                }
              }
              return false;
            }"""
        )
        if clicked:
            logger.info("dismissed ChatGPT modal (Got it / rate limit)")
            await asyncio.sleep(0.6)
            return True
    except Exception as exc:
        logger.debug("dismiss rate-limit modal failed: %s", exc)
    # Locator fallback
    for sel in (
        'button:has-text("Got it")',
        'button:has-text("Okay")',
        'button:has-text("OK")',
        '[role="dialog"] button:has-text("Got it")',
    ):
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click(timeout=2000)
                logger.info("dismissed modal via selector %s", sel)
                await asyncio.sleep(0.6)
                return True
        except Exception:
            continue
    return False


async def _dismiss_common_modals(page) -> None:
    await _dismiss_rate_limit_modal(page)
    for sel in (
        'button:has-text("Okay")',
        'button:has-text("Got it")',
        'button:has-text("Accept")',
        '[data-testid="modal-close"]',
    ):
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click(timeout=1500)
                await asyncio.sleep(0.3)
        except Exception:
            pass


async def run_playwright_chat(
    *,
    prompt: str = "",
    images: list[dict[str, Any]] | None = None,
    picture_mode: bool = False,
    timeout_s: float = 180.0,
    slot_id: str | None = None,
) -> dict[str, Any]:
    """Drive ChatGPT UI on a specific CDP slot and capture Network `conversation`."""
    from flow2api.services.chatgpt_pool_settings import (
        get_playwright_slot,
        list_playwright_slots,
    )

    prompt = (prompt or "").strip()
    images = list(images or [])
    if not prompt and not images:
        return {"ok": False, "error": "empty_prompt"}

    sid = (slot_id or "").strip()
    if sid in ("", "playwright"):
        slots = list_playwright_slots()
        sid = slots[0].id if slots else "pw1"
    slot = get_playwright_slot(sid)
    if not slot:
        return {
            "ok": False,
            "error": f"slot_not_found — Không có Playwright slot '{sid}'. Thêm slot trên dashboard.",
        }

    target_url = IMAGES_URL if picture_mode else CHAT_URL
    timeout_ms = int(max(30.0, timeout_s) * 1000)
    rt = _get_slot_runtime(slot.id)

    async with rt.lock:
        tmp_dir = Path(tempfile.mkdtemp(prefix="flow2api_cgpt_"))
        try:
            page = await _ensure_slot_page(
                rt,
                cdp_url=slot.cdp_url(),
                user_data_dir=slot.user_data_dir(),
            )
            await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(1.0)

            await _dismiss_common_modals(page)
            await _dismiss_rate_limit_modal(page)

            if images:
                await _upload_images(page, images, tmp_dir)
                await _wait_uploads_ready(page, len(images), max_wait_s=90.0)
                await _dismiss_rate_limit_modal(page)

            if prompt:
                await _fill_prompt(page, prompt)
            elif images:
                pass

            await _dismiss_rate_limit_modal(page)

            # Collect image CDN URLs from network while generating
            net_images: list[dict[str, Any]] = []

            def _on_response(resp) -> None:
                try:
                    url = resp.url or ""
                    ul = url.lower()
                    is_estuary = (
                        "/backend-api/estuary/content" in ul
                        and "id=file" in ul
                    )
                    is_cdn = _is_result_image_url(url) and not is_estuary
                    if not is_estuary and not is_cdn:
                        return
                    if resp.status != 200:
                        return
                    ctype = ((resp.headers or {}).get("content-type") or "").lower()
                    if is_estuary:
                        if any(x in ctype for x in ("json", "javascript", "text/html", "text/plain")):
                            return
                    elif "svg" in ctype or "json" in ctype or "javascript" in ctype:
                        return
                    entry: dict[str, Any] = {
                        "kind": "image",
                        "download_url": url if is_estuary else (
                            url.split("?")[0] if "download" not in ul else url
                        ),
                    }
                    try:
                        q = parse_qs(urlparse(url).query)
                        fid = (q.get("id") or [None])[0]
                        if fid:
                            entry["file_id"] = fid
                    except Exception:
                        pass
                    net_images.append(entry)
                    logger.info("slot=%s captured image url=%s", slot.id, url[:120])
                except Exception:
                    pass

            page.on("response", _on_response)

            def _match(resp) -> bool:
                try:
                    return _is_conversation_response(resp.url, resp.request.method)
                except Exception:
                    return False

            try:
                async with page.expect_response(_match, timeout=timeout_ms) as resp_info:
                    await _click_send(page, settle_s=5.0, has_images=bool(images))
                response = await resp_info.value
                try:
                    await response.finished()
                except Exception:
                    pass

                status = response.status
                body = ""
                try:
                    body = await response.text()
                except Exception as exc:
                    return {
                        "ok": False,
                        "error": f"conversation_body_read_failed: {exc}",
                        "status": status,
                        "endpoint": response.url,
                        "slot_id": slot.id,
                    }

                if status >= 400:
                    detail = body[:500] if body else f"http_{status}"
                    try:
                        j = json.loads(body)
                        err = j.get("error")
                        if isinstance(err, dict):
                            detail = str(err.get("message") or err or detail)
                        elif err:
                            detail = str(err)
                        elif j.get("detail"):
                            detail = str(j.get("detail"))
                    except Exception:
                        pass
                    return {
                        "ok": False,
                        "error": detail,
                        "status": status,
                        "endpoint": response.url,
                        "slot_id": slot.id,
                    }

                parsed = _parse_sse_body(body)
                text = _normalize_answer_text(parsed.get("text") or "")
                out_images = list(parsed.get("images") or [])
                conversation_id = parsed.get("conversationId")
                message_id = parsed.get("messageId")
                stream_done = bool(parsed.get("stream_done"))
                handoff = bool(parsed.get("handoff"))
                wait_for_images = _needs_wait_for_images(
                    picture_mode=picture_mode,
                    page_url=target_url,
                    body=body,
                    uploaded_count=len(images),
                )

                # stream_handoff + [DONE] bootstrap ≠ final answer
                # (Thinking / Analyzing image cũng chưa phải kết quả)
                need_wait = (
                    handoff
                    or not _is_usable_answer_text(text)
                    or _is_status_placeholder(parsed.get("text") or "")
                )
                if need_wait and not wait_for_images:
                    logger.info(
                        "slot=%s conversation handoff/placeholder — chờ final "
                        "handoff=%s topic=%s conversation_id=%s text=%r",
                        slot.id,
                        handoff,
                        (parsed.get("handoff_topic_id") or "")[:40],
                        (conversation_id or "")[:12],
                        str(parsed.get("text") or "")[:60],
                    )
                    waited = await _await_after_handoff(
                        page,
                        conversation_id=conversation_id,
                        topic_id=parsed.get("handoff_topic_id"),
                        resume_token=parsed.get("resume_token"),
                        seed_text=text,
                        seed_images=_merge_images(out_images, net_images),
                        seed_message_id=message_id,
                        max_wait_s=min(240.0, max(120.0, timeout_s)),
                        want_images=False,
                    )
                    text = _normalize_answer_text(waited.get("text") or text)
                    out_images = _merge_images(out_images, waited.get("images") or [], net_images)
                    if waited.get("messageId"):
                        message_id = waited.get("messageId") or message_id
                    if waited.get("conversationId"):
                        conversation_id = waited.get("conversationId") or conversation_id
                    # Final answer sẵn → coi như stream done thật
                    if _is_usable_answer_text(text):
                        stream_done = True

                if stream_done and _is_usable_answer_text(text) and not wait_for_images:
                    out = {
                        "ok": True,
                        "text": text,
                        "conversationId": conversation_id,
                        "messageId": message_id,
                        "images": _merge_images(out_images, net_images),
                        "files": [],
                        "uploadedImages": [],
                        "endpoint": response.url,
                        "model": None,
                        "via": "playwright",
                        "page_url": target_url,
                        "stream_done": True,
                        "slot_id": slot.id,
                    }
                    await _ready_for_next_task(page)
                    return out

                if wait_for_images:
                    logger.info(
                        "slot=%s stream DONE — waiting images conversation_id=%s",
                        slot.id,
                        (conversation_id or "")[:12],
                    )
                    if handoff:
                        waited_h = await _await_after_handoff(
                            page,
                            conversation_id=conversation_id,
                            topic_id=parsed.get("handoff_topic_id"),
                            resume_token=parsed.get("resume_token"),
                            seed_text=text,
                            seed_images=_merge_images(out_images, net_images),
                            seed_message_id=message_id,
                            max_wait_s=min(240.0, max(120.0, timeout_s)),
                            want_images=True,
                        )
                        text = _normalize_answer_text(waited_h.get("text") or text)
                        out_images = _merge_images(
                            out_images, waited_h.get("images") or [], net_images
                        )
                        if waited_h.get("messageId"):
                            message_id = waited_h.get("messageId") or message_id
                    waited = await _wait_images_after_stream_done(
                        page,
                        conversation_id=conversation_id,
                        seed_images=_merge_images(out_images, net_images),
                        live_net_images=net_images,
                        max_wait_s=min(150.0, max(60.0, timeout_s)),
                        interval_s=1.5,
                    )
                    if waited.get("text") and (
                        len(waited["text"]) >= len(text) or not text
                    ):
                        waited_text = _normalize_answer_text(waited["text"])
                        if _is_usable_answer_text(waited_text):
                            text = waited_text
                    out_images = _merge_images(
                        out_images, waited.get("images") or [], net_images
                    )
                elif not _is_usable_answer_text(text):
                    scraped = await _scrape_result_media(page)
                    scraped_text = _normalize_answer_text(scraped.get("text") or "")
                    if _is_usable_answer_text(scraped_text):
                        text = scraped_text
                    out_images = _merge_images(
                        out_images, scraped.get("images") or [], net_images
                    )
                    if conversation_id and not _is_usable_answer_text(text):
                        detail = await _poll_conversation_detail(
                            page,
                            conversation_id,
                            max_attempts=24,
                            interval_s=2.0,
                            want_images=False,
                        )
                        detail_text = _normalize_answer_text(detail.get("text") or "")
                        if _is_usable_answer_text(detail_text):
                            text = detail_text
                        if detail.get("messageId"):
                            message_id = detail.get("messageId") or message_id
                        out_images = _merge_images(
                            out_images, detail.get("images") or [], net_images
                        )

                text = _normalize_answer_text(text)
                if _is_status_placeholder(text):
                    text = ""
                out_images = _filter_result_images(_merge_images(out_images, net_images))

                if wait_for_images or out_images:
                    out_images = await _hydrate_result_images(page, out_images)

                if wait_for_images and not any(
                    str(i.get("data") or "").startswith("data:image/") for i in out_images
                ):
                    scraped = await _scrape_result_media(page)
                    if scraped.get("text") and len(scraped["text"]) >= len(text):
                        text = scraped["text"]
                    out_images = await _hydrate_result_images(
                        page,
                        _filter_result_images(
                            _merge_images(out_images, scraped.get("images") or [], net_images)
                        ),
                    )

                final_images: list[dict[str, Any]] = []
                for img in out_images:
                    data = str(img.get("data") or "")
                    url = str(img.get("download_url") or "")
                    if data.startswith("data:image/") and "svg" not in data[:80].lower():
                        final_images.append(
                            {
                                **img,
                                "data": data,
                                "download_url": url if _is_result_image_url(url) else data,
                                "kind": "image",
                            }
                        )
                out_images = final_images

                if (not _is_usable_answer_text(text) and not out_images) or (
                    wait_for_images and not out_images
                ):
                    captured = [
                        str(i.get("download_url"))
                        for i in (net_images or [])
                        if "estuary/content" in str(i.get("download_url") or "").lower()
                    ][:3]
                    if not captured:
                        captured = [
                            str(i.get("download_url"))
                            for i in await _collect_estuary_urls_from_dom(page)
                        ][:3]
                    err = (
                        "empty_result — ChatGPT stream_handoff/[DONE] bootstrap xong "
                        "nhưng chưa có câu trả lời cuối (vẫn Thinking/Analyzing)."
                        if not wait_for_images
                        else (
                            "empty_result — Chưa hydrate được ảnh estuary/content. "
                            + (f"seen={captured}" if captured else "Chưa thấy URL estuary/content?id=file_… trên Network/DOM.")
                        )
                    )
                    return {
                        "ok": False,
                        "error": err,
                        "conversationId": conversation_id,
                        "messageId": message_id,
                        "endpoint": response.url,
                        "page_url": target_url,
                        "stream_done": stream_done,
                        "handoff": handoff,
                        "waited_for_images": wait_for_images,
                        "slot_id": slot.id,
                    }

                return_payload = {
                    "ok": True,
                    "text": text if _is_usable_answer_text(text) else ("(đã tạo ảnh)" if out_images else ""),
                    "conversationId": conversation_id,
                    "messageId": message_id,
                    "images": out_images,
                    "files": [],
                    "uploadedImages": [],
                    "endpoint": response.url,
                    "model": None,
                    "via": "playwright",
                    "page_url": target_url,
                    "stream_done": stream_done,
                    "handoff": handoff,
                    "waited_for_images": wait_for_images,
                    "slot_id": slot.id,
                }
                await _ready_for_next_task(page)
                return return_payload
            finally:
                try:
                    page.remove_listener("response", _on_response)
                except Exception:
                    pass
        except Exception as exc:
            logger.exception("chatgpt playwright chat failed slot=%s", slot.id)
            err = str(exc) or "playwright_chat_failed"
            if "Target closed" in err or "has been closed" in err or "launch_failed" in err:
                await _close_slot_runtime(rt)
            else:
                # Still try New chat so the next job is not stuck in a broken thread
                try:
                    await _ready_for_next_task(page)
                except Exception:
                    pass
            return {"ok": False, "error": err, "slot_id": slot.id}
        finally:
            try:
                for p in tmp_dir.glob("*"):
                    try:
                        p.unlink()
                    except Exception:
                        pass
                tmp_dir.rmdir()
            except Exception:
                pass


async def playwright_slot_status(slot_id: str | None = None) -> dict[str, Any]:
    from flow2api.services.chatgpt_pool_settings import (
        get_playwright_slot,
        list_playwright_slots,
    )

    ready = False
    hint = ""
    try:
        from playwright.async_api import async_playwright  # noqa: F401

        ready = True
    except ImportError:
        hint = "Cài: pip install playwright && playwright install chrome"

    slots_out = []
    for slot in list_playwright_slots():
        if slot_id and slot.id != slot_id:
            continue
        rt = _slot_runtimes.get(slot.id)
        page_open = bool(rt and rt.page is not None and not rt.page.is_closed())
        cdp = slot.cdp_url()
        alive = system_ops.cdp_endpoint_alive(cdp)
        slots_out.append(
            {
                **slot.to_dict(),
                "cdp_alive": alive,
                "browser_open": page_open,
                "launch_mode": rt.launch_mode if rt else None,
            }
        )

    any_alive = any(s.get("cdp_alive") for s in slots_out)
    any_open = any(s.get("browser_open") for s in slots_out)
    return {
        "playwright_installed": ready,
        "browser_open": any_open,
        "cdp_alive": any_alive,
        "cdp_url": slots_out[0]["cdp_url"] if slots_out else "",
        "slots": slots_out,
        "hint": hint
        or (
            "Mỗi slot = Chrome CDP riêng (user-data riêng). "
            "Bấm Mở CDP / Mở tất cả, login chatgpt.com từng cửa sổ, bật Nhận job."
        ),
    }


async def playwright_status() -> dict[str, Any]:
    cfg = system_ops.chatgpt_config()
    st = await playwright_slot_status()
    return {
        "transport_setting": str(cfg.get("transport") or "playwright").strip().lower(),
        "playwright_installed": st.get("playwright_installed"),
        "browser_open": st.get("browser_open"),
        "launch_mode": None,
        "cdp_url": st.get("cdp_url") or "",
        "cdp_alive": st.get("cdp_alive"),
        "chrome_profile": cfg.get("chrome_profile") or "Default",
        "use_system_chrome_profile": bool(cfg.get("use_system_chrome_profile")),
        "headless": bool(cfg.get("headless")),
        "chrome_profiles": system_ops.list_chrome_profiles(),
        "slots": st.get("slots") or [],
        "hint": st.get("hint"),
    }


async def reset_playwright_browser(slot_id: str | None = None) -> None:
    sid = (slot_id or "").strip()
    if sid:
        rt = _slot_runtimes.get(sid)
        if rt:
            async with rt.lock:
                await _close_slot_runtime(rt)
        return
    async with _registry_lock:
        await _close_browser()
