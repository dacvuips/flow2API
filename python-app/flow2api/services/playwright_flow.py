"""Google Flow UI automation via Playwright — upload ảnh + điền prompt."""
from __future__ import annotations

import asyncio
import logging
import random
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from flow2api.config import (
    UI_ACTION_DELAY_MAX_S,
    UI_ACTION_DELAY_MIN_S,
    UI_AUTOMATION_ENABLED,
    UI_PREP_ONLY,
    UI_UPLOAD_PREVIEW_TIMEOUT_S,
    UI_UPLOAD_RETRY_MAX,
)
from flow2api.services import flow_sdk
from flow2api.services.flow_sdk import UPLOAD_IMAGE_PATH
from flow2api.services.playwright_pool import get_playwright_pool, is_playwright_enabled
from flow2api.services.result_media import _decode_image_bytes
from flow2api.services.system_ops import load_config

logger = logging.getLogger(__name__)

_UPLOAD_MENU_PATTERNS = re.compile(
    r"tải nội dung nghe nhìn|tải nội dung nghe|tải tệp|upload.*media|upload.*visual|tải lên",
    re.I,
)
# Sidebar "Tệp tải lên" — không bấm khi upload qua prompt [+].
_SIDEBAR_UPLOAD = re.compile(r"tệp tải lên|uploaded files", re.I)
_ADD_TO_PROMPT_PATTERNS = re.compile(
    r"thêm vào câu lệnh|add to prompt|add to command",
    re.I,
)
_NEW_PROJECT_PATTERNS = re.compile(
    r"dự án mới|new project|create.*project|tạo dự án",
    re.I,
)
_NEW_PROJECT_LOOSE = re.compile(r"\+.*(mới|new)|^(?:\+|)\s*(?:dự án mới|new project)", re.I)
_PROMPT_PLACEHOLDER = re.compile(r"bạn muốn tạo|what do you want", re.I)
# DevTools: nút [+] prompt bar — <button aria-haspopup="dialog"><i>add_2</i><span>Tạo</span>
_PROMPT_ADD_ICON = re.compile(r"^add_2$", re.I)
_CREATE_BTN_NAME = re.compile(r"^tạo$", re.I)
_MODEL_CHIP_PATTERNS = re.compile(r"nano banana|banana|imagen|pro|lite", re.I)
_MODEL_CHIP_ROW_PATTERNS = re.compile(r"nano banana|banana", re.I)
_UPLOAD_TEXT = re.compile(r"tải nội dung|tải tệp|upload", re.I)
_ASPECT_RATIOS = ("16:9", "4:3", "1:1", "3:4", "9:16")
_VARIANT_COUNT_LABELS = frozenset({"1x", "x1", "x2", "x3", "x4"})
_KNOWN_IMAGE_MODELS: dict[str, str] = {
    "NANO_BANANA_PRO": "Nano Banana Pro",
    "NANO_BANANA_2": "Nano Banana 2",
    "NANO_BANANA_2_LITE": "Nano Banana 2 Lite",
}
_GENERATE_IMAGE_API_PATTERNS = re.compile(r"flowMedia:batchGenerateImages|batchGenerateImages", re.I)


async def _ui_delay(step: str = "") -> float:
    """Chờ ngẫu nhiên giữa các thao tác UI (mặc định 2–5s)."""
    lo = min(UI_ACTION_DELAY_MIN_S, UI_ACTION_DELAY_MAX_S)
    hi = max(UI_ACTION_DELAY_MIN_S, UI_ACTION_DELAY_MAX_S)
    if hi <= 0:
        return 0.0
    sec = random.uniform(lo, hi)
    if step:
        logger.info("Playwright UI delay %.1fs — %s", sec, step)
    else:
        logger.info("Playwright UI delay %.1fs", sec)
    await asyncio.sleep(sec)
    return sec


async def _open_menu_scope_locator(page: Any) -> Any | None:
    """Popover/dialog/menu đang mở sau bấm nút Tạo (add_2)."""
    selectors = (
        '[role="menu"][data-state="open"]',
        '[role="dialog"][data-state="open"]',
        '[data-radix-popper-content-wrapper]',
        '[role="listbox"][data-state="open"]',
    )
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if await loc.count() > 0:
                return loc.last
        except Exception:
            continue
    return None


async def _is_clickable_menu_target(cand: Any) -> bool:
    """Bỏ qua ảnh / vùng canvas — chỉ menuitem hoặc control trong popover."""
    try:
        tag = (await cand.evaluate("el => el.tagName") or "").upper()
        if tag in ("IMG", "VIDEO", "CANVAS", "PICTURE"):
            return False
        role = (await cand.get_attribute("role") or "").lower()
        if role in ("menuitem", "option"):
            return True
        if tag in ("BUTTON", "A") or role == "button":
            return True
        # div/span chỉ chấp nhận nếu nằm trong menu/dialog, không phải ảnh lớn giữa màn
        box = await cand.bounding_box()
        if box and (box.get("width") or 0) > 400 and (box.get("height") or 0) > 200:
            return False
        parent_role = await cand.evaluate(
            """el => {
              const m = el.closest('[role="menu"], [role="dialog"], [data-radix-popper-content-wrapper]');
              return m ? m.getAttribute('role') || 'popover' : '';
            }"""
        )
        return bool(parent_role)
    except Exception:
        return False


async def _prompt_bar_y_anchor(page: Any) -> float | None:
    """Y của thanh prompt — nút + phải gần vị trí này."""
    prompt = await _prompt_input_locator(page)
    if prompt is None:
        return None
    try:
        box = await prompt.bounding_box()
        if box:
            return float(box.get("y") or 0)
    except Exception:
        pass
    return None


async def _pick_bottommost_visible(
    locator: Any, *, near_y: float | None = None, y_tolerance: float = 180
) -> Any | None:
    count = await locator.count()
    best = None
    best_y = -1.0
    for idx in range(count):
        cand = locator.nth(idx)
        try:
            if not await cand.is_visible():
                continue
            box = await cand.bounding_box()
            if not box:
                continue
            y = float(box.get("y") or 0)
            if near_y is not None and abs(y - near_y) > y_tolerance:
                continue
            if y >= best_y:
                best_y = y
                best = cand
        except Exception:
            continue
    return best


async def _prompt_create_button_locator(page: Any, root: Any | None = None) -> Any | None:
    """
    Nút 'Tạo' (icon add_2) trong thanh prompt — từ DevTools user cung cấp.
    Không dùng nút + header / trợ giúp.
    """
    scope = root if root is not None else page
    near_y = await _prompt_bar_y_anchor(page)
    queries = (
        'button[aria-haspopup="dialog"]:has(i.google-symbols:text-is("add_2"))',
        'button[aria-haspopup="dialog"]:has(i:text-is("add_2"))',
        'button[aria-haspopup="dialog"]:has(i:text("add_2"))',
    )
    for sel in queries:
        try:
            loc = scope.locator(sel)
            pick = await _pick_bottommost_visible(loc, near_y=near_y)
            if pick is not None:
                return pick
        except Exception:
            continue

    try:
        loc = scope.get_by_role("button", name=_CREATE_BTN_NAME)
        count = await loc.count()
        for idx in range(count):
            cand = loc.nth(idx)
            try:
                if await cand.get_attribute("aria-haspopup") != "dialog":
                    continue
                icon = cand.locator("i.google-symbols, i")
                if await icon.count() > 0:
                    txt = (await icon.first.inner_text() or "").strip()
                    if not _PROMPT_ADD_ICON.match(txt):
                        continue
                if await cand.is_visible():
                    if near_y is not None:
                        box = await cand.bounding_box()
                        if box and abs(float(box.get("y") or 0) - near_y) > 180:
                            continue
                    return cand
            except Exception:
                continue
    except Exception:
        pass
    return None


