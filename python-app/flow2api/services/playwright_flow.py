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
)
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


async def upload_image_via_ui(
    page: Any,
    *,
    image_path: Path,
) -> dict[str, Any]:
    """
    Chỉ gọi khi request có ảnh (image_base64s).
    UI flow: [+] → 'Tải nội dung nghe nhìn lên' → chọn file → 'Thêm vào câu lệnh'
    """
    if not await _open_image_upload_menu(page):
        raise RuntimeError(
            f"upload_menu_not_open url={(page.url or '')[:120]}"
        )
    await _ui_delay("trước bấm upload / chọn file")

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
        logger.warning("Playwright UI: file chooser upload failed: %s — thử input[type=file]", exc)

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

    status = resp.status
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

    return {"status": status, "data": body}


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


async def prepare_request_on_flow_ui(
    *,
    profile_id: str,
    request_id: str,
    prompt: str,
    image_base64s: list[str] | None = None,
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
    try:
        imgs = [x for x in (image_base64s or []) if x]
        if imgs:
            path = _base64_to_temp_file(imgs[0], prefix=f"ui_{request_id[:8]}_")
            temp_files.append(path)
            upload_meta = await upload_image_via_ui(page, image_path=path)
        elif str(prompt or "").strip():
            logger.info("Playwright UI: không có ảnh — chỉ điền prompt (không bấm [+])")

        await fill_prompt(page, prompt)

        return {
            "ui_prep": True,
            "profile_id": profile_id,
            "request_id": request_id,
            "opened_new_project": opened_new_project,
            "prompt_filled": bool(str(prompt or "").strip()),
            "image_uploaded": bool(imgs),
            "upload": upload_meta,
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