async def _create_dialog_open(page: Any) -> bool:
    try:
        loc = page.locator(
            '[role="dialog"][data-state="open"], [role="menu"][data-state="open"], '
            '[data-radix-popper-content-wrapper] [role="menu"]'
        )
        return await loc.count() > 0
    except Exception:
        return False


async def _prompt_input_locator(page: Any) -> Any | None:
    try:
        loc = page.get_by_placeholder(_PROMPT_PLACEHOLDER)
        if await loc.count() > 0:
            return loc.last
    except Exception:
        pass
    for sel in (
        '[contenteditable="true"][data-placeholder*="tạo"]',
        '[contenteditable="true"]',
        "textarea",
        '[role="textbox"]',
    ):
        try:
            loc = page.locator(sel)
            if await loc.count() > 0:
                return loc.last
        except Exception:
            continue
    return None


async def _prompt_composer_locator(page: Any) -> Any | None:
    """
  Khối chứa ô 'Bạn muốn tạo gì?' + hàng nút [+] / Tác nhân / model (như ảnh UI mới).
  """
    prompt = await _prompt_input_locator(page)
    if prompt is None:
        return None
    try:
        bar = prompt.locator(
            "xpath=ancestor::*[.//button or .//*[@role='button']][position()<=8][last()]"
        )
        if await bar.count() > 0:
            return bar.last
    except Exception:
        pass
    try:
        bar = prompt.locator("xpath=ancestor::form | ancestor::*[contains(@class,'prompt') or contains(@class,'composer')][1]")
        if await bar.count() > 0:
            return bar.first
    except Exception:
        pass
    return None


async def _upload_menu_visible(page: Any) -> bool:
    """Sau khi bấm nút Tạo (add_2) → dialog/menu upload mở."""
    if not await _create_dialog_open(page):
        return False
    return await _has_upload_item_visible(page)


async def _click_prompt_bar_plus(page: Any) -> bool:
    """
    Bấm nút DevTools: button[aria-haspopup=dialog] + icon add_2 (accessible name: Tạo).
  """
    composer = await _prompt_composer_locator(page)
    prompt = await _prompt_input_locator(page)
    if prompt is not None:
        try:
            await prompt.scroll_into_view_if_needed(timeout=5000)
            await asyncio.sleep(0.3)
        except Exception:
            pass

    loc = await _prompt_create_button_locator(page, composer)
    if loc is None:
        loc = await _prompt_create_button_locator(page, page)
    if loc is None:
        logger.warning("Playwright UI: không tìm thấy button add_2 / Tạo trong prompt bar")
        return False

    try:
        await loc.scroll_into_view_if_needed(timeout=5000)
        await loc.click(timeout=10000)
        await _ui_delay("sau bấm nút Tạo (add_2)")
        if await _upload_menu_visible(page):
            logger.info("Playwright UI: đã bấm nút Tạo (add_2) — menu/dialog upload mở")
            return True
        logger.warning("Playwright UI: đã bấm add_2 nhưng chưa thấy menu upload")
        return False
    except Exception as exc:
        logger.warning("Playwright UI: click add_2 failed: %s", exc)
        return False


async def _flow_home_list_visible(page: Any) -> bool:
    """Trang danh sách dự án (có '+ Dự án mới'), chưa vào editor."""
    try:
        if await page.get_by_text(_NEW_PROJECT_PATTERNS).count() > 0:
            return True
    except Exception:
        pass
    try:
        return await page.locator("a, button, [role='button']").filter(
            has_text=_NEW_PROJECT_LOOSE
        ).count() > 0
    except Exception:
        return False


async def _upload_item_locator(page: Any) -> Any | None:
    """Menuitem upload trong dialog/menu đang mở sau nút add_2 — không quét toàn trang."""
    menu = await _open_menu_scope_locator(page)
    if menu is None:
        return None

    try:
        item = menu.get_by_role("menuitem", name=_UPLOAD_MENU_PATTERNS)
        count = await item.count()
        for idx in range(count):
            cand = item.nth(idx)
            try:
                if await cand.is_visible() and await _is_clickable_menu_target(cand):
                    return cand
            except Exception:
                continue
    except Exception:
        pass

    try:
        item = menu.locator("[role='menuitem'], [role='option']").filter(has_text=_UPLOAD_MENU_PATTERNS)
        count = await item.count()
        for idx in range(count):
            cand = item.nth(idx)
            try:
                txt = (await cand.inner_text() or "").strip()
                if _SIDEBAR_UPLOAD.search(txt) and not _UPLOAD_MENU_PATTERNS.search(txt):
                    continue
                if await cand.is_visible() and await _is_clickable_menu_target(cand):
                    return cand
            except Exception:
                continue
    except Exception:
        pass

    try:
        item = menu.locator("button, [role='button']").filter(has_text=_UPLOAD_MENU_PATTERNS)
        count = await item.count()
        for idx in range(count):
            cand = item.nth(idx)
            try:
                if await cand.is_visible() and await _is_clickable_menu_target(cand):
                    return cand
            except Exception:
                continue
    except Exception:
        pass
    return None


async def _has_upload_item_visible(page: Any) -> bool:
    return (await _upload_item_locator(page)) is not None


async def _click_upload_menu_item(page: Any) -> bool:
    if not await _create_dialog_open(page):
        logger.warning("Playwright UI: menu upload chưa mở — cần bấm nút + (add_2) trước")
        return False
    loc = await _upload_item_locator(page)
    if loc is None:
        return False
    try:
        txt = ((await loc.inner_text()) or "").strip()[:80]
        logger.info("Playwright UI: bấm menu upload: %r", txt)
        await loc.click(timeout=15000)
        await _ui_delay("sau bấm Tải nội dung nghe nhìn lên")
        logger.info("Playwright UI: đã bấm 'Tải nội dung nghe nhìn lên'")
        return True
    except Exception as exc:
        logger.warning("Playwright UI: click upload item failed: %s", exc)
        return False


async def _set_files_via_hidden_input(page: Any, image_path: Path) -> bool:
    try:
        loc = page.locator('input[type="file"]')
        count = await loc.count()
        for idx in range(count):
            inp = loc.nth(idx)
            try:
                await inp.set_input_files(str(image_path), timeout=5000)
                await _ui_delay("sau chọn file ảnh")
                logger.info("Playwright UI: set_input_files qua input[type=file] #%s", idx)
                return True
            except Exception:
                continue
    except Exception as exc:
        logger.debug("hidden file input: %s", exc)
    return False


async def _open_image_upload_menu(page: Any) -> bool:
    """
    Mở menu [+] (nếu UI mới). Chưa bấm item upload — bấm item trong expect_file_chooser.
    """
    if not await _wait_for_flow_editor(page, timeout_s=20):
        logger.error(
            "Playwright UI: editor chưa sẵn sàng url=%s — không mở menu upload",
            (page.url or "")[:120],
        )
        return False

    if await _create_dialog_open(page) and await _has_upload_item_visible(page):
        logger.info("Playwright UI: menu upload đã hiện sẵn")
        return True

    logger.info("Playwright UI: bấm nút Tạo (add_2) url=%s", (page.url or "")[:80])
    for attempt in range(3):
        if await _click_prompt_bar_plus(page):
            if await _has_upload_item_visible(page):
                return True
        await _ui_delay(f"thử mở menu upload lần {attempt + 2}")

    logger.error(
        "Playwright UI: upload_menu_not_open url=%s composer=%s",
        (page.url or "")[:120],
        (await _prompt_composer_locator(page)) is not None,
    )
    return False


async def _has_upload_button(page: Any) -> bool:
    """Editor có sẵn đường upload (nút trực tiếp hoặc qua [+])."""
    if await _has_upload_item_visible(page):
        return True
    composer = await _prompt_composer_locator(page)
    return composer is not None


async def _has_prompt_input(page: Any) -> bool:
    try:
        if await page.get_by_placeholder(_PROMPT_PLACEHOLDER).count() > 0:
            return True
    except Exception:
        pass
    for sel in ('[contenteditable="true"]', "textarea", '[role="textbox"]'):
        try:
            if await page.locator(sel).count() > 0:
                return True
        except Exception:
            continue
    return False


async def _is_flow_editor_ready(page: Any) -> bool:
    """Editor = có nút Tạo (add_2) hoặc composer prompt."""
    composer = await _prompt_composer_locator(page)
    if await _prompt_create_button_locator(page, composer):
        return True
    if await _prompt_create_button_locator(page, page):
        return True
    if composer is not None:
        return True
    return await _has_prompt_input(page)


def _is_flow_home_url(url: str) -> bool:
    """URL dạng listing — SPA có thể vẫn dùng path này khi đã mở project."""
    u = (url or "").lower()
    if "labs.google" not in u or "flow" not in u:
        return False
    if "/project" in u:
        return False
    if re.search(r"/flow/[a-f0-9-]{8,}", u):
        return False
    return "tools/flow" in u


async def _is_on_flow_home_list(page: Any) -> bool:
    """Home thật = URL listing + thấy '+ Dự án mới' + chưa có composer prompt."""
    if not _is_flow_home_url(page.url or ""):
        return False
    if await _prompt_composer_locator(page):
        return False
    return await _flow_home_list_visible(page)


async def ensure_flow_editor_page(page: Any) -> bool:
    """
    Đảm bảo đang ở editor Flow (ô 'Bạn muốn tạo gì?').
    Không goto home nếu user đã mở project — chỉ bấm '+ Dự án mới' khi đang ở list home.
    """
    flow_url = _flow_url()
    current = page.url or ""
    if "labs.google" not in current:
        await page.goto(flow_url, wait_until="domcontentloaded", timeout=60000)
        await _ui_delay("sau mở trang Flow")
        current = page.url or ""

    if await _wait_for_flow_editor(page, timeout_s=12):
        logger.debug("Flow editor ready url=%s", current[:100])
        return False

    if await _is_on_flow_home_list(page):
        logger.info("Flow home list — clicking '+ Dự án mới' url=%s", (page.url or "")[:100])
        clicked = await _click_new_project(page)
        if not clicked:
            raise RuntimeError("new_project_button_not_found")
        if not await _wait_for_flow_editor(page, timeout_s=45):
            raise RuntimeError("flow_editor_not_ready_after_new_project")
        logger.info("Flow editor ready after new project url=%s", (page.url or "")[:100])
        return True

    # Đang trên Flow nhưng editor chưa load — chờ, KHÔNG goto home (tránh đá ra project).
    logger.info("Flow: chờ editor load url=%s", (page.url or "")[:100])
    if await _wait_for_flow_editor(page, timeout_s=30):
        return False

    if await _is_on_flow_home_list(page):
        clicked = await _click_new_project(page)
        if not clicked:
            raise RuntimeError("new_project_button_not_found")
        if not await _wait_for_flow_editor(page, timeout_s=45):
            raise RuntimeError("flow_editor_not_ready_after_new_project")
        return True

    raise RuntimeError(f"flow_editor_not_ready url={(page.url or '')[:120]}")


async def _wait_for_flow_editor(page: Any, *, timeout_s: int = 45) -> bool:
    for _ in range(max(5, timeout_s)):
        if await _is_flow_editor_ready(page):
            return True
        await asyncio.sleep(1.0)
    return False


async def _click_new_project(page: Any) -> bool:
    if await _click_first_matching(page, _NEW_PROJECT_PATTERNS):
        return True
    if await _click_first_matching(page, _NEW_PROJECT_PATTERNS, role="link"):
        return True
    try:
        loc = page.locator("a, button, [role='button'], [role='link']").filter(has_text=_NEW_PROJECT_PATTERNS)
        if await loc.count() > 0:
            await loc.first.click(timeout=15000)
            await _ui_delay("sau bấm + Dự án mới")
            return True
    except Exception:
        pass
    try:
        loc = page.locator("a, button, [role='button'], [role='link']").filter(has_text=_NEW_PROJECT_LOOSE)
        if await loc.count() > 0:
            await loc.first.click(timeout=15000)
            await _ui_delay("sau bấm + Dự án mới")
            return True
    except Exception:
        pass
    try:
        loc = page.get_by_text(_NEW_PROJECT_PATTERNS)
        if await loc.count() > 0:
            await loc.first.click(timeout=15000)
            await _ui_delay("sau bấm + Dự án mới")
            return True
    except Exception:
        pass
    return False


def is_ui_automation_enabled() -> bool:
    return is_playwright_enabled() and UI_AUTOMATION_ENABLED


def is_ui_prep_only() -> bool:
    return UI_PREP_ONLY


def _flow_url() -> str:
    return str(load_config().get("flow_url") or "https://labs.google/fx/vi/tools/flow")


def _base64_to_temp_file(b64: str, *, prefix: str = "flow_upload") -> Path:
    decoded = _decode_image_bytes(b64)
    if not decoded:
        raise ValueError("invalid_image_base64")
    raw, mime = decoded
    ext = "png" if mime == "image/png" else ("webp" if mime == "image/webp" else "jpg")
    tmp = tempfile.NamedTemporaryFile(prefix=prefix, suffix=f".{ext}", delete=False)
    tmp.write(raw)
    tmp.close()
    return Path(tmp.name)


async def _click_first_matching(page: Any, pattern: re.Pattern[str], *, role: str = "button") -> bool:
    try:
        loc = page.get_by_role(role, name=pattern)
        if await loc.count() > 0:
            await loc.first.click(timeout=15000)
            await _ui_delay(f"sau click {role}")
            return True
    except Exception:
        pass
    try:
        loc = page.locator("button, [role='button']").filter(has_text=pattern)
        if await loc.count() > 0:
            await loc.first.click(timeout=15000)
            await _ui_delay("sau click nút")
            return True
    except Exception:
        pass
    return False


def _upload_image_response_matcher(resp: Any) -> bool:
    """Cùng endpoint với flow_sdk.upload_image."""
    url = resp.url or ""
    return UPLOAD_IMAGE_PATH in url or "uploadImage" in url


async def _media_library_dialog(page: Any) -> Any | None:
    """Modal thư viện media (sidebar + preview bên phải)."""
    try:
        loc = page.locator('[role="dialog"][data-state="open"]')
        if await loc.count() > 0:
            return loc.last
    except Exception:
        pass
    return None


async def _large_preview_image_in_dialog(dialog: Any) -> Any | None:
    """Ảnh preview lớn bên phải modal — bỏ thumbnail nhỏ trong danh sách."""
    try:
        imgs = dialog.locator("img")
        count = await imgs.count()
        best = None
        best_area = 0.0
        for idx in range(count):
            img = imgs.nth(idx)
            try:
                if not await img.is_visible():
                    continue
                box = await img.bounding_box()
                if not box:
                    continue
                w = float(box.get("width") or 0)
                h = float(box.get("height") or 0)
                if w < 150 or h < 200:
                    continue
                loaded = await img.evaluate("el => el.complete && el.naturalWidth > 0")
                if not loaded:
                    continue
                area = w * h
                if area > best_area:
                    best_area = area
                    best = img
            except Exception:
                continue
        return best
    except Exception:
        return None


async def _wait_for_upload_preview(
    page: Any,
    *,
    filename: str,
    timeout_s: float | None = None,
) -> bool:
    """
    Chờ ảnh vừa upload hiện ở panel preview (bên phải modal) trước khi bấm Thêm vào câu lệnh.
    """
    limit = timeout_s if timeout_s is not None else UI_UPLOAD_PREVIEW_TIMEOUT_S
    deadline = time.monotonic() + max(5.0, limit)
    stem = Path(filename).name
    logger.info("Playwright UI: chờ preview ảnh %s (tối đa %.0fs)", stem, limit)

    while time.monotonic() < deadline:
        dialog = await _media_library_dialog(page)
        if dialog is None:
            await asyncio.sleep(0.6)
            continue

        preview = await _large_preview_image_in_dialog(dialog)
        if preview is not None:
            if stem:
                try:
                    if await dialog.get_by_text(stem, exact=False).count() > 0:
                        logger.info("Playwright UI: preview sẵn sàng — %s + ảnh lớn bên phải", stem)
                        return True
                except Exception:
                    pass
            try:
                box = await preview.bounding_box()
                logger.info(
                    "Playwright UI: preview sẵn sàng — ảnh %.0fx%.0f",
                    float((box or {}).get("width") or 0),
                    float((box or {}).get("height") or 0),
                )
                return True
            except Exception:
                return True

        await asyncio.sleep(0.8)

    logger.warning("Playwright UI: hết thời gian chờ preview ảnh %s", stem)
    return False


async def _click_add_to_prompt(page: Any) -> bool:
    """Bấm 'Thêm vào câu lệnh' trong modal — không click thumbnail/danh sách."""
    dialog = await _media_library_dialog(page)
    scopes: list[Any] = []
    if dialog is not None:
        scopes.append(dialog)
    scopes.append(page)

    for scope in scopes:
        try:
            loc = scope.get_by_role("button", name=_ADD_TO_PROMPT_PATTERNS)
            count = await loc.count()
            for idx in range(count - 1, -1, -1):
                btn = loc.nth(idx)
                try:
                    if not await btn.is_visible():
                        continue
                    if not await btn.is_enabled():
                        continue
                    await btn.scroll_into_view_if_needed(timeout=5000)
                    await btn.click(timeout=15000)
                    await _ui_delay("sau bấm Thêm vào câu lệnh")
                    logger.info("Playwright UI: đã bấm Thêm vào câu lệnh")
                    return True
                except Exception:
                    continue
        except Exception:
            continue

    return await _click_first_matching(page, _ADD_TO_PROMPT_PATTERNS)


def _normalize_image_model_name(model: str) -> str:
    raw = str(model or "").strip()
    if not raw:
        return ""
    upper = raw.upper()
    if upper in _KNOWN_IMAGE_MODELS:
        return _KNOWN_IMAGE_MODELS[upper]
    return raw.replace("_", " ").strip()


def _normalize_aspect_ratio(value: str) -> str:
    raw = str(value or "").strip()
    return raw if raw in _ASPECT_RATIOS else "16:9"


def _panel_has_aspect_toolbar_text(text: str) -> bool:
    txt = str(text or "")
    return all(r in txt for r in _ASPECT_RATIOS)


def _is_media_library_panel_text(text: str) -> bool:
    """Modal thư viện upload — không phải panel cài đặt model/tỉ lệ."""
    txt = str(text or "")
    if "Tìm kiếm thành phần" in txt:
        return True
    if "Thêm vào câu lệnh" in txt and not _panel_has_aspect_toolbar_text(txt):
        return True
    if _UPLOAD_TEXT.search(txt) and not _panel_has_aspect_toolbar_text(txt):
        return True
    return False


async def _settings_popover_open(page: Any) -> Any | None:
    """Popover cài đặt model + tỉ lệ (UI gộp Image+Video)."""
    selectors = (
        '[role="dialog"][data-state="open"]',
        '[data-radix-popper-content-wrapper]',
        '[role="menu"][data-state="open"]',
    )
    for sel in selectors:
        try:
            loc = page.locator(sel)
            count = await loc.count()
            for idx in range(count - 1, -1, -1):
                cand = loc.nth(idx)
                try:
                    if not await cand.is_visible():
                        continue
                    txt = (await cand.inner_text() or "").strip()
                    if _is_media_library_panel_text(txt):
                        continue
                    if _panel_has_aspect_toolbar_text(txt):
                        return cand
                    if "Nano Banana" in txt and any(r in txt for r in _ASPECT_RATIOS):
                        return cand
                except Exception:
                    continue
        except Exception:
            continue
    return None


async def _model_chip_button_locator(page: Any) -> Any | None:
    """Chip model ở prompt bar (Nano Banana + 1x) — không phải nút + upload."""
    composer = await _prompt_composer_locator(page)
    near_y = await _prompt_bar_y_anchor(page)
    scopes = [composer, page] if composer is not None else [page]
    for scope in scopes:
        if scope is None:
            continue
        try:
            # Candidate rộng hơn: nhiều UI không chứa "Nano Banana" rõ ràng.
            loc = scope.locator("button, [role='button']")
            count = await loc.count()
            best = None
            best_score = -1
            for idx in range(count):
                cand = loc.nth(idx)
                try:
                    if not await cand.is_visible():
                        continue
                    label = (await cand.inner_text() or "").strip()
                    if not label:
                        continue
                    if _UPLOAD_TEXT.search(label):
                        continue
                    if _PROMPT_ADD_ICON.search(label):
                        continue
                    if "tác nhân" in label.lower():
                        continue
                    if _is_variant_count_label(label):
                        continue
                    box = await cand.bounding_box()
                    if near_y is not None and box and abs(float(box.get("y") or 0) - near_y) > 220:
                        continue
                    score = 0
                    if _MODEL_CHIP_PATTERNS.search(label):
                        score += 4
                    if any(v in label.lower() for v in ("1x", "x1")):
                        score += 3
                    if any(r in label for r in _ASPECT_RATIOS):
                        score += 2
                    # tránh nút + create có aria-haspopup dialog
                    icon = cand.locator("i.google-symbols, i")
                    if await icon.count() > 0:
                        txt = (await icon.first.inner_text() or "").strip()
                        if _PROMPT_ADD_ICON.match(txt):
                            continue
                    if score > best_score:
                        best_score = score
                        best = cand
                except Exception:
                    continue
            if best is not None and best_score >= 2:
                return best
        except Exception:
            continue

    # Fallback 1: text "Nano Banana ..." rồi leo lên phần tử clickable gần nhất.
    for scope in scopes:
        if scope is None:
            continue
        try:
            txt_hits = scope.get_by_text(_MODEL_CHIP_ROW_PATTERNS)
            count = await txt_hits.count()
            for idx in range(count):
                node = txt_hits.nth(idx)
                try:
                    if not await node.is_visible():
                        continue
                    clickables = node.locator(
                        "xpath=ancestor-or-self::button[1] | ancestor-or-self::*[@role='button'][1]"
                    )
                    if await clickables.count() > 0:
                        cand = clickables.first
                        label = (await cand.inner_text() or "").strip()
                        if _UPLOAD_TEXT.search(label):
                            continue
                        if _PROMPT_ADD_ICON.search(label):
                            continue
                        return cand
                except Exception:
                    continue
        except Exception:
            continue

    # Fallback 2: nút mũi tên bên phải hàng model (nếu chip tách làm 2 phần).
    for scope in scopes:
        if scope is None:
            continue
        try:
            arrows = scope.locator(
                "button:has(i.google-symbols:text-matches('arrow_forward|chevron_right|navigate_next')),"
                " [role='button']:has(i.google-symbols:text-matches('arrow_forward|chevron_right|navigate_next'))"
            )
            count = await arrows.count()
            for idx in range(count):
                cand = arrows.nth(idx)
                try:
                    if not await cand.is_visible():
                        continue
                    box = await cand.bounding_box()
                    if near_y is not None and box and abs(float(box.get("y") or 0) - near_y) > 260:
                        continue
                    return cand
                except Exception:
                    continue
        except Exception:
            continue
    return None


async def _open_image_settings_panel(page: Any) -> Any | None:
    """Bấm chip model ở prompt bar để mở panel cài đặt (gộp Image+Video)."""
    for attempt in range(3):
        chip = await _model_chip_button_locator(page)
        if chip is None:
            logger.warning("Playwright UI: không tìm thấy chip model trên prompt bar (lần %s)", attempt + 1)
            await asyncio.sleep(0.6)
            continue
        try:
            try:
                logger.info(
                    "Playwright UI: model chip candidate #%s text=%r",
                    attempt + 1,
                    ((await chip.inner_text()) or "").strip()[:120],
                )
            except Exception:
                pass
            await chip.scroll_into_view_if_needed(timeout=5000)
            await chip.click(timeout=10000)
            await _ui_delay("sau mở panel model")
            panel = await _settings_popover_open(page)
            if panel is not None:
                return panel
            await asyncio.sleep(0.5)
        except Exception as exc:
            logger.warning("Playwright UI: mở panel model failed (lần %s): %s", attempt + 1, exc)
    logger.warning("Playwright UI: không mở được panel model settings")
    return None


def _is_variant_count_label(text: str) -> bool:
    """Nút số lượng 1x/x2/x3/x4 — mặc định 1x, không bấm."""
    raw = str(text or "").strip()
    if not raw:
        return False
    for line in raw.splitlines():
        if line.strip().lower() in _VARIANT_COUNT_LABELS:
            return True
    compact = re.sub(r"\s+", "", raw.lower())
    return compact in _VARIANT_COUNT_LABELS


def _label_matches(text: str, target: str) -> bool:
    raw = str(text or "").strip()
    want = str(target or "").strip()
    if not raw or not want:
        return False
    if raw == want:
        return True
    for line in raw.splitlines():
        if line.strip() == want:
            return True
    compact = re.sub(r"\s+", " ", raw)
    return compact == want or compact.endswith(f" {want}") or compact.endswith(want)


async def _click_exact_label(scope: Any, label: str) -> bool:
    """Click phần tử có text khớp chính xác (tránh Nano Banana 2 vs Lite)."""
    target = str(label or "").strip()
    if not target:
        return False
    try:
        loc = scope.locator(
            "button, [role='button'], [role='menuitem'], [role='option'], [role='radio'], [role='tab'], div, span"
        )
        count = await loc.count()
        for idx in range(count):
            cand = loc.nth(idx)
            try:
                if not await cand.is_visible():
                    continue
                txt = (await cand.inner_text() or "").strip()
                if _is_variant_count_label(txt):
                    continue
                if not _label_matches(txt, target):
                    continue
                tag = (await cand.evaluate("el => el.tagName") or "").upper()
                if tag in ("DIV", "SPAN"):
                    btn = cand.locator(
                        "xpath=ancestor-or-self::button[1] | ancestor-or-self::*[@role='button'][1] | ancestor-or-self::*[@role='radio'][1]"
                    )
                    if await btn.count() > 0:
                        await btn.first.click(timeout=10000)
                    else:
                        await cand.click(timeout=10000)
                else:
                    await cand.click(timeout=10000)
                return True
            except Exception:
                continue
    except Exception:
        pass
    return False


async def _ensure_media_settings_ready(panel: Any) -> bool:
    """
    UI mặc định gộp Upload Image + Video — không còn tab Hình ảnh / Video riêng.
    Chỉ bấm tab Hình ảnh nếu gặp UI cũ (có tab nhưng chưa thấy hàng aspect ratio).
    """
    if await _aspect_ratio_toolbar_locator(panel) is not None:
        logger.info("Playwright UI: panel gộp Image+Video — bỏ qua tab riêng")
        return True

    # UI cũ: còn tab Hình ảnh / Video
    legacy_image = re.compile(r"^hình ảnh$|^image$", re.I)
    try:
        tabs = panel.get_by_role("tab", name=legacy_image)
        if await tabs.count() > 0:
            await tabs.first.click(timeout=10000)
            await _ui_delay("sau chọn tab Hình ảnh (UI cũ)")
            return True
    except Exception:
        pass
    if await _click_exact_label(panel, "Hình ảnh"):
        await _ui_delay("sau chọn tab Hình ảnh (UI cũ)")
        return True

    logger.warning("Playwright UI: chưa thấy toolbar aspect ratio trong panel settings")
    return False


def _button_matches_single_ratio(text: str, target: str) -> bool:
    """Nút aspect ratio chỉ chứa đúng 1 label (vd. 9:16), không phải x2/x3/x4."""
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    if any(_is_variant_count_label(ln) for ln in lines):
        return False
    found = [r for r in _ASPECT_RATIOS if any(ln == r for ln in lines)]
    return target in found and len(found) == 1


async def _aspect_ratio_toolbar_locator(panel: Any) -> Any | None:
    """Hàng 5 nút aspect ratio — không gồm hàng 1x/x2/x3/x4."""
    try:
        radiogroup = panel.get_by_role("radiogroup")
        if await radiogroup.count() > 0:
            best = None
            best_score = -1
            for idx in range(await radiogroup.count()):
                cand = radiogroup.nth(idx)
                try:
                    txt = (await cand.inner_text() or "")
                    if not all(r in txt for r in _ASPECT_RATIOS):
                        continue
                    # Ưu tiên nhóm chỉ có ratio, không có x2/x3/x4
                    score = 10
                    if _is_variant_count_label(txt):
                        score = 1
                    if score > best_score:
                        best_score = score
                        best = cand
                except Exception:
                    continue
            if best is not None:
                return best
    except Exception:
        pass

    try:
        loc = panel.locator(
            "xpath=.//*[.//*[normalize-space(text())='16:9']"
            " and .//*[normalize-space(text())='4:3']"
            " and .//*[normalize-space(text())='1:1']"
            " and .//*[normalize-space(text())='3:4']"
            " and .//*[normalize-space(text())='9:16']"
            " and not(.//*[normalize-space(text())='x2'])]"
        )
        count = await loc.count()
        best = None
        best_area = float("inf")
        for idx in range(count):
            cand = loc.nth(idx)
            try:
                if not await cand.is_visible():
                    continue
                box = await cand.bounding_box()
                if not box:
                    continue
                area = float(box.get("width") or 0) * float(box.get("height") or 0)
                if 0 < area < best_area:
                    best_area = area
                    best = cand
            except Exception:
                continue
        if best is not None:
            return best
    except Exception:
        pass

    # Fallback: hàng ratio kể cả khi nằm chung panel với x2/x3/x4
    try:
        loc = panel.locator(
            "xpath=.//*[.//*[normalize-space(text())='16:9']"
            " and .//*[normalize-space(text())='4:3']"
            " and .//*[normalize-space(text())='1:1']"
            " and .//*[normalize-space(text())='3:4']"
            " and .//*[normalize-space(text())='9:16']]"
        )
        count = await loc.count()
        best = None
        best_area = float("inf")
        for idx in range(count):
            cand = loc.nth(idx)
            try:
                if not await cand.is_visible():
                    continue
                txt = (await cand.inner_text() or "")
                if "x2" in txt or "x3" in txt or "x4" in txt:
                    if not all(r in txt for r in _ASPECT_RATIOS):
                        continue
                box = await cand.bounding_box()
                if not box:
                    continue
                area = float(box.get("width") or 0) * float(box.get("height") or 0)
                if 0 < area < best_area:
                    best_area = area
                    best = cand
            except Exception:
                continue
        if best is not None:
            return best
    except Exception:
        pass
    return None


async def _select_aspect_ratio(panel: Any, aspect_ratio: str) -> bool:
    target = _normalize_aspect_ratio(aspect_ratio)
    toolbar = await _aspect_ratio_toolbar_locator(panel)
    if toolbar is None:
        logger.warning("Playwright UI: không tìm thấy hàng aspect ratio")
        return False

    logger.info("Playwright UI: chọn aspect ratio %s trong toolbar", target)

    # Ưu tiên role=radio trong radiogroup (bỏ qua 1x/x2/x3/x4)
    try:
        radio = toolbar.get_by_role("radio", name=re.compile(rf"^{re.escape(target)}$"))
        count = await radio.count()
        for idx in range(count):
            cand = radio.nth(idx)
            try:
                if not await cand.is_visible():
                    continue
                txt = (await cand.inner_text() or "").strip()
                if _is_variant_count_label(txt):
                    continue
                await cand.click(timeout=10000)
                await _ui_delay(f"sau chọn tỉ lệ {target}")
                logger.info("Playwright UI: đã chọn aspect ratio %s (radio)", target)
                return True
            except Exception:
                continue
    except Exception:
        pass

    # Nút từng ô — inner_text chỉ có đúng 1 ratio, không phải x2/x3/x4
    try:
        buttons = toolbar.locator("button, [role='button'], [role='radio']")
        count = await buttons.count()
        for idx in range(count):
            btn = buttons.nth(idx)
            try:
                if not await btn.is_visible():
                    continue
                txt = (await btn.inner_text() or "").strip()
                if _is_variant_count_label(txt):
                    continue
                if not _button_matches_single_ratio(txt, target):
                    continue
                await btn.click(timeout=10000)
                await _ui_delay(f"sau chọn tỉ lệ {target}")
                logger.info("Playwright UI: đã chọn aspect ratio %s (button)", target)
                return True
            except Exception:
                continue
    except Exception:
        pass

    # Fallback: text đúng trong toolbar → click ancestor button
    try:
        hits = toolbar.get_by_text(target, exact=True)
        count = await hits.count()
        for idx in range(count):
            node = hits.nth(idx)
            try:
                if not await node.is_visible():
                    continue
                btn = node.locator(
                    "xpath=ancestor-or-self::button[1] | ancestor-or-self::*[@role='button'][1] | ancestor-or-self::*[@role='radio'][1]"
                )
                if await btn.count() == 0:
                    continue
                pick = btn.first
                txt = (await pick.inner_text() or "").strip()
                if _is_variant_count_label(txt):
                    continue
                if not _button_matches_single_ratio(txt, target):
                    continue
                await pick.click(timeout=10000)
                await _ui_delay(f"sau chọn tỉ lệ {target}")
                logger.info("Playwright UI: đã chọn aspect ratio %s (text ancestor)", target)
                return True
            except Exception:
                continue
    except Exception:
        pass

    logger.warning("Playwright UI: không chọn được aspect ratio=%s", target)
    return False


async def _open_model_dropdown_in_panel(panel: Any) -> bool:
    """Bấm hàng chọn model (Nano Banana ...) trong panel settings."""
    try:
        loc = panel.locator("button, [role='button']").filter(has_text=_MODEL_CHIP_PATTERNS)
        count = await loc.count()
        for idx in range(count):
            cand = loc.nth(idx)
            try:
                if not await cand.is_visible():
                    continue
                txt = (await cand.inner_text() or "").strip()
                if _UPLOAD_TEXT.search(txt):
                    continue
                await cand.click(timeout=10000)
                await _ui_delay("sau mở danh sách model image")
                return True
            except Exception:
                continue
    except Exception:
        pass
    return False


async def _model_dropdown_scope(page: Any) -> Any | None:
    """Menu dropdown model sau bấm Nano Banana trong panel."""
    selectors = (
        '[role="menu"][data-state="open"]',
        '[role="listbox"][data-state="open"]',
        '[data-radix-popper-content-wrapper]',
    )
    for sel in selectors:
        try:
            loc = page.locator(sel)
            count = await loc.count()
            for idx in range(count - 1, -1, -1):
                cand = loc.nth(idx)
                try:
                    if not await cand.is_visible():
                        continue
                    txt = (await cand.inner_text() or "").strip()
                    if "Nano Banana" in txt and _UPLOAD_TEXT.search(txt) is None:
                        return cand
                except Exception:
                    continue
        except Exception:
            continue
    return page


async def _select_image_model(page: Any, panel: Any, image_model: str) -> bool:
    target = _normalize_image_model_name(image_model)
    if not target:
        return False

    if not await _open_model_dropdown_in_panel(panel):
        logger.warning("Playwright UI: không mở được dropdown model")
        return False

    menu = await _model_dropdown_scope(page)
    if menu is None:
        return False

    if await _click_exact_label(menu, target):
        await _ui_delay(f"sau chọn model {target}")
        logger.info("Playwright UI: đã chọn model %s", target)
        return True

    logger.warning("Playwright UI: không chọn được model image=%s", target)
    return False


async def _dismiss_settings_panel(page: Any) -> bool:
    """Click ra ngoài để đóng panel model trước khi upload."""
    if await _settings_popover_open(page) is None:
        return True

    prompt = await _prompt_input_locator(page)
    if prompt is not None:
        try:
            await prompt.click(timeout=5000)
            await _ui_delay("sau click ra ngoài đóng panel model")
        except Exception:
            pass

    if await _settings_popover_open(page) is not None:
        try:
            await page.keyboard.press("Escape")
            await _ui_delay("sau Escape đóng panel model")
        except Exception:
            pass

    closed = await _settings_popover_open(page) is None
    if not closed:
        logger.warning("Playwright UI: panel model vẫn còn mở sau dismiss")
    return closed


async def _chip_shows_model(page: Any, image_model: str) -> bool:
    target = _normalize_image_model_name(image_model)
    chip = await _model_chip_button_locator(page)
    if chip is None or not target:
        return False
    try:
        txt = (await chip.inner_text() or "").strip()
        return target.lower() in txt.lower()
    except Exception:
        return False


async def configure_image_generation_ui(
    page: Any,
    *,
    aspect_ratio: str,
    image_model: str,
) -> dict[str, Any]:
    """
    Nhánh image — UI gộp Image+Video:
    1) chip model  2) aspect ratio  3) chọn model
    4) click ra ngoài đóng panel  →  sau đó mới upload (nếu có ảnh).
    """
    target_ratio = _normalize_aspect_ratio(aspect_ratio)
    target_model = _normalize_image_model_name(image_model)

    panel = await _open_image_settings_panel(page)
    if panel is None:
        return {
            "configured": False,
            "aspect_ratio_selected": False,
            "image_model_selected": False,
            "panel_dismissed": False,
            "unified_media_ui": True,
            "aspect_ratio": target_ratio,
            "image_model": target_model,
        }

    media_ready = await _ensure_media_settings_ready(panel)
    logger.info("Playwright UI: giữ mặc định 1x — bỏ qua x2/x3/x4")
    ratio_ok = await _select_aspect_ratio(panel, target_ratio) if media_ready else False
    model_ok = await _select_image_model(page, panel, target_model)
    dismissed = await _dismiss_settings_panel(page)
    chip_ok = await _chip_shows_model(page, target_model) if model_ok else False

    configured = media_ready and ratio_ok and model_ok and dismissed
    if not configured:
        logger.warning(
            "Playwright UI: image settings chưa đủ media=%s ratio=%s model=%s dismissed=%s chip=%s",
            media_ready,
            ratio_ok,
            model_ok,
            dismissed,
            chip_ok,
        )

    return {
        "configured": configured,
        "unified_media_ui": True,
        "media_panel_ready": media_ready,
        "aspect_ratio_selected": ratio_ok,
        "image_model_selected": model_ok,
        "panel_dismissed": dismissed,
        "chip_verified": chip_ok,
        "aspect_ratio": target_ratio,
        "image_model": target_model,
    }


async def upload_image_via_ui(
    page: Any,
    *,
    image_path: Path,
) -> dict[str, Any]:
    """
    Chỉ gọi khi request có ảnh (image_base64s).
    UI flow: [+] → 'Tải nội dung nghe nhìn lên' → chọn file → 'Thêm vào câu lệnh'
    """
    if await _settings_popover_open(page) is not None:
        await _dismiss_settings_panel(page)

    attempts = max(1, int(UI_UPLOAD_RETRY_MAX or 1))
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            if not await _open_image_upload_menu(page):
                raise RuntimeError(f"upload_menu_not_open url={(page.url or '')[:120]}")
            await _ui_delay(f"trước bấm upload / chọn file (lần {attempt}/{attempts})")

            response_matcher = _upload_image_response_matcher
            uploaded = False
            resp = None

            # Cách 1: file chooser sau khi bấm item upload
            try:
                async with page.expect_response(response_matcher, timeout=120_000) as resp_info:
                    async with page.expect_file_chooser(timeout=45_000) as fc_info:
                        if not await _click_upload_menu_item(page):
                            raise RuntimeError("upload_button_not_found")
                        chooser = await fc_info.value
                        await chooser.set_files(str(image_path))
                        await _ui_delay("sau chọn file (file chooser)")
                    resp = await resp_info.value
                uploaded = True
            except Exception as exc:
                logger.warning(
                    "Playwright UI: file chooser upload failed: %s — thử input[type=file]",
                    exc,
                )

            # Cách 2: input file ẩn (một số bản Flow không bật file chooser CDP)
            if not uploaded:
                await _open_image_upload_menu(page)
                async with page.expect_response(response_matcher, timeout=120_000) as resp_info:
                    if not await _set_files_via_hidden_input(page, image_path):
                        if not await _click_upload_menu_item(page):
                            raise RuntimeError("upload_button_not_found")
                        await _ui_delay("chờ input file sau click upload")
                        if not await _set_files_via_hidden_input(page, image_path):
                            raise RuntimeError("file_chooser_timeout")
                    resp = await resp_info.value

            status = int(resp.status or 0)
            body: Any = None
            try:
                body = await resp.json()
            except Exception:
                body = await resp.text()
            if status >= 400:
                raise RuntimeError(f"upload_image_http_{status}: {body}")

            if not await _wait_for_upload_preview(page, filename=image_path.name):
                raise RuntimeError("upload_preview_not_ready")

            added = await _click_add_to_prompt(page)
            if not added:
                logger.warning("add_to_prompt button not found — ảnh có thể đã được gắn tự động")

            if attempt > 1:
                logger.info("Playwright UI: upload ảnh thành công sau %s lần thử", attempt)
            return {"status": status, "data": body, "attempt": attempt}
        except Exception as exc:
            last_error = exc
            if attempt >= attempts:
                break
            logger.warning(
                "Playwright UI: upload lỗi lần %s/%s (%s) — sẽ thử lại",
                attempt,
                attempts,
                exc,
            )
            await _ui_delay("chờ trước khi retry upload")

    raise RuntimeError(f"upload_retry_exhausted({attempts}): {last_error}")


async def fill_prompt(page: Any, prompt: str) -> None:
    text = str(prompt or "").strip()
    if not text:
        return

    await _ui_delay("trước điền prompt")

    try:
        loc = page.get_by_placeholder(_PROMPT_PLACEHOLDER)
        if await loc.count() > 0:
            target = loc.last
            await target.click(timeout=8000)
            await _ui_delay("sau focus ô prompt")
            await target.fill(text, timeout=8000)
            await _ui_delay("sau điền prompt")
            return
    except Exception:
        pass

    selectors = [
        '[contenteditable="true"][data-placeholder]',
        '[contenteditable="true"]',
        "textarea[placeholder]",
        "textarea",
        '[role="textbox"]',
    ]
    for sel in selectors:
        loc = page.locator(sel)
        try:
            if await loc.count() == 0:
                continue
            target = loc.last
            await target.click(timeout=8000)
            await _ui_delay("sau focus ô prompt")
            await target.fill(text, timeout=8000)
            await _ui_delay("sau điền prompt")
            return
        except Exception:
            continue

    # Fallback: keyboard after focusing editable area
    editable = page.locator('[contenteditable="true"]').last
    await editable.click(timeout=8000)
    await _ui_delay("sau focus contenteditable")
    await page.keyboard.press("Control+A")
    await page.keyboard.type(text, delay=15)
    await _ui_delay("sau điền prompt (keyboard)")


def _is_image_generate_response(resp: Any) -> bool:
    try:
        url = str(resp.url or "")
    except Exception:
        return False
    return bool(_GENERATE_IMAGE_API_PATTERNS.search(url))


async def _submit_arrow_button(page: Any) -> Any | None:
    """Nút gửi ở bên phải prompt bar (mũi tên tròn)."""
    composer = await _prompt_composer_locator(page)
    scopes = [composer, page] if composer is not None else [page]
    icon_re = re.compile(r"arrow_forward|send|north_east|arrow_upward|chevron_right", re.I)
    for scope in scopes:
        if scope is None:
            continue
        try:
            loc = scope.locator(
                "button:has(i.google-symbols), [role='button']:has(i.google-symbols)"
            )
            count = await loc.count()
            for idx in range(count - 1, -1, -1):
                cand = loc.nth(idx)
                try:
                    if not await cand.is_visible():
                        continue
                    if not await cand.is_enabled():
                        continue
                    icon = cand.locator("i.google-symbols, i")
                    if await icon.count() == 0:
                        continue
                    txt = (await icon.first.inner_text() or "").strip()
                    if not icon_re.search(txt):
                        continue
                    return cand
                except Exception:
                    continue
        except Exception:
            continue
    return None


async def _submit_prompt_and_wait_image(page: Any, *, timeout_s: int = 300) -> dict[str, Any]:
    """
    Bấm mũi tên gửi và chờ network response ảnh (batchGenerateImages).
    Trả urls/media_ids giống luồng flow_sdk.gen_image hiện tại.
    """
    submit = await _submit_arrow_button(page)
    if submit is None:
        raise RuntimeError("submit_arrow_not_found")

    timeout_ms = max(30_000, int(timeout_s * 1000))
    try:
        async with page.expect_response(_is_image_generate_response, timeout=timeout_ms) as resp_info:
            await submit.scroll_into_view_if_needed(timeout=5000)
            await submit.click(timeout=10000)
            await _ui_delay("sau bấm mũi tên gửi")
        resp = await resp_info.value
    except Exception as exc:
        raise RuntimeError(f"image_submit_wait_response_failed: {exc}") from exc

    status = int(resp.status or 0)
    payload: Any
    try:
        payload = await resp.json()
    except Exception:
        payload = {"raw_text": (await resp.text())[:4000]}

    if status >= 400:
        raise RuntimeError(f"image_submit_http_{status}: {payload}")

    data = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data.get("data")
    if not isinstance(data, dict):
        data = payload if isinstance(payload, dict) else {}

    image_urls = flow_sdk.extract_image_urls(data)
    media_ids = flow_sdk.extract_image_media_ids(data)
    media_entries = flow_sdk.build_image_media_entries(data)

    logger.info(
        "Playwright UI: submit image done status=%s urls=%s media_ids=%s",
        status,
        len(image_urls),
        len(media_ids),
    )
    return {
        "submitted": True,
        "status": status,
        "image_urls": image_urls,
        "media_ids": media_ids,
        "media_entries": media_entries,
        "raw": data,
    }


async def prepare_request_on_flow_ui(
    *,
    profile_id: str,
    request_id: str,
    request_type: str,
    prompt: str,
    image_base64s: list[str] | None = None,
    aspect_ratio: str = "16:9",
    image_model: str = "",
    proxy_url: str | None = None,
) -> dict[str, Any]:
    """
    Automation bước 1: upload ảnh (nếu có) + copy prompt vào ô nhập.
    Traffic đi qua proxy của profile (pool dashboard) hoặc proxy_url truyền vào request.
    """
    if not is_ui_automation_enabled():
        raise RuntimeError("ui_automation_disabled")

    pool = get_playwright_pool()
    session = await pool.get_session(profile_id)

    if proxy_url:
        from flow2api.services.extension_pool import get_extension_pool
        from flow2api.services.system_ops import apply_proxy_to_session, format_proxy_public

        ext = get_extension_pool().get(profile_id)
        if ext and ext.connected:
            await apply_proxy_to_session(ext, str(proxy_url).strip())
            await pool.invalidate_profile(profile_id)
        proxy_info = format_proxy_public(str(proxy_url).strip())
        session._proxy_display = str(proxy_info.get("proxy_display") or "")
    else:
        proxy_info = await session.sync_proxy(force=True)

    page = await session.get_page(
        flow_url_hint=_flow_url(),
        sync_proxy=not bool(proxy_url),
    )
    try:
        await page.bring_to_front()
    except Exception as exc:
        logger.debug("bring_to_front: %s", exc)
    logger.info(
        "Playwright UI start profile=%s url=%s",
        profile_id[:12],
        (page.url or "")[:100],
    )

    opened_new_project = await ensure_flow_editor_page(page)
    await _ui_delay("sau vào editor Flow")

    temp_files: list[Path] = []
    upload_meta: dict[str, Any] | None = None
    image_settings_meta: dict[str, Any] | None = None
    ui_generation_meta: dict[str, Any] | None = None
    try:
        if str(request_type or "").strip().lower() == "gen_image":
            image_settings_meta = await configure_image_generation_ui(
                page,
                aspect_ratio=aspect_ratio,
                image_model=image_model,
            )
            if not image_settings_meta.get("configured"):
                raise RuntimeError(
                    "image_settings_not_configured "
                    f"ratio={image_settings_meta.get('aspect_ratio_selected')} "
                    f"model={image_settings_meta.get('image_model_selected')} "
                    f"dismissed={image_settings_meta.get('panel_dismissed')}"
                )
            await _ui_delay("sau cấu hình model/tỉ lệ — chuẩn bị upload")

        imgs = [x for x in (image_base64s or []) if x]
        if imgs:
            path = _base64_to_temp_file(imgs[0], prefix=f"ui_{request_id[:8]}_")
            temp_files.append(path)
            upload_meta = await upload_image_via_ui(page, image_path=path)
        elif str(prompt or "").strip():
            logger.info("Playwright UI: không có ảnh — chỉ điền prompt (không bấm [+])")

        await fill_prompt(page, prompt)
        if str(request_type or "").strip().lower() == "gen_image" and is_ui_prep_only():
            ui_generation_meta = await _submit_prompt_and_wait_image(page)

        return {
            "ui_prep": True,
            "profile_id": profile_id,
            "request_id": request_id,
            "opened_new_project": opened_new_project,
            "prompt_filled": bool(str(prompt or "").strip()),
            "image_uploaded": bool(imgs),
            "upload": upload_meta,
            "image_settings": image_settings_meta,
            "ui_generated": bool(ui_generation_meta and ui_generation_meta.get("submitted")),
            "ui_generation": ui_generation_meta,
            "page_url": page.url,
            "prep_only": is_ui_prep_only(),
            "proxy": proxy_info.get("proxy_display") or proxy_info.get("proxy") or "",
            "proxy_attached": bool(proxy_info.get("proxy_attached")),
            "proxy_assigned": proxy_info.get("proxy_assigned") or "",
        }
    finally:
        for path in temp_files:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
