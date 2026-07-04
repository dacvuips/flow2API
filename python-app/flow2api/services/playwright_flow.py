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
    UI_GENERATION_SUBMIT_TIMEOUT_S,
    UI_MOUSE_NUDGE_ENABLED,
    UI_MOUSE_NUDGE_PX,
    UI_PREP_ONLY,
    UI_UPLOAD_PREVIEW_TIMEOUT_S,
    UI_UPLOAD_RETRY_MAX,
    UI_UPLOAD_ERROR_RETRY_WAIT_S,
)
from flow2api.services import flow_sdk
from flow2api.services.flow_sdk import UPLOAD_IMAGE_PATH
from flow2api.services.playwright_pool import get_playwright_pool, is_playwright_enabled
from flow2api.services.result_media import _decode_image_bytes
from flow2api.services.system_ops import load_config

logger = logging.getLogger(__name__)


class UiFlowRestartFromUpload(Exception):
    """Upload hoặc generate lỗi — chạy lại pipeline từ bước upload."""

    def __init__(self, message: str = "", *, cleaned: bool = False) -> None:
        super().__init__(message)
        self.cleaned = cleaned


class FlowHttp403Error(RuntimeError):
    """HTTP 403 từ Flow API — không retry UI pipeline."""


_RESTART_STEPS = frozenset({"upload", "prompt", "submit"})


def _pipeline_restart_index(steps: list[tuple[str, Any]]) -> int:
    """Bước bắt đầu lại: upload (nếu có) rồi prompt → submit."""
    names = [name for name, _ in steps]
    for target in ("upload", "prompt"):
        if target in names:
            return names.index(target)
    return 0


async def _has_generation_error_card(page: Any) -> bool:
    """Kiểm tra nhanh thẻ 「Không thành công」 — không walk ancestor."""
    try:
        hits = page.get_by_text(re.compile(r"không thành công", re.I))
        count = await hits.count()
        for idx in range(min(count, 6)):
            try:
                if await hits.nth(idx).is_visible():
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


async def _prepare_restart_from_upload(page: Any) -> None:
    """Dọn thẻ lỗi / overlay trước khi chạy lại từ upload — không chờ dài."""
    await _dismiss_upload_error_ui(page)
    await _close_floating_overlays(page)
    await _ui_pause(0.2, "sau dọn lỗi — restart upload")


async def _reraise_as_upload_restart(
    page: Any,
    exc: BaseException | None = None,
    *,
    context: str,
) -> None:
    """Upload hoặc generate lỗi — restart pipeline từ bước upload."""
    await _prepare_restart_from_upload(page)
    msg = f"{context}: {exc}" if exc is not None else context
    raise UiFlowRestartFromUpload(msg, cleaned=True) from (
        exc if isinstance(exc, BaseException) else None
    )


async def _restart_from_upload_if_error_card(page: Any, *, context: str) -> None:
    if not await _has_generation_error_card(page):
        return
    await _reraise_as_upload_restart(page, context=f"Không thành công [{context}]")

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
_MODEL_CHIP_VIDEO_PATTERNS = re.compile(r"\bvideo\b|veo 3\.1|omni flash", re.I)
_VIDEO_CHIP_DURATION = re.compile(r"\b\d+s\b", re.I)
# Chip settings — text chứa Nano Banana hoặc dòng bắt đầu Video
_SETTINGS_CHIP_LEADING_IMAGE = re.compile(r"nano\s+banana", re.I)
_SETTINGS_CHIP_LEADING_VIDEO = re.compile(r"^video\b", re.I)
_UPLOAD_TEXT = re.compile(r"tải nội dung|tải tệp|upload", re.I)
_ASPECT_RATIOS = ("16:9", "4:3", "1:1", "3:4", "9:16")
_VIDEO_ASPECT_RATIOS = ("9:16", "16:9")
_VARIANT_COUNT_LABELS = frozenset({"1x", "x1", "x2", "x3", "x4"})
_VARIANT_COUNT_UI = ("1x", "x2", "x3", "x4")
_KNOWN_IMAGE_MODELS: dict[str, str] = {
    "NANO_BANANA_PRO": "Nano Banana Pro",
    "NANO_BANANA_2": "Nano Banana 2",
    "NANO_BANANA_2_LITE": "Nano Banana 2 Lite",
}
_KNOWN_VIDEO_MODELS: dict[str, str] = {
    "lite": "Veo 3.1 - Lite",
    "fast": "Veo 3.1 - Fast",
    "quality": "Veo 3.1 - Quality",
    "lite_relaxed": "Veo 3.1 - Lite [Lower Priority]",
    "omni_flash": "Omni Flash",
}
_GENERATE_IMAGE_API_PATTERNS = re.compile(r"flowMedia:batchGenerateImages|batchGenerateImages", re.I)
_GENERATE_VIDEO_API_PATTERNS = re.compile(
    r"batchAsyncGenerateVideo|batchAsyncGenerateVideoEditVideo|batchGenerateVideo|batchGenerateVideos",
    re.I,
)
_ERROR_SKIP_ICON_PATTERNS = re.compile(
    r"undo|arrow_back|arrow_left|arrow_right|delete|close|clear|cancel|remove",
    re.I,
)
_ERROR_BANNER_PATTERNS = re.compile(
    r"không thành công|không tạo được|unsuccessful|could not create|thử một câu lệnh|hoạt động bất thường",
    re.I,
)
_VIDEO_DURATIONS = ("4s", "6s", "8s", "10s")
_ERROR_RETRY_MAX_CLICKS = 3


async def _ui_pause(sec: float = 0.35, step: str = "") -> None:
    """Chờ ngắn cố định — dùng trong upload (tránh delay 1–5s)."""
    if sec <= 0:
        return
    if step:
        logger.info("Playwright UI pause %.2fs — %s", sec, step)
    await asyncio.sleep(sec)


async def _ui_delay(step: str = "") -> float:
    """Chờ ngẫu nhiên giữa các thao tác UI (mặc định 0–1.5s)."""
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


async def _micro_mouse_wiggle(page: Any, *, near: Any | None = None) -> None:
    """Di chuyển chuột nhẹ quanh vị trí target — giả lập người dùng."""
    if not UI_MOUSE_NUDGE_ENABLED:
        return
    try:
        jitter = max(2.0, float(UI_MOUSE_NUDGE_PX or 10))
        if near is not None:
            box = await near.bounding_box()
            if box and (box.get("width") or 0) > 0 and (box.get("height") or 0) > 0:
                cx = float(box["x"]) + float(box["width"]) * random.uniform(0.38, 0.62)
                cy = float(box["y"]) + float(box["height"]) * random.uniform(0.38, 0.62)
                await page.mouse.move(
                    cx + random.uniform(-jitter, jitter),
                    cy + random.uniform(-jitter, jitter),
                )
                await asyncio.sleep(random.uniform(0.02, 0.07))
                await page.mouse.move(
                    cx + random.uniform(-jitter * 0.35, jitter * 0.35),
                    cy + random.uniform(-jitter * 0.35, jitter * 0.35),
                )
                return
        vp = page.viewport_size or {"width": 1280, "height": 720}
        w = float(vp.get("width") or 1280)
        h = float(vp.get("height") or 720)
        x = random.uniform(w * 0.25, w * 0.75)
        y = random.uniform(h * 0.25, h * 0.75)
        await page.mouse.move(x, y)
        await page.mouse.move(
            x + random.uniform(-jitter, jitter),
            y + random.uniform(-jitter, jitter),
        )
    except Exception:
        pass


async def _human_click(target: Any, **kwargs: Any) -> None:
    """Click sau khi di chuyển chuột nhẹ tới phần tử."""
    if not UI_MOUSE_NUDGE_ENABLED:
        await target.click(**kwargs)
        return
    try:
        page = target.page
        await _micro_mouse_wiggle(page, near=target)
        await asyncio.sleep(random.uniform(0.02, 0.06))
        await target.click(**kwargs)
    except Exception:
        await target.click(**kwargs)


async def _submit_button_clickable(submit: Any) -> bool:
    try:
        if not await submit.is_visible():
            return False
        if not await submit.is_enabled():
            return False
        if await submit.get_attribute("disabled") is not None:
            return False
        aria = (await submit.get_attribute("aria-disabled") or "").strip().lower()
        if aria in ("true", "1"):
            return False
        return True
    except Exception:
        return False


async def _wait_submit_clickable(submit: Any, *, timeout_s: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_s
    last_log = 0.0
    while time.monotonic() < deadline:
        if await _submit_button_clickable(submit):
            return
        if time.monotonic() - last_log > 3.0:
            logger.info("Playwright UI: chờ nút mũi tên gửi sẵn sàng (enabled)...")
            last_log = time.monotonic()
        await asyncio.sleep(0.3)
    raise RuntimeError("submit_button_not_ready")


def _ui_step_retry_max() -> int:
    """Số lần retry pipeline UI — lỗi ở step N thì chạy lại từ step N-1."""
    return min(_ERROR_RETRY_MAX_CLICKS, max(1, int(UI_UPLOAD_RETRY_MAX or 1)))


def _count_generation_outputs(meta: dict[str, Any], generation_kind: str) -> int:
    if not isinstance(meta, dict):
        return 0
    kind = str(generation_kind or "").strip().lower()
    if kind == "video":
        return len([m for m in (meta.get("media_ids") or []) if str(m).strip()])
    urls = [u for u in (meta.get("image_urls") or []) if str(u).strip()]
    mids = [m for m in (meta.get("media_ids") or []) if str(m).strip()]
    return max(len(urls), len(mids))


async def _run_ui_step_pipeline(
    page: Any,
    steps: list[tuple[str, Any]],
    *,
    label: str = "ui_prep",
) -> dict[str, Any]:
    """
    Pipeline UI theo bước.
    Lỗi upload / prompt / generate → chạy lại từ upload → prompt → submit.
    Lỗi settings → chỉ chạy lại settings.
    """
    results: dict[str, Any] = {}
    max_retry = _ui_step_retry_max()
    restart_idx = _pipeline_restart_index(steps)
    restart_name = steps[restart_idx][0]
    completed = 0
    fail_streak = 0
    total = len(steps)

    while completed < total:
        step_name, step_fn = steps[completed]
        try:
            logger.info(
                "Playwright UI pipeline [%s] → %s (%s/%s)",
                label,
                step_name,
                completed + 1,
                total,
            )
            out = await step_fn()
            if out is not None:
                results[step_name] = out
            completed += 1
            fail_streak = 0
            continue
        except FlowHttp403Error:
            raise
        except UiFlowRestartFromUpload as exc:
            fail_streak += 1
            if fail_streak > max_retry:
                raise RuntimeError(
                    f"ui_restart_from_upload_exhausted step={step_name} "
                    f"retries={max_retry}: {exc}"
                ) from exc
            if not getattr(exc, "cleaned", False):
                await _prepare_restart_from_upload(page)
            logger.warning(
                "Playwright UI: lỗi %s — chạy lại từ '%s' "
                "(upload → chọn ảnh → prompt → submit) (%s/%s): %s",
                step_name,
                restart_name,
                fail_streak,
                max_retry,
                exc,
            )
            for key in ("upload", "prompt", "submit"):
                results.pop(key, None)
            completed = restart_idx
            continue
        except Exception as exc:
            fail_streak += 1
            if fail_streak > max_retry:
                raise RuntimeError(
                    f"ui_step_exhausted step={step_name} retries={max_retry}: {exc}"
                ) from exc

            if step_name in _RESTART_STEPS:
                await _prepare_restart_from_upload(page)
                logger.warning(
                    "Playwright UI: lỗi %s — chạy lại từ '%s' (%s/%s): %s",
                    step_name,
                    restart_name,
                    fail_streak,
                    max_retry,
                    exc,
                )
                for key in ("upload", "prompt", "submit"):
                    results.pop(key, None)
                completed = restart_idx
                continue

            # settings / bước khác: chỉ chạy lại step hiện tại
            logger.warning(
                "Playwright UI: lỗi step '%s' — thử lại (%s/%s): %s",
                step_name,
                fail_streak,
                max_retry,
                exc,
            )
            await _close_floating_overlays(page)
            await _ui_pause(0.25, f"retry step {step_name}")

    return results


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
        await _human_click(loc, timeout=10000)
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


async def _is_upload_menu_target(cand: Any) -> bool:
    """Nút/menu upload thật — không phải tab Tất cả / Thư viện."""
    if not await _is_clickable_menu_target(cand):
        return False
    try:
        role = (await cand.get_attribute("role") or "").lower()
        if role in ("tab", "tablist", "radio"):
            return False
        txt = (await cand.inner_text() or "").strip()
        if _SIDEBAR_UPLOAD.search(txt) and not _UPLOAD_MENU_PATTERNS.search(txt):
            return False
        return bool(_UPLOAD_MENU_PATTERNS.search(txt))
    except Exception:
        return False


async def _upload_item_locator(page: Any) -> Any | None:
    """Menuitem upload trong dialog/menu — không bấm tab thư viện."""
    menu = await _open_menu_scope_locator(page)
    if menu is None:
        return None

    try:
        item = menu.get_by_role("menuitem", name=_UPLOAD_MENU_PATTERNS)
        count = await item.count()
        for idx in range(count):
            cand = item.nth(idx)
            try:
                if await cand.is_visible() and await _is_upload_menu_target(cand):
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
                if await cand.is_visible() and await _is_upload_menu_target(cand):
                    return cand
            except Exception:
                continue
    except Exception:
        pass

    try:
        item = menu.locator(
            "button:not([role='tab']), [role='button']:not([role='tab'])"
        ).filter(has_text=_UPLOAD_MENU_PATTERNS)
        count = await item.count()
        for idx in range(count):
            cand = item.nth(idx)
            try:
                if await cand.is_visible() and await _is_upload_menu_target(cand):
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
        try:
            await _human_click(loc, timeout=8000)
        except Exception:
            await _human_click(loc, timeout=8000, force=True)
        await _ui_pause(0.25, "sau bấm Tải nội dung")
        logger.info("Playwright UI: đã bấm 'Tải nội dung nghe nhìn lên'")
        return True
    except Exception as exc:
        logger.warning("Playwright UI: click upload item failed: %s", exc)
        return False


async def _set_files_via_hidden_input(page: Any, image_path: Path, *, fast: bool = False) -> bool:
    try:
        loc = page.locator('input[type="file"]')
        count = await loc.count()
        for idx in range(count):
            inp = loc.nth(idx)
            try:
                await inp.set_input_files(str(image_path), timeout=5000)
                if fast:
                    await _ui_pause(0.15, "sau chọn file (fast)")
                else:
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
            await _human_click(loc.first,timeout=15000)
            await _ui_delay("sau bấm + Dự án mới")
            return True
    except Exception:
        pass
    try:
        loc = page.locator("a, button, [role='button'], [role='link']").filter(has_text=_NEW_PROJECT_LOOSE)
        if await loc.count() > 0:
            await _human_click(loc.first,timeout=15000)
            await _ui_delay("sau bấm + Dự án mới")
            return True
    except Exception:
        pass
    try:
        loc = page.get_by_text(_NEW_PROJECT_PATTERNS)
        if await loc.count() > 0:
            await _human_click(loc.first,timeout=15000)
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
            await _human_click(loc.first,timeout=15000)
            await _ui_delay(f"sau click {role}")
            return True
    except Exception:
        pass
    try:
        loc = page.locator("button, [role='button']").filter(has_text=pattern)
        if await loc.count() > 0:
            await _human_click(loc.first,timeout=15000)
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
                    await _human_click(btn,timeout=15000)
                    await _ui_delay("sau bấm Thêm vào câu lệnh")
                    logger.info("Playwright UI: đã bấm Thêm vào câu lệnh")
                    return True
                except Exception:
                    continue
        except Exception:
            continue

    return await _click_first_matching(page, _ADD_TO_PROMPT_PATTERNS)


_LIBRARY_UPLOADED_TAB = re.compile(r"tệp tải lên|uploaded files?", re.I)


def _ui_library_index_for_request_index(request_index: int, total: int) -> int:
    """API upload theo thứ tự request; thư viện UI mới nhất ở trên cùng."""
    if total <= 0:
        return 0
    return max(0, total - 1 - int(request_index))


def _slice_images_for_ui_upload(
    image_base64s: list[str],
    *,
    generation_kind: str,
    video_mode: str,
) -> list[str]:
    """Số ảnh upload qua UI theo loại request."""
    imgs = [x for x in (image_base64s or []) if x]
    if not imgs:
        return []
    kind = str(generation_kind or "").strip().lower()
    mode = str(video_mode or "").strip().lower()
    if kind == "video" and mode == "frame":
        return imgs[:2]
    return imgs[:3]


def _build_ui_file_upload_plan(
    image_base64s: list[str],
    *,
    generation_kind: str,
    video_mode: str,
) -> list[dict[str, Any]]:
    """
    Kế hoạch upload file qua UI (Tải nội dung nghe nhìn lên).
    Video Khung hình: startImage → Bắt đầu, endImage → Kết thúc (theo thứ tự).
    """
    imgs = _slice_images_for_ui_upload(
        image_base64s,
        generation_kind=generation_kind,
        video_mode=video_mode,
    )
    if not imgs:
        return []

    kind = str(generation_kind or "").strip().lower()
    mode = str(video_mode or "").strip().lower()

    if kind == "video" and mode == "frame":
        plan: list[dict[str, Any]] = []
        if len(imgs) >= 1:
            plan.append({"b64": imgs[0], "frame_slot": "start", "role": "start"})
        if len(imgs) >= 2:
            plan.append({"b64": imgs[1], "frame_slot": "end", "role": "end"})
        return plan

    return [
        {"b64": b64, "frame_slot": None, "role": f"ref_{idx}"}
        for idx, b64 in enumerate(imgs)
    ]


def _media_id_from_upload_data(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    try:
        info = flow_sdk._media_from_upload_payload(data)
        return str(info.get("media_id") or info.get("name") or "").strip()
    except Exception:
        pass
    for key in ("mediaId", "name", "id"):
        val = str(data.get(key) or "").strip()
        if val:
            return val
    media = data.get("media")
    if isinstance(media, dict):
        return str(media.get("name") or media.get("mediaId") or "").strip()
    return ""


async def attach_images_via_ui(
    page: Any,
    *,
    image_base64s: list[str],
    generation_kind: str,
    video_mode: str,
    video_duration_s: Any = 8,
    video_settings_configured: bool = False,
    variant_count: int = 1,
) -> list[dict[str, Any]]:
    """
    Upload ảnh qua UI Flow — bấm Tải nội dung nghe nhìn lên / Khung hình Bắt đầu·Kết thúc.
    Không gọi API upload trước.
    """
    plan = _build_ui_file_upload_plan(
        image_base64s,
        generation_kind=generation_kind,
        video_mode=video_mode,
    )
    if not plan:
        return []

    kind = str(generation_kind or "").strip().lower()
    mode = str(video_mode or "").strip().lower()
    vc = max(1, min(4, int(variant_count or 1)))
    if kind == "video" and mode == "frame":
        if not await _wait_for_video_frame_slots(page, timeout_s=10.0):
            logger.warning("Playwright UI: slot Khung hình chưa sẵn sàng — thử ép lại")
            if not await _force_video_frame_mode(
                page,
                variant_count=vc,
                video_duration_s=video_duration_s,
            ):
                raise RuntimeError("frame_slots_not_visible")
    elif kind == "video" and mode == "component":
        if not video_settings_configured and not await _video_component_mode_active(page):
            logger.warning("Playwright UI: Thành phần chưa active — ép lại trước upload")
            await _force_video_component_mode(
                page,
                variant_count=vc,
                video_duration_s=video_duration_s,
            )

    results: list[dict[str, Any]] = []
    temp_paths: list[Path] = []
    completed = 0
    try:
        while completed < len(plan):
            item = plan[completed]
            path = _base64_to_temp_file(str(item["b64"]), prefix=f"flow_ui_{completed}_")
            temp_paths.append(path)
            slot = item.get("frame_slot")
            role = item.get("role") or f"img_{completed}"
            try:
                logger.info(
                    "Playwright UI: upload ảnh %s/%s role=%s slot=%s",
                    completed + 1,
                    len(plan),
                    role,
                    slot or "-",
                )
                one = await upload_image_via_ui(
                    page,
                    image_path=path,
                    frame_slot=slot,
                )
                one["index"] = completed
                one["role"] = role
                mid = _media_id_from_upload_data(one.get("data"))
                if mid:
                    one["media_id"] = mid
                results.append(one)
                completed += 1
                await _ui_pause(0.35, f"sau upload ảnh {role} ({completed}/{len(plan)})")
            except UiFlowRestartFromUpload:
                raise
            except Exception as exc:
                await _reraise_as_upload_restart(
                    page,
                    exc,
                    context=f"upload ảnh {completed + 1}/{len(plan)}",
                )
        return results
    finally:
        for path in temp_paths:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass


def _build_image_attachment_plan(
    media_ids: list[str],
    *,
    generation_kind: str,
    video_mode: str,
) -> list[dict[str, Any]]:
    """
    Kế hoạch chọn ảnh trên UI sau khi đã upload qua API.
    Video Khung hình 2 ảnh: ảnh request[0]=Kết thúc, request[1]=Bắt đầu (đảo vì UI mới nhất trên).
    """
    ids = [str(m).strip() for m in media_ids if str(m).strip()]
    if not ids:
        return []

    total = len(ids)
    mode = str(video_mode or "").strip().lower()
    kind = str(generation_kind or "").strip().lower()

    if kind == "video" and mode == "frame":
        plan: list[dict[str, Any]] = []
        if total >= 2:
            plan.append(
                {
                    "media_id": ids[1],
                    "frame_slot": "start",
                    "request_index": 1,
                    "ui_list_index": _ui_library_index_for_request_index(1, total),
                    "role": "start",
                }
            )
            plan.append(
                {
                    "media_id": ids[0],
                    "frame_slot": "end",
                    "request_index": 0,
                    "ui_list_index": _ui_library_index_for_request_index(0, total),
                    "role": "end",
                }
            )
        elif total >= 1:
            plan.append(
                {
                    "media_id": ids[0],
                    "frame_slot": "start",
                    "request_index": 0,
                    "ui_list_index": _ui_library_index_for_request_index(0, total),
                    "role": "start",
                }
            )
        return plan

    if kind == "image":
        return [
            {
                "media_id": ids[0],
                "frame_slot": None,
                "request_index": 0,
                "ui_list_index": 0,
                "role": "reference",
            }
        ]

    max_attach = min(3, total)
    return [
        {
            "media_id": ids[req_idx],
            "frame_slot": None,
            "request_index": req_idx,
            "ui_list_index": _ui_library_index_for_request_index(req_idx, total),
            "role": "reference",
        }
        for req_idx in range(max_attach)
    ]


async def _collect_library_thumbnails(dialog: Any) -> list[Any]:
    """Thumbnail nhỏ trong sidebar modal — sắp trên→dưới (mới nhất trên cùng)."""
    items: list[tuple[float, float, Any]] = []
    try:
        imgs = dialog.locator("img")
        count = await imgs.count()
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
                if w >= 150 or h >= 200:
                    continue
                if w < 36 or h < 36:
                    continue
                loaded = await img.evaluate("el => el.complete && el.naturalWidth > 0")
                if not loaded:
                    continue
                target = await _frame_slot_click_target(img) or img
                items.append((float(box.get("y") or 0), float(box.get("x") or 0), target))
            except Exception:
                continue
    except Exception:
        return []
    items.sort(key=lambda t: (t[0], t[1]))
    return [t[2] for t in items]


async def _find_library_thumb_index_by_media_id(dialog: Any, media_id: str) -> int | None:
    """Tìm thumbnail theo media_id trong src (ổn định hơn index khi thư viện chưa sync đủ)."""
    mid = str(media_id or "").strip()
    if not mid:
        return None
    await _click_uploaded_files_tab(dialog)
    thumbs = await _collect_library_thumbnails(dialog)
    for idx, target in enumerate(thumbs):
        try:
            img = target.locator("xpath=ancestor-or-self::img[1]")
            if await img.count() == 0:
                continue
            src = (await img.first.get_attribute("src")) or ""
            if mid in src:
                return idx
        except Exception:
            continue
    return None


async def _resolve_library_thumb_index(
    dialog: Any,
    *,
    media_id: str,
    ui_list_index: int,
) -> int:
    """Chọn index thumbnail — ưu tiên media_id, fallback index, cuối cùng 0."""
    by_id = await _find_library_thumb_index_by_media_id(dialog, media_id)
    if by_id is not None:
        return by_id
    thumbs = await _collect_library_thumbnails(dialog)
    if 0 <= ui_list_index < len(thumbs):
        return ui_list_index
    if thumbs:
        logger.warning(
            "Playwright UI: index %s ngoài phạm vi (count=%s) — dùng 0 (media=%s…)",
            ui_list_index,
            len(thumbs),
            str(media_id)[:12],
        )
        return 0
    return ui_list_index


async def _click_uploaded_files_tab(dialog: Any) -> bool:
    """Tab 'Tệp tải lên' trong modal thư viện."""
    for role in ("tab", "button"):
        try:
            tabs = dialog.get_by_role(role, name=_LIBRARY_UPLOADED_TAB)
            count = await tabs.count()
            for idx in range(count):
                tab = tabs.nth(idx)
                try:
                    if not await tab.is_visible():
                        continue
                    await tab.scroll_into_view_if_needed(timeout=5000)
                    await _human_click(tab,timeout=8000)
                    await _ui_pause(0.35, "sau tab Tệp tải lên")
                    return True
                except Exception:
                    continue
        except Exception:
            continue
    return await _click_first_matching(dialog, _LIBRARY_UPLOADED_TAB)


async def _open_media_library_modal(page: Any, *, frame_slot: str | None = None) -> Any:
    """Mở modal thư viện — qua ô Khung hình hoặc menu [+]."""
    if await _settings_popover_open(page) is not None:
        await _dismiss_settings_panel(page)

    slot = str(frame_slot or "").strip().lower() or None
    if slot in ("start", "end"):
        if not await _click_video_frame_slot(page, slot):
            raise RuntimeError(f"frame_slot_not_found:{slot}")
        await _ui_pause(0.45, f"mở modal Khung hình {slot}")
    else:
        if not await _open_image_upload_menu(page):
            raise RuntimeError("upload_menu_not_open")
        await _ui_pause(0.3, "menu + mở")
        try:
            async with page.expect_file_chooser(timeout=1500):
                if not await _click_upload_menu_item(page):
                    raise RuntimeError("upload_button_not_found")
        except Exception:
            if not await _click_upload_menu_item(page):
                raise RuntimeError("upload_button_not_found")
        await _ui_pause(0.45, "modal thư viện")

    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline:
        dialog = await _media_library_dialog(page)
        if dialog is not None:
            return dialog
        await asyncio.sleep(0.35)
    raise RuntimeError("media_library_not_open")


async def _wait_uploaded_library_thumbnails(
    dialog: Any,
    *,
    min_count: int,
    timeout_s: float = 25.0,
) -> list[Any]:
    deadline = time.monotonic() + max(5.0, timeout_s)
    need = max(1, int(min_count))
    while time.monotonic() < deadline:
        await _click_uploaded_files_tab(dialog)
        thumbs = await _collect_library_thumbnails(dialog)
        if len(thumbs) >= need:
            return thumbs
        await asyncio.sleep(0.6)
    thumbs = await _collect_library_thumbnails(dialog)
    if len(thumbs) < need:
        logger.warning(
            "Playwright UI: thư viện chỉ có %s thumbnail (cần >= %s)",
            len(thumbs),
            need,
        )
    return thumbs


async def _select_library_thumbnail_at_index(dialog: Any, list_index: int) -> bool:
    thumbs = await _collect_library_thumbnails(dialog)
    if list_index < 0 or list_index >= len(thumbs):
        logger.warning(
            "Playwright UI: thumbnail index %s ngoài phạm vi (có %s)",
            list_index,
            len(thumbs),
        )
        return False
    target = thumbs[list_index]
    await target.scroll_into_view_if_needed(timeout=5000)
    await _human_click(target,timeout=10000)
    await _ui_pause(0.35, f"chọn thumbnail #{list_index}")
    return True


async def _wait_for_library_preview(
    dialog: Any,
    *,
    media_id: str = "",
    timeout_s: float = 15.0,
) -> bool:
    mid = str(media_id or "").strip()
    deadline = time.monotonic() + max(3.0, timeout_s)
    while time.monotonic() < deadline:
        preview = await _large_preview_image_in_dialog(dialog)
        if preview is None:
            await asyncio.sleep(0.4)
            continue
        if mid:
            try:
                src = (await preview.get_attribute("src")) or ""
                if mid in src:
                    return True
            except Exception:
                pass
        try:
            box = await preview.bounding_box()
            if box and float(box.get("width") or 0) >= 150:
                return True
        except Exception:
            return True
        await asyncio.sleep(0.4)
    return False


async def _error_delete_button_in_scope(scope: Any) -> Any | None:
    """Nút Xóa (trash) trên thẻ lỗi — không phải Retry/Undo."""
    try:
        loc = scope.locator("button, [role='button']")
        visible_btns: list[Any] = []
        count = await loc.count()
        for idx in range(count):
            cand = loc.nth(idx)
            try:
                if await cand.is_visible() and await cand.is_enabled():
                    visible_btns.append(cand)
            except Exception:
                continue
        if len(visible_btns) >= 3:
            return visible_btns[-1]
        for btn in visible_btns:
            try:
                txt = ((await btn.inner_text()) or "").strip()
                aria = ((await btn.get_attribute("aria-label")) or "").strip()
                title = ((await btn.get_attribute("title")) or "").strip()
                combined = f"{txt} {aria} {title}"
                if re.search(r"delete|remove|trash|xóa|clear", combined, re.I):
                    return btn
                icons = btn.locator("i, span[class*='symbol'], span, svg")
                ic = await icons.count()
                for i in range(ic):
                    icon_txt = ((await icons.nth(i).inner_text()) or "").strip()
                    if icon_txt and _ERROR_SKIP_ICON_PATTERNS.search(icon_txt):
                        return btn
            except Exception:
                continue
    except Exception:
        pass
    return None


async def _dismiss_upload_error_ui(page: Any) -> int:
    """Xóa thẻ Không thành công / đóng overlay — không bấm Retry."""
    cleared = 0
    if not await _has_generation_error_card(page):
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        return 0
    for card in await _generation_error_card_locators(page):
        btn = await _error_delete_button_in_scope(card)
        if btn is None:
            continue
        try:
            await _human_click(btn,timeout=2500)
            cleared += 1
        except Exception as exc:
            logger.debug("Playwright UI: không xóa được thẻ lỗi: %s", exc)
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    return cleared


async def _click_upload_error_retry_then_wait(
    page: Any,
    *,
    reason: str,
    retry_index: int,
    retry_max: int,
) -> bool:
    """API upload lỗi: dọn UI rồi chờ ngắn — không bấm Retry."""
    await _prepare_restart_from_upload(page)
    wait_s = min(5.0, max(0.5, float(UI_UPLOAD_ERROR_RETRY_WAIT_S)))
    logger.warning(
        "Playwright UI: upload lỗi %s — chờ %.1fs rồi thử lại (%s/%s)",
        reason,
        wait_s,
        retry_index,
        retry_max,
    )
    await asyncio.sleep(wait_s)
    return True


def _upload_retry_max_clicks() -> int:
    return min(_ERROR_RETRY_MAX_CLICKS, max(1, int(UI_UPLOAD_RETRY_MAX or 1)))


async def upload_images_via_api_with_ui_retry(
    page: Any,
    *,
    project_id: str,
    image_base64s: list[str],
) -> list[str]:
    """Upload ảnh qua API; lỗi thì xóa thẻ lỗi + chờ rồi upload lại (không bấm Retry)."""
    from flow2api.services.flow_client import get_flow_client

    imgs = [x for x in (image_base64s or []) if x]
    if not imgs:
        return []
    client = get_flow_client()
    max_clicks = _upload_retry_max_clicks()
    last_error: Exception | None = None
    for attempt in range(max_clicks + 1):
        try:
            ids = await flow_sdk.upload_images(
                client,
                project_id=project_id,
                image_base64s=imgs,
            )
            logger.info("Playwright UI: API upload OK — %s media_id", len(ids))
            return ids
        except Exception as exc:
            last_error = exc
            if attempt >= max_clicks:
                break
            logger.warning(
                "Playwright UI: API upload lỗi (%s/%s): %s",
                attempt + 1,
                max_clicks,
                exc,
            )
            if not await _click_upload_error_retry_then_wait(
                page,
                reason="api_upload",
                retry_index=attempt + 1,
                retry_max=max_clicks,
            ):
                raise
    raise RuntimeError(f"api_upload_retry_exhausted({max_clicks}): {last_error}")


async def select_uploaded_image_via_ui(
    page: Any,
    *,
    media_id: str,
    ui_list_index: int,
    frame_slot: str | None = None,
    total_uploaded: int = 1,
) -> dict[str, Any]:
    """Chọn ảnh đã upload API từ tab Tệp tải lên — không upload file lại."""
    dialog = await _open_media_library_modal(page, frame_slot=frame_slot)
    plan_count = max(1, int(total_uploaded))
    thumbs = await _wait_uploaded_library_thumbnails(
        dialog,
        min_count=min(plan_count, ui_list_index + 1),
    )
    pick_index = await _resolve_library_thumb_index(
        dialog,
        media_id=media_id,
        ui_list_index=ui_list_index,
    )
    if pick_index >= len(thumbs):
        raise RuntimeError(
            f"library_thumb_not_found index={pick_index} count={len(thumbs)} "
            f"wanted={ui_list_index} media={str(media_id)[:12]}"
        )
    if not await _select_library_thumbnail_at_index(dialog, pick_index):
        raise RuntimeError(f"library_thumb_click_failed:{pick_index}")
    if not await _wait_for_library_preview(dialog, media_id=media_id):
        logger.warning("Playwright UI: preview chưa rõ media_id=%s…", str(media_id)[:12])
    added = await _click_add_to_prompt(page)
    slot = str(frame_slot or "").strip().lower() or None
    if slot in ("start", "end"):
        await _wait_for_frame_slot_filled(page, slot, timeout_s=12.0)
    return {
        "status": 200,
        "phase": "api_upload_select",
        "media_id": media_id,
        "frame_slot": slot,
        "ui_list_index": pick_index,
        "added_to_prompt": added,
    }


async def select_uploaded_image_via_ui_with_retry(
    page: Any,
    *,
    media_id: str,
    ui_list_index: int,
    frame_slot: str | None = None,
    total_uploaded: int = 1,
) -> dict[str, Any]:
    """Chọn ảnh từ thư viện — lỗi thì restart pipeline từ upload."""
    try:
        return await select_uploaded_image_via_ui(
            page,
            media_id=media_id,
            ui_list_index=ui_list_index,
            frame_slot=frame_slot,
            total_uploaded=total_uploaded,
        )
    except UiFlowRestartFromUpload:
        raise
    except Exception as exc:
        await _reraise_as_upload_restart(
            page,
            exc,
            context=f"chọn ảnh media={str(media_id)[:12]}",
        )


async def attach_uploaded_images_via_ui(
    page: Any,
    *,
    media_ids: list[str],
    generation_kind: str,
    video_mode: str,
    video_duration_s: Any = 8,
    variant_count: int = 1,
) -> list[dict[str, Any]]:
    """Gắn ảnh đã upload API vào prompt qua UI (chọn thư viện, không upload file)."""
    plan = _build_image_attachment_plan(
        media_ids,
        generation_kind=generation_kind,
        video_mode=video_mode,
    )
    if not plan:
        return []

    kind = str(generation_kind or "").strip().lower()
    mode = str(video_mode or "").strip().lower()
    vc = max(1, min(4, int(variant_count or 1)))
    if kind == "video" and mode == "frame":
        if not await _wait_for_video_frame_slots(page, timeout_s=10.0):
            logger.warning("Playwright UI: slot Khung hình chưa sẵn sàng — thử ép lại")
            if not await _force_video_frame_mode(
                page,
                variant_count=vc,
                video_duration_s=video_duration_s,
            ):
                raise RuntimeError("frame_slots_not_visible")
    elif kind == "video" and mode == "component":
        if not await _video_component_mode_active(page):
            logger.warning("Playwright UI: Thành phần chưa active — ép lại trước chọn ảnh")
            await _force_video_component_mode(
                page,
                variant_count=vc,
                video_duration_s=video_duration_s,
            )

    total = len([m for m in media_ids if str(m).strip()])
    results: list[dict[str, Any]] = []
    for idx, item in enumerate(plan):
        try:
            one = await select_uploaded_image_via_ui_with_retry(
                page,
                media_id=str(item["media_id"]),
                ui_list_index=int(item["ui_list_index"]),
                frame_slot=item.get("frame_slot"),
                total_uploaded=total,
            )
        except UiFlowRestartFromUpload:
            raise
        except Exception as exc:
            await _reraise_as_upload_restart(
                page,
                exc,
                context=f"chọn ảnh {idx + 1}/{len(plan)}",
            )
        one["index"] = idx
        one["request_index"] = item.get("request_index")
        one["role"] = item.get("role")
        results.append(one)
        await _ui_delay(
            f"sau chọn ảnh {item.get('role') or idx + 1} ({idx + 1}/{len(plan)})"
        )
    return results


def _normalize_image_model_name(model: str) -> str:
    raw = str(model or "").strip()
    if not raw:
        return ""
    upper = raw.upper()
    if upper in _KNOWN_IMAGE_MODELS:
        return _KNOWN_IMAGE_MODELS[upper]
    return raw.replace("_", " ").strip()


def _normalize_video_model_name(model: str) -> str:
    raw = str(model or "").strip()
    if not raw:
        return _KNOWN_VIDEO_MODELS["lite_relaxed"]
    lower = raw.lower()
    if "low_priority" in lower or "lower priority" in lower:
        return _KNOWN_VIDEO_MODELS["lite_relaxed"]
    if "omni" in lower:
        return _KNOWN_VIDEO_MODELS["omni_flash"]
    if lower in _KNOWN_VIDEO_MODELS:
        return _KNOWN_VIDEO_MODELS[lower]
    upper = raw.upper()
    if upper in _KNOWN_VIDEO_MODELS:
        return _KNOWN_VIDEO_MODELS[upper]
    return raw.replace("_", " ").strip()


def _image_model_name_variants(model: str) -> list[str]:
    target = _normalize_image_model_name(model)
    if not target:
        return []
    variants: set[str] = {target}
    raw = str(model or "").strip()
    upper = raw.upper()
    if upper in _KNOWN_IMAGE_MODELS:
        variants.add(_KNOWN_IMAGE_MODELS[upper])
    for label in _KNOWN_IMAGE_MODELS.values():
        if label == target or target in label or label in target:
            variants.add(label)
    return [v for v in variants if v]


def _video_model_name_variants(model: str) -> list[str]:
    target = _normalize_video_model_name(model)
    if not target:
        return []
    variants = {
        target,
        target.replace(" - Lite [Lower Priority]", " Lite [Lower Priority]"),
        target.replace(" Lite [Lower Priority]", " - Lite [Lower Priority]"),
    }
    return [v for v in variants if v]


def _normalize_aspect_ratio(value: str) -> str:
    raw = str(value or "").strip()
    return raw if raw in _ASPECT_RATIOS else "16:9"


def _normalize_generation_kind(request_type: str) -> str:
    req = str(request_type or "").strip().lower()
    return "video" if "video" in req else "image"


def _panel_has_aspect_toolbar_text(text: str) -> bool:
    txt = str(text or "")
    return all(r in txt for r in _ASPECT_RATIOS)


def _is_video_settings_panel_text(text: str) -> bool:
    txt = str(text or "")
    if "Khung hình" in txt and "Thành phần" in txt:
        return True
    if "Hình ảnh" in txt and "Video" in txt and (
        "Veo" in txt or "Omni" in txt or _VIDEO_CHIP_DURATION.search(txt)
    ):
        return True
    if ("9:16" in txt and "16:9" in txt and "4:3" not in txt) and (
        "Veo" in txt or "Omni" in txt or "Khung hình" in txt
    ):
        return True
    return False


def _is_settings_panel_text(text: str) -> bool:
    """Panel popover cài đặt (Hình ảnh/Video + tỉ lệ + model) — không phải thư viện upload."""
    if _is_media_library_panel_text(text):
        return False
    txt = str(text or "")
    if "Hình ảnh" in txt and "Video" in txt:
        if any(v in txt for v in ("1x", "x2", "x3", "x4")):
            return True
        if "Nano Banana" in txt or "Veo" in txt or "Omni" in txt:
            return True
        if sum(1 for r in _ASPECT_RATIOS if r in txt) >= 3:
            return True
    if _panel_has_aspect_toolbar_text(txt):
        return True
    if _is_video_settings_panel_text(txt):
        return True
    if "Quá trình tạo" in txt and (
        "Nano Banana" in txt or "Veo" in txt or "Omni" in txt or "tín dụng" in txt
    ):
        return True
    return False


async def _score_settings_panel_candidate(cand: Any) -> float:
    try:
        if not await cand.is_visible():
            return -1.0
        txt = (await cand.inner_text() or "").strip()
        if not _is_settings_panel_text(txt):
            return -1.0
        box = await cand.bounding_box()
        if not box:
            return -1.0
        w = float(box.get("width") or 0)
        h = float(box.get("height") or 0)
        if w < 160 or h < 80:
            return -1.0
        score = 2000.0 - (w * h)
        if _panel_has_aspect_toolbar_text(txt):
            score += 800.0
        if any(v in txt for v in ("1x", "x2", "x3", "x4")):
            score += 400.0
        if "Khung hình" in txt and "Thành phần" in txt:
            score += 600.0
        if "Nano Banana" in txt or "Veo" in txt or "Omni" in txt:
            score += 200.0
        return score
    except Exception:
        return -1.0


async def _settings_panel_from_kind_row(page: Any) -> Any | None:
    """Tìm panel qua hàng toggle Hình ảnh | Video (ổn định nhất trên UI mới)."""
    row = await _panel_kind_row_locator(page)
    if row is None:
        return None
    try:
        if not await row.is_visible():
            return None
    except Exception:
        return None

    best = None
    best_score = -1.0
    for xpath in (
        "xpath=ancestor::*[@role='dialog'][1]",
        "xpath=ancestor::*[@data-radix-popper-content-wrapper][1]",
        "xpath=ancestor::*[@data-state='open'][1]",
        "xpath=ancestor::*[.//*[normalize-space(text())='1x' or normalize-space(text())='x2']][position()<=12]",
        "xpath=ancestor::*[position()<=8]",
    ):
        try:
            loc = row.locator(xpath)
            count = await loc.count()
            for idx in range(count):
                cand = loc.nth(idx)
                sc = await _score_settings_panel_candidate(cand)
                if sc > best_score:
                    best_score = sc
                    best = cand
        except Exception:
            continue
    return best


async def _wait_settings_panel_open(page: Any, *, timeout_s: float = 5.0) -> Any | None:
    deadline = time.monotonic() + max(0.5, float(timeout_s))
    while time.monotonic() < deadline:
        panel = await _settings_popover_open(page)
        if panel is not None:
            return panel
        await asyncio.sleep(0.25)
    return None


async def _settings_popover_open(page: Any) -> Any | None:
    """Popover cài đặt model + tỉ lệ (UI gộp Image+Video)."""
    row = await _panel_kind_row_locator(page)
    if row is not None:
        try:
            if not await row.is_visible():
                row = None
        except Exception:
            row = None
    if row is not None:
        panel = await _settings_panel_from_kind_row(page)
        if panel is not None:
            return panel

    selectors = (
        '[role="dialog"][data-state="open"]',
        '[role="dialog"]',
        '[data-radix-popper-content-wrapper]',
        '[role="menu"][data-state="open"]',
        '[role="listbox"][data-state="open"]',
    )
    best = None
    best_area = float("inf")
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
                    if not _is_settings_panel_text(txt):
                        continue
                    box = await cand.bounding_box()
                    area = float((box or {}).get("width") or 0) * float(
                        (box or {}).get("height") or 0
                    )
                    if 0 < area < best_area:
                        best_area = area
                        best = cand
                except Exception:
                    continue
        except Exception:
            continue
    if best is not None:
        return best

    try:
        loc = page.locator("div, section, form")
        count = min(await loc.count(), 120)
        for idx in range(count - 1, -1, -1):
            cand = loc.nth(idx)
            try:
                if not await cand.is_visible():
                    continue
                txt = (await cand.inner_text() or "").strip()
                if not _is_settings_panel_text(txt):
                    continue
                box = await cand.bounding_box()
                if not box:
                    continue
                w = float(box.get("width") or 0)
                h = float(box.get("height") or 0)
                if w < 180 or h < 120:
                    continue
                area = w * h
                if area < best_area:
                    best_area = area
                    best = cand
            except Exception:
                continue
    except Exception:
        pass
    return best


async def _refresh_settings_panel(page: Any, panel: Any | None = None) -> Any | None:
    """Lấy panel settings đang mở — không bấm chip (tránh toggle đóng panel)."""
    if panel is not None:
        try:
            if await panel.is_visible():
                txt = (await panel.inner_text() or "").strip()
                if _is_settings_panel_text(txt):
                    return panel
        except Exception:
            pass

    if not await _settings_panel_visibly_open(page):
        return None

    return await _settings_popover_open(page)


async def _keep_settings_panel_scope(
    page: Any,
    panel: Any | None,
    *,
    kind: str = "auto",
) -> Any | None:
    """Giữ panel mở giữa các bước — refresh scope nếu DOM đổi sau khi chọn tab/toggle."""
    panel = await _refresh_settings_panel(page, panel)
    if panel is None:
        return None
    return await _resolve_settings_panel_scope(page, panel, kind=kind)


def _normalize_label_text(value: str) -> str:
    return re.sub(r"\s*-\s*", " ", re.sub(r"\s+", " ", str(value or "").strip().lower()))


def _label_fuzzy_matches(text: str, target: str) -> bool:
    want = _normalize_label_text(target)
    if not want:
        return False
    raw = str(text or "").strip()
    if _normalize_label_text(raw) == want:
        return True
    for line in raw.splitlines():
        line_n = _normalize_label_text(line)
        if line_n == want or want in line_n:
            return True
    return want in _normalize_label_text(raw)


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


def _settings_chip_is_video_label(label: str) -> bool:
    raw = str(label or "").strip()
    if not raw:
        return False
    for line in raw.splitlines():
        ln = line.strip()
        if ln and _SETTINGS_CHIP_LEADING_VIDEO.match(ln):
            return True
    return bool(_SETTINGS_CHIP_LEADING_VIDEO.search(raw))


def _settings_chip_is_image_label(label: str) -> bool:
    raw = str(label or "").strip()
    if not raw:
        return False
    if _settings_chip_is_video_label(raw):
        return False
    return bool(_SETTINGS_CHIP_LEADING_IMAGE.search(raw))


def _settings_chip_label_valid(label: str, *, prefer: str = "auto") -> bool:
    """Text chip — dòng đầu bắt đầu Nano Banana hoặc Video (phải kèm mũi tên kế bên)."""
    raw = str(label or "").strip()
    if not raw or _UPLOAD_TEXT.search(raw) or _PROMPT_ADD_ICON.search(raw):
        return False
    if "tác nhân" in raw.lower():
        return False
    is_video = _settings_chip_is_video_label(raw)
    is_image = _settings_chip_is_image_label(raw)
    if not is_image and not is_video:
        return False
    kind = str(prefer or "auto").strip().lower()
    if kind == "video" and not is_video:
        return False
    if kind == "image" and not is_image:
        return False
    return True


_SUBMIT_ARROW_ICON_RE = re.compile(
    r"arrow_forward|send|north_east|arrow_upward|chevron_right",
    re.I,
)


async def _locator_has_submit_arrow_icon(loc: Any) -> bool:
    """Nút tròn mũi tên (→) kế chip settings."""
    try:
        icons = loc.locator(
            "i.google-symbols, i, span[class*='symbol'], span[class*='material']"
        )
        count = await icons.count()
        for idx in range(count):
            txt = (await icons.nth(idx).inner_text() or "").strip()
            if _SUBMIT_ARROW_ICON_RE.search(txt):
                return True
    except Exception:
        pass
    try:
        label = (await loc.inner_text() or "").strip()
        if label in ("→", "›", "▶") or _SUBMIT_ARROW_ICON_RE.search(label):
            return True
    except Exception:
        pass
    for attr in ("aria-label", "title"):
        try:
            val = (await loc.get_attribute(attr) or "").strip()
            if val and re.search(r"arrow|send|submit|gửi|tạo|generate", val, re.I):
                return True
        except Exception:
            continue
    return False


def _chip_arrow_boxes_adjacent(chip_box: dict[str, Any], arrow_box: dict[str, Any]) -> bool:
    cr = float(chip_box.get("x") or 0) + float(chip_box.get("width") or 0)
    cy = float(chip_box.get("y") or 0)
    ch = float(chip_box.get("height") or 0)
    ax = float(arrow_box.get("x") or 0)
    ay = float(arrow_box.get("y") or 0)
    if abs(ay - cy) > max(64, ch * 0.95):
        return False
    gap = ax - cr
    return -24 <= gap <= 120


async def _chip_adjacent_submit_arrow(chip: Any) -> Any | None:
    """Mũi tên (→) ngay bên phải chip — bắt buộc để nhận diện đúng control."""
    bases: list[Any] = [chip]
    try:
        parent = chip.locator("xpath=parent::*")
        if await parent.count() > 0:
            bases.append(parent.first)
    except Exception:
        pass

    queries = (
        "xpath=following-sibling::button[1]",
        "xpath=following-sibling::*[@role='button'][1]",
        "xpath=following-sibling::*[1]",
    )
    for base in bases:
        try:
            cbox = await base.bounding_box()
        except Exception:
            cbox = None
        if not cbox:
            continue
        for xpath in queries:
            try:
                loc = base.locator(xpath)
                if await loc.count() == 0:
                    continue
                arrow = loc.first
                if not await arrow.is_visible():
                    continue
                if not await _locator_has_submit_arrow_icon(arrow):
                    continue
                abox = await arrow.bounding_box()
                if abox and _chip_arrow_boxes_adjacent(cbox, abox):
                    return arrow
            except Exception:
                continue
    return None


async def _settings_chip_click_target(node: Any) -> Any:
    """Phần tử nên click để mở panel — button/role=button gần nhất."""
    try:
        btn = node.locator(
            "xpath=ancestor-or-self::button[1] | ancestor-or-self::*[@role='button'][1]"
        )
        if await btn.count() > 0:
            return btn.first
    except Exception:
        pass
    return node


async def _submit_arrow_adjacent_settings_chip(arrow: Any, *, prefer: str = "auto") -> Any | None:
    """Từ mũi tên (→) tìm chip text Nano Banana/Video ngay bên trái."""
    try:
        sbox = await arrow.bounding_box()
    except Exception:
        sbox = None
    if sbox is None:
        return None
    sy = float(sbox.get("y") or 0)
    sx = float(sbox.get("x") or 0)

    candidates: list[Any] = []
    queries = (
        "xpath=preceding-sibling::button[1]",
        "xpath=preceding-sibling::*[@role='button'][1]",
        "xpath=preceding-sibling::*[1]",
        "xpath=preceding-sibling::div[1]",
        "xpath=parent::*/*[last()-1]",
        "xpath=parent::*/*[1][not(self::button[@aria-haspopup='dialog'])]",
    )
    for xpath in queries:
        try:
            loc = arrow.locator(xpath)
            count = await loc.count()
            for idx in range(count):
                cand = loc.nth(idx)
                if await cand.evaluate("(a,b)=>a===b", arrow):
                    continue
                candidates.append(cand)
        except Exception:
            continue

    best = None
    best_dx = float("inf")
    seen: set[int] = set()
    for cand in candidates:
        try:
            oid = await cand.evaluate("el => el.outerHTML.length ^ (el.innerText||'').length")
        except Exception:
            oid = id(cand)
        if oid in seen:
            continue
        seen.add(oid)
        try:
            if not await cand.is_visible():
                continue
            label = (await cand.inner_text() or "").strip()
            if not _settings_chip_label_valid(label, prefer=prefer):
                continue
            chip = await _settings_chip_click_target(cand)
            if not await chip.is_visible():
                continue
            cbox = await chip.bounding_box()
            if not cbox or not _chip_arrow_boxes_adjacent(cbox, sbox):
                continue
            cy = float(cbox.get("y") or 0)
            cx = float(cbox.get("x") or 0)
            if abs(cy - sy) > 64 or cx >= sx + 8:
                continue
            dx = sx - cx
            if dx < best_dx:
                best_dx = dx
                best = chip
        except Exception:
            continue
    return best


async def _all_submit_arrow_buttons(page: Any) -> list[Any]:
    composer = await _prompt_composer_locator(page)
    near_y = await _prompt_bar_y_anchor(page)
    scopes = [composer, page] if composer is not None else [page]
    found: list[Any] = []
    seen: set[int] = set()
    for scope in scopes:
        if scope is None:
            continue
        try:
            loc = scope.locator("button, [role='button']")
            count = await loc.count()
            for idx in range(count - 1, -1, -1):
                cand = loc.nth(idx)
                try:
                    if not await cand.is_visible():
                        continue
                    box = await cand.bounding_box()
                    if near_y is not None and box and abs(float(box.get("y") or 0) - near_y) > 320:
                        continue
                    if not await _locator_has_submit_arrow_icon(cand):
                        continue
                    oid = await cand.evaluate("el => el.outerHTML.length")
                    if oid in seen:
                        continue
                    seen.add(oid)
                    found.append(cand)
                except Exception:
                    continue
        except Exception:
            continue
    return found


async def _settings_chip_from_geometry(page: Any, *, prefer: str = "auto") -> Any | None:
    """Ghép chip text + mũi tên theo vị trí trong prompt bar (fallback mạnh)."""
    composer = await _prompt_composer_locator(page)
    scope = composer if composer is not None else page
    near_y = await _prompt_bar_y_anchor(page)
    arrows = await _all_submit_arrow_buttons(page)
    if not arrows:
        return None

    text_patterns: list[tuple[str, re.Pattern[str]]] = []
    kind = str(prefer or "auto").strip().lower()
    if kind in ("auto", "image"):
        text_patterns.append(("image", re.compile(r"Nano\s+Banana", re.I)))
    if kind in ("auto", "video"):
        text_patterns.append(("video", re.compile(r"^Video\b", re.I | re.M)))

    best: Any | None = None
    best_score = -1.0
    for arrow in arrows:
        try:
            sbox = await arrow.bounding_box()
            if not sbox:
                continue
            sx = float(sbox.get("x") or 0)
            sy = float(sbox.get("y") or 0)
        except Exception:
            continue

        for mode, pat in text_patterns:
            try:
                hits = scope.get_by_text(pat)
                hcount = await hits.count()
            except Exception:
                continue
            for hi in range(hcount):
                node = hits.nth(hi)
                try:
                    if not await node.is_visible():
                        continue
                    chip = await _settings_chip_click_target(node)
                    if not await chip.is_visible():
                        continue
                    label = (await chip.inner_text() or "").strip()
                    if not _settings_chip_label_valid(label, prefer=mode):
                        continue
                    cbox = await chip.bounding_box()
                    if not cbox or not _chip_arrow_boxes_adjacent(cbox, sbox):
                        continue
                    cy = float(cbox.get("y") or 0)
                    cx = float(cbox.get("x") or 0)
                    if cx >= sx or abs(cy - sy) > 64:
                        continue
                    if near_y is not None and abs(cy - near_y) > 320:
                        continue
                    dx = sx - cx
                    score = 1000.0 - dx
                    if mode == kind:
                        score += 50.0
                    if score > best_score:
                        best_score = score
                        best = chip
                except Exception:
                    continue
    return best


async def _settings_chip_locator(page: Any, *, prefer: str = "auto") -> Any | None:
    """
    Chip mở panel settings = text bắt đầu Nano Banana / Video
    VÀ có nút mũi tên (→) ngay bên phải (như ảnh UI).
    """
    prefer_modes = (prefer,) if str(prefer or "auto").strip().lower() != "auto" else (
        "auto",
        "image",
        "video",
    )

    # Cách 1: từ mũi tên (→) → chip bên trái.
    arrows = await _all_submit_arrow_buttons(page)
    for mode in prefer_modes:
        best = None
        best_dx = float("inf")
        for arrow in arrows:
            chip = await _submit_arrow_adjacent_settings_chip(arrow, prefer=mode)
            if chip is None:
                continue
            try:
                cbox = await chip.bounding_box()
                sbox = await arrow.bounding_box()
                if not cbox or not sbox:
                    continue
                dx = float(sbox.get("x") or 0) - float(cbox.get("x") or 0)
                if dx < best_dx:
                    best_dx = dx
                    best = chip
            except Exception:
                best = chip
        if best is not None:
            return best

    # Cách 2: từ text → phải có mũi tên kế bên phải.
    composer = await _prompt_composer_locator(page)
    scopes = [composer, page] if composer is not None else [page]
    for mode in prefer_modes:
        for scope in scopes:
            if scope is None:
                continue
            try:
                loc = scope.locator("button, [role='button'], div, span")
                count = min(await loc.count(), 220)
                for idx in range(count):
                    node = loc.nth(idx)
                    try:
                        if not await node.is_visible():
                            continue
                        label = (await node.inner_text() or "").strip()
                        if not _settings_chip_label_valid(label, prefer=mode):
                            continue
                        chip = await _settings_chip_click_target(node)
                        if not await chip.is_visible():
                            continue
                        final_label = (await chip.inner_text() or "").strip()
                        if not _settings_chip_label_valid(final_label, prefer=mode):
                            continue
                        if await _chip_adjacent_submit_arrow(chip) is not None:
                            return chip
                    except Exception:
                        continue
            except Exception:
                continue

    return await _settings_chip_from_geometry(page, prefer=prefer)


async def _settings_chip_from_submit_anchor(page: Any, *, prefer: str = "auto") -> Any | None:
    return await _settings_chip_locator(page, prefer=prefer)


async def _settings_chip_by_summary_text(page: Any, *, prefer: str = "auto") -> Any | None:
    return await _settings_chip_locator(page, prefer=prefer)


async def _model_chip_button_locator(page: Any, *, prefer: str = "auto") -> Any | None:
    """Chip settings: Nano Banana / Video + mũi tên (→) kế bên."""
    kind = str(prefer or "auto").strip().lower()
    chip = await _settings_chip_locator(page, prefer=kind)
    if chip is not None:
        return chip
    return None


async def _open_settings_panel(page: Any, *, generation_kind: str = "image") -> Any | None:
    """Mở panel settings — nếu đã mở thì dùng luôn, không bấm chip (tránh đóng panel)."""
    kind = str(generation_kind or "").strip().lower()
    if kind == "auto":
        prefer_modes = ("auto", "video", "image")
    elif kind == "video":
        prefer_modes = ("video", "auto", "image")
    else:
        prefer_modes = ("image", "auto", "video")

    for attempt in range(3):
        existing = await _refresh_settings_panel(page)
        if existing is not None:
            logger.info("Playwright UI: panel settings đã mở — tiếp tục chọn trong panel")
            return existing

        prompt = await _prompt_input_locator(page)
        if prompt is not None:
            try:
                await prompt.scroll_into_view_if_needed(timeout=5000)
                await asyncio.sleep(0.25)
            except Exception:
                pass

        chip = None
        used_prefer = ""
        for prefer in prefer_modes:
            chip = await _model_chip_button_locator(page, prefer=prefer)
            if chip is not None:
                used_prefer = prefer
                break
        if chip is None:
            arrows_n = len(await _all_submit_arrow_buttons(page))
            composer = await _prompt_composer_locator(page)
            nb_n = 0
            try:
                scope = composer if composer is not None else page
                nb_n = await scope.get_by_text(re.compile(r"Nano\s+Banana", re.I)).count()
            except Exception:
                pass
            logger.warning(
                "Playwright UI: không tìm thấy chip settings trên prompt bar (lần %s) "
                "composer=%s arrows=%s nano_banana_hits=%s",
                attempt + 1,
                composer is not None,
                arrows_n,
                nb_n,
            )
            await asyncio.sleep(0.6)
            continue
        try:
            try:
                arrow = await _chip_adjacent_submit_arrow(chip)
                logger.info(
                    "Playwright UI: settings chip candidate #%s (%s) text=%r arrow=%s",
                    attempt + 1,
                    used_prefer,
                    ((await chip.inner_text()) or "").strip()[:120],
                    arrow is not None,
                )
            except Exception:
                pass
            await chip.scroll_into_view_if_needed(timeout=5000)
            if await _settings_panel_visibly_open(page):
                panel = await _refresh_settings_panel(page)
                if panel is not None:
                    logger.info("Playwright UI: panel settings đã mở (trước khi bấm chip)")
                    return panel
            try:
                expanded = (await chip.get_attribute("aria-expanded") or "").lower()
            except Exception:
                expanded = ""
            if expanded == "true":
                panel = await _wait_settings_panel_open(page, timeout_s=2.0)
                if panel is not None:
                    return panel
                panel = await _refresh_settings_panel(page)
                if panel is not None:
                    return panel
                logger.info(
                    "Playwright UI: chip aria-expanded=true nhưng chưa thấy panel — không bấm chip (tránh đóng)"
                )
                await asyncio.sleep(0.4)
                continue
            await _human_click(chip,timeout=10000)
            panel = await _wait_settings_panel_open(page, timeout_s=5.0)
            if panel is not None:
                await _ui_delay("sau mở panel settings")
                return panel
            # Toggle — thử bấm lại nếu panel chưa nhận diện được
            await _human_click(chip,timeout=10000)
            panel = await _wait_settings_panel_open(page, timeout_s=3.0)
            if panel is not None:
                await _ui_delay("sau mở panel settings (retry toggle)")
                return panel
            await asyncio.sleep(0.5)
        except Exception as exc:
            logger.warning("Playwright UI: mở panel settings failed (lần %s): %s", attempt + 1, exc)
    logger.warning("Playwright UI: không mở được panel settings")
    return None


async def _open_image_settings_panel(page: Any) -> Any | None:
    """Alias giữ tương thích — mặc định image."""
    return await _open_settings_panel(page, generation_kind="image")


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
                        await _human_click(btn.first,timeout=10000)
                    else:
                        await _human_click(cand,timeout=10000)
                else:
                    await _human_click(cand,timeout=10000)
                return True
            except Exception:
                continue
    except Exception:
        pass
    return False


async def _click_fuzzy_label(scope: Any, label: str) -> bool:
    """Click label gần đúng (bỏ qua badge Quá tải, khác dấu '-')."""
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
                if not _label_fuzzy_matches(txt, target):
                    continue
                tag = (await cand.evaluate("el => el.tagName") or "").upper()
                if tag in ("DIV", "SPAN"):
                    btn = cand.locator(
                        "xpath=ancestor-or-self::button[1] | ancestor-or-self::*[@role='button'][1] | ancestor-or-self::*[@role='radio'][1] | ancestor-or-self::*[@role='menuitem'][1] | ancestor-or-self::*[@role='option'][1]"
                    )
                    if await btn.count() > 0:
                        await _human_click(btn.first,timeout=10000)
                    else:
                        await _human_click(cand,timeout=10000)
                else:
                    await _human_click(cand,timeout=10000)
                return True
            except Exception:
                continue
    except Exception:
        pass
    return False


async def _ensure_media_settings_ready(panel: Any) -> bool:
    """
    UI gộp Image+Video — đảm bảo tab Hình ảnh + hàng 5 tỉ lệ trong panel.
    """
    return await _ensure_image_media_settings_ready(panel)


async def _ensure_image_media_settings_ready(panel: Any) -> bool:
    """Panel settings đang ở nhánh Hình ảnh (16:9, 4:3, 1:1, …)."""
    try:
        txt = (await panel.inner_text() or "").strip()
        if _panel_has_aspect_toolbar_text(txt):
            return True
    except Exception:
        pass

    if await _aspect_ratio_toolbar_locator(panel) is not None:
        try:
            txt = (await panel.inner_text() or "").strip()
            if _panel_has_aspect_toolbar_text(txt):
                return True
        except Exception:
            pass

    if await _select_generation_kind_in_panel(panel, "image"):
        await _ui_pause(0.45, "ép tab Hình ảnh trong panel")

    try:
        txt = (await panel.inner_text() or "").strip()
        if _panel_has_aspect_toolbar_text(txt):
            return True
    except Exception:
        pass

    if await _aspect_ratio_toolbar_locator(panel) is not None:
        return True

    logger.warning("Playwright UI: chưa thấy toolbar aspect ratio (Hình ảnh) trong panel")
    return False


async def _ensure_video_panel_ready(panel: Any) -> bool:
    """Panel Video: đã chọn tab Video + có Khung hình/Veo."""
    if await _panel_generation_kind_active(panel, "video"):
        return True
    if await _select_generation_kind_in_panel(panel, "video"):
        await _ui_pause(0.45, "ép tab Video trong panel")
    if await _panel_generation_kind_active(panel, "video"):
        return True
    try:
        txt = (await panel.inner_text() or "").strip()
    except Exception:
        txt = ""
    if "Khung hình" in txt and "Thành phần" in txt:
        return True
    if await _video_aspect_ratio_row_locator(panel) is not None:
        return True
    if "Veo" in txt or "Omni" in txt:
        return True
    logger.warning("Playwright UI: chưa thấy controls video trong panel settings")
    return False


async def _video_aspect_ratio_row_locator(panel: Any) -> Any | None:
    """Hàng ratio video (chỉ 9:16 + 16:9, không có 4:3)."""
    return await _smallest_visible_locator(
        panel,
        "xpath=.//*[.//*[normalize-space(text())='9:16']"
        " and .//*[normalize-space(text())='16:9']"
        " and not(.//*[normalize-space(text())='4:3'])]",
    )


async def _element_bg_luminance(el: Any) -> float:
    """Độ sáng nền nút — nút đang chọn thường sáng hơn."""
    try:
        return float(
            await el.evaluate(
                """(node) => {
                function luminance(el) {
                    if (!el) return 0;
                    const style = window.getComputedStyle(el);
                    const bg = style.backgroundColor || '';
                    const m = bg.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)(?:,\\s*([\\d.]+))?\\)/);
                    if (!m) return 0;
                    const a = m[4] !== undefined ? parseFloat(m[4]) : 1;
                    if (a < 0.2) return 0;
                    return 0.299 * +m[1] + 0.587 * +m[2] + 0.114 * +m[3];
                }
                let n = node;
                for (let i = 0; i < 5; i++) {
                    const lum = luminance(n);
                    if (lum > 0) return lum;
                    n = n.parentElement;
                }
                return 0;
            }"""
            )
        )
    except Exception:
        return 0.0


async def _smallest_visible_locator(panel: Any, xpath: str) -> Any | None:
    """Phần tử xpath nhỏ nhất (visible) — thường là hàng toggle thật."""
    try:
        loc = panel.locator(xpath)
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
        return best
    except Exception:
        return None


def _toggle_button_label(text: str, allowed_labels: tuple[str, ...]) -> str | None:
    """Một nút chỉ chứa đúng 1 label trong allowed_labels."""
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    if allowed_labels != _VARIANT_COUNT_UI and any(_is_variant_count_label(ln) for ln in lines):
        return None
    found: list[str] = []
    for lbl in allowed_labels:
        for ln in lines:
            if ln == lbl or ln.lower() == lbl.lower():
                found.append(lbl)
                break
    if len(found) == 1:
        return found[0]
    compact = re.sub(r"\s+", "", str(text or "")).lower()
    for lbl in allowed_labels:
        if compact == re.sub(r"\s+", "", lbl).lower():
            return lbl
    return None


async def _resolve_toggle_click_target(node: Any) -> Any:
    """Span/text → nút button thực sự."""
    try:
        tag = (await node.evaluate("el => el.tagName") or "").upper()
        role = await node.get_attribute("role")
        if tag == "BUTTON" or role in ("button", "radio", "tab"):
            return node
        btn = node.locator(
            "xpath=ancestor-or-self::button[1]"
            " | ancestor-or-self::*[@role='button'][1]"
            " | ancestor-or-self::*[@role='radio'][1]"
            " | ancestor-or-self::*[@role='tab'][1]"
        )
        if await btn.count() > 0:
            return btn.first
    except Exception:
        pass
    return node


async def _collect_toggle_buttons(
    row: Any,
    allowed_labels: tuple[str, ...],
) -> list[tuple[str, Any, float]]:
    """[(label, button, x)] sắp trái→phải trong hàng toggle."""
    entries: list[tuple[str, Any, float]] = []
    seen: set[str] = set()
    try:
        loc = row.locator("button, [role='button'], [role='radio'], [role='tab']")
        count = await loc.count()
        for idx in range(count):
            cand = loc.nth(idx)
            try:
                if not await cand.is_visible():
                    continue
                txt = (await cand.inner_text() or "").strip()
                label = _toggle_button_label(txt, allowed_labels)
                if not label:
                    continue
                click_el = await _resolve_toggle_click_target(cand)
                box = await click_el.bounding_box()
                if not box:
                    continue
                key = f"{label}:{round(float(box.get('x') or 0))}"
                if key in seen:
                    continue
                seen.add(key)
                entries.append((label, click_el, float(box.get("x") or 0)))
            except Exception:
                continue
    except Exception:
        pass

    if not entries:
        for label in allowed_labels:
            try:
                hits = row.get_by_text(label, exact=True)
                n = await hits.count()
                for i in range(n):
                    node = hits.nth(i)
                    try:
                        if not await node.is_visible():
                            continue
                        click_el = await _resolve_toggle_click_target(node)
                        txt = (await click_el.inner_text() or "").strip()
                        if _toggle_button_label(txt, allowed_labels) != label:
                            continue
                        box = await click_el.bounding_box()
                        if not box:
                            continue
                        key = f"{label}:{round(float(box.get('x') or 0))}"
                        if key in seen:
                            continue
                        seen.add(key)
                        entries.append((label, click_el, float(box.get("x") or 0)))
                    except Exception:
                        continue
            except Exception:
                continue

    entries.sort(key=lambda item: item[2])
    return entries


async def _toggle_selected_in_row(
    row: Any,
    target: str,
    allowed_labels: tuple[str, ...],
) -> bool:
    """Nút sáng hơn = đang chọn (so sánh trong hàng toggle)."""
    want = str(target or "").strip()
    if want not in allowed_labels:
        return False
    entries = await _collect_toggle_buttons(row, allowed_labels)
    if not entries:
        return await _is_labeled_toggle_selected(row, want)

    by_label: dict[str, tuple[float, Any]] = {}
    for label, btn, _x in entries:
        lum = await _element_bg_luminance(btn)
        prev = by_label.get(label)
        if prev is None or lum > prev[0]:
            by_label[label] = (lum, btn)

    if want not in by_label:
        return False
    if len(by_label) == 1:
        return True

    lum_want, btn_want = by_label[want]
    others = [lum for lbl, (lum, _) in by_label.items() if lbl != want]
    max_other = max(others) if others else 0.0
    if lum_want > max_other + 6:
        return True
    if max_other > lum_want + 6:
        return False
    return await _toggle_looks_selected(btn_want)


async def _toggle_button_in_row(
    row: Any,
    target: str,
    allowed_labels: tuple[str, ...],
) -> Any | None:
    want = str(target or "").strip()
    for label, btn, _x in await _collect_toggle_buttons(row, allowed_labels):
        if label == want:
            return btn
    return None


async def _select_toggle_in_row(
    row: Any,
    target: str,
    *,
    allowed_labels: tuple[str, ...],
    ordered_labels: tuple[str, ...] | None = None,
    log_name: str = "toggle",
) -> bool:
    """Chọn nút trong hàng toggle — xác nhận bằng độ sáng / aria."""
    want = str(target or "").strip()
    if want not in allowed_labels:
        return False

    if await _toggle_selected_in_row(row, want, allowed_labels):
        logger.info("Playwright UI: %s đã chọn sẵn", log_name)
        return True

    btn = await _toggle_button_in_row(row, want, allowed_labels)
    if btn is not None:
        try:
            await btn.scroll_into_view_if_needed(timeout=5000)
            await _human_click(btn,timeout=10000)
            await _ui_pause(0.2, f"sau bấm {log_name}")
            if await _toggle_selected_in_row(row, want, allowed_labels):
                await _ui_delay(f"sau chọn {log_name}")
                logger.info("Playwright UI: đã chọn %s", log_name)
                return True
            logger.warning(
                "Playwright UI: đã bấm %s nhưng UI vẫn chưa chọn (kiểm tra độ sáng nút)",
                log_name,
            )
        except Exception as exc:
            logger.warning("Playwright UI: click %s failed: %s", log_name, exc)

    order = ordered_labels or allowed_labels
    if want in order:
        try:
            entries = await _collect_toggle_buttons(row, allowed_labels)
            sorted_entries = sorted(entries, key=lambda item: item[2])
            if len(sorted_entries) >= 2:
                want_idx = order.index(want)
                if len(sorted_entries) == len(order):
                    _, fallback_btn, _ = sorted_entries[want_idx]
                else:
                    pick_idx = 0 if want_idx == 0 else len(sorted_entries) - 1
                    _, fallback_btn, _ = sorted_entries[pick_idx]
                if fallback_btn is not None and fallback_btn != btn:
                    await _human_click(fallback_btn,timeout=10000)
                    await _ui_pause(0.2, f"sau bấm {log_name} (vị trí)")
                    if await _toggle_selected_in_row(row, want, allowed_labels):
                        await _ui_delay(f"sau chọn {log_name}")
                        logger.info("Playwright UI: đã chọn %s (vị trí)", log_name)
                        return True
        except Exception:
            pass

    try:
        radio = row.get_by_role("radio", name=re.compile(rf"^{re.escape(want)}$", re.I))
        count = await radio.count()
        for idx in range(count):
            cand = radio.nth(idx)
            try:
                if not await cand.is_visible():
                    continue
                txt = (await cand.inner_text() or "").strip()
                if _toggle_button_label(txt, allowed_labels) != want:
                    continue
                await _human_click(cand,timeout=10000)
                await _ui_pause(0.2, f"sau bấm {log_name} (radio)")
                if await _toggle_selected_in_row(row, want, allowed_labels):
                    await _ui_delay(f"sau chọn {log_name}")
                    return True
            except Exception:
                continue
    except Exception:
        pass

    return False


async def _select_video_aspect_ratio(panel: Any, aspect_ratio: str) -> bool:
    target = _normalize_aspect_ratio(aspect_ratio)
    if target not in _VIDEO_ASPECT_RATIOS:
        target = "16:9"
    logger.info("Playwright UI: chọn video aspect ratio %s", target)
    row = await _video_aspect_ratio_row_locator(panel)
    if row is None:
        logger.warning("Playwright UI: không tìm thấy hàng ratio video")
        return False
    return await _select_toggle_in_row(
        row,
        target,
        allowed_labels=_VIDEO_ASPECT_RATIOS,
        ordered_labels=_VIDEO_ASPECT_RATIOS,
        log_name=f"video aspect ratio {target}",
    )


async def _panel_kind_row_locator(panel: Any) -> Any | None:
    """Hàng đầu panel settings: toggle 「Hình ảnh」 | 「Video」."""
    try:
        loc = panel.locator(
            "xpath=.//*[.//*[contains(normalize-space(.), 'Hình')]"
            " and .//*[normalize-space(text())='Video']]"
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
        return best
    except Exception:
        return None


async def _panel_kind_tab_active(panel: Any, kind: str) -> bool:
    """Tab Hình ảnh / Video trong hàng đầu panel đang được chọn."""
    target = "Video" if str(kind or "").strip().lower() == "video" else "Hình ảnh"
    row = await _panel_kind_row_locator(panel)
    scope = row if row is not None else panel
    try:
        loc = scope.locator("button, [role='button'], [role='tab'], [role='radio']")
        count = await loc.count()
        for idx in range(count):
            cand = loc.nth(idx)
            try:
                if not await cand.is_visible():
                    continue
                txt = (await cand.inner_text() or "").strip()
                if not _button_matches_kind_label(txt, target):
                    continue
                if await _toggle_looks_selected(cand):
                    return True
                cls = (await cand.get_attribute("class") or "").lower()
                if any(tok in cls for tok in ("bg-", "selected", "active", "pressed")):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


async def _panel_generation_kind_active(panel: Any, kind: str) -> bool:
    """Panel đang ở nhánh Hình ảnh (5 tỉ lệ) hay Video (Khung hình/Veo/duration)."""
    if await _panel_kind_tab_active(panel, kind):
        return True
    try:
        txt = (await panel.inner_text() or "").strip()
    except Exception:
        return False
    want_video = str(kind or "").strip().lower() == "video"
    if want_video:
        if "Khung hình" in txt and "Thành phần" in txt:
            return True
        if "Veo" in txt or "Omni Flash" in txt:
            return True
        if any(d in txt for d in _VIDEO_DURATIONS) and "4:3" not in txt:
            return True
        return False
    if _panel_has_aspect_toolbar_text(txt):
        return True
    if "4:3" in txt and ("Nano Banana" in txt or "Banana" in txt):
        return True
    return False


async def _toggle_looks_selected(el: Any) -> bool:
    for attr in ("aria-selected", "aria-pressed", "data-state"):
        try:
            val = (await el.get_attribute(attr) or "").strip().lower()
            if val in ("true", "on", "checked"):
                return True
        except Exception:
            continue
    try:
        return bool(
            await el.evaluate(
                """(node) => {
                function looksSelected(el) {
                    if (!el) return false;
                    for (const attr of ['aria-selected', 'aria-pressed', 'aria-checked', 'data-state']) {
                        const v = (el.getAttribute(attr) || '').toLowerCase();
                        if (['true', 'on', 'checked', 'active'].includes(v)) return true;
                    }
                    const cls = (el.className || '').toString().toLowerCase();
                    if (/\\b(selected|active|pressed|on)\\b/.test(cls)) return true;
                    if (/bg-(white|primary|secondary|accent|foreground)/.test(cls)) return true;
                    const style = window.getComputedStyle(el);
                    const bg = style.backgroundColor || '';
                    const m = bg.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
                    if (m) {
                        const lum = 0.299 * +m[1] + 0.587 * +m[2] + 0.114 * +m[3];
                        const a = bg.includes('rgba') ? parseFloat(bg.split(',').pop() || '1') : 1;
                        if (lum > 125 && a > 0.35) return true;
                    }
                    return false;
                }
                let n = node;
                for (let i = 0; i < 4; i++) {
                    if (looksSelected(n)) return true;
                    n = n.parentElement;
                }
                return false;
            }"""
            )
        )
    except Exception:
        return False


def _button_matches_kind_label(text: str, target: str) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return False
    want = _normalize_label_text(target)
    for line in raw.splitlines():
        line_n = _normalize_label_text(line)
        if line_n == want:
            return True
        if want in ("video", "hình ảnh", "image") and line_n == want:
            return True
    return _label_fuzzy_matches(raw, target)


async def _click_panel_kind_toggle(scope: Any, target: str) -> bool:
    """Bấm nút Hình ảnh hoặc Video trong hàng toggle panel (ảnh UI)."""
    role_re = (
        re.compile(r"^video$", re.I)
        if target == "Video"
        else re.compile(r"^hình ảnh$|^image$", re.I)
    )
    for role in ("tab", "radio", "button"):
        try:
            loc = scope.get_by_role(role, name=role_re)
            count = await loc.count()
            for idx in range(count):
                cand = loc.nth(idx)
                try:
                    if not await cand.is_visible() or not await cand.is_enabled():
                        continue
                    if await _toggle_looks_selected(cand):
                        return True
                    await cand.scroll_into_view_if_needed(timeout=5000)
                    await _human_click(cand,timeout=10000)
                    return True
                except Exception:
                    continue
        except Exception:
            pass
    try:
        loc = scope.locator("button, [role='button'], [role='tab'], [role='radio']")
        count = await loc.count()
        for idx in range(count):
            cand = loc.nth(idx)
            try:
                if not await cand.is_visible() or not await cand.is_enabled():
                    continue
                txt = (await cand.inner_text() or "").strip()
                if not _button_matches_kind_label(txt, target):
                    continue
                if await _toggle_looks_selected(cand):
                    return True
                await cand.scroll_into_view_if_needed(timeout=5000)
                await _human_click(cand,timeout=10000)
                return True
            except Exception:
                continue
    except Exception:
        pass
    return False


async def _select_generation_kind_in_panel(panel: Any, kind: str) -> bool:
    """
    Chọn 「Hình ảnh」 hoặc 「Video」 — hàng toggle đầu tiên trong panel settings.
    """
    want = str(kind or "").strip().lower()
    target = "Video" if want == "video" else "Hình ảnh"

    if await _panel_generation_kind_active(panel, kind):
        logger.info("Playwright UI: panel settings đã ở %s", target)
        return True

    row = await _panel_kind_row_locator(panel)
    scope = row if row is not None else panel

    if await _click_panel_kind_toggle(scope, target):
        await _ui_pause(0.55, f"chờ panel chuyển sang {target}")
        if await _panel_generation_kind_active(panel, kind):
            logger.info("Playwright UI: đã chọn %s trong panel settings", target)
            return True

    if await _click_row_toggle(scope, target):
        await _ui_pause(0.55, f"chờ panel (row toggle) {target}")
        if await _panel_generation_kind_active(panel, kind):
            return True

    if await _click_exact_label(panel, target):
        await _ui_pause(0.55, f"fallback chọn {target} trong panel")
        return await _panel_generation_kind_active(panel, kind)

    logger.warning("Playwright UI: không chọn được %s trong panel settings", target)
    return False


async def _click_row_toggle(scope: Any, label: str) -> bool:
    """Bấm đúng nút toggle trong một hàng (button/tab/radio) — không bấm span lẻ."""
    target = str(label or "").strip()
    if not target:
        return False
    role_name = re.compile(rf"^{re.escape(target)}$", re.I)
    for role in ("tab", "radio", "button"):
        try:
            loc = scope.get_by_role(role, name=role_name)
            count = await loc.count()
            for idx in range(count):
                cand = loc.nth(idx)
                try:
                    if not await cand.is_visible() or not await cand.is_enabled():
                        continue
                    if await _toggle_looks_selected(cand):
                        return True
                    await _human_click(cand,timeout=10000)
                    return True
                except Exception:
                    continue
        except Exception:
            pass
    try:
        loc = scope.locator(
            "button, [role='button'], [role='tab'], [role='radio'], div, span"
        )
        count = await loc.count()
        for idx in range(count):
            cand = loc.nth(idx)
            try:
                if not await cand.is_visible() or not await cand.is_enabled():
                    continue
                txt = (await cand.inner_text() or "").strip()
                if not _label_matches(txt, target):
                    continue
                tag = (await cand.evaluate("el => el.tagName") or "").upper()
                click_target = cand
                if tag in ("DIV", "SPAN"):
                    btn = cand.locator(
                        "xpath=ancestor-or-self::button[1]"
                        " | ancestor-or-self::*[@role='button'][1]"
                        " | ancestor-or-self::*[@role='radio'][1]"
                        " | ancestor-or-self::*[@role='tab'][1]"
                    )
                    if await btn.count() > 0:
                        click_target = btn.first
                if await _toggle_looks_selected(click_target):
                    return True
                await _human_click(click_target,timeout=10000)
                return True
            except Exception:
                continue
    except Exception:
        pass
    return False


async def _video_mode_row_locator(panel: Any) -> Any | None:
    try:
        loc = panel.locator(
            "xpath=.//*[.//*[normalize-space(text())='Khung hình']"
            " and .//*[normalize-space(text())='Thành phần']]"
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
        return best
    except Exception:
        return None


def _normalize_ui_words(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


_FRAME_SLOT_START_LABELS = ("Bắt đầu", "Start")
_FRAME_SLOT_END_LABELS = ("Kết thúc", "End")


async def _prompt_area_locator(page: Any) -> Any | None:
    """Vùng prompt rộng hơn composer — gồm hàng Khung hình Bắt đầu/Kết thúc."""
    prompt = await _prompt_input_locator(page)
    if prompt is None:
        return await _prompt_composer_locator(page)
    try:
        bar = prompt.locator(
            "xpath=ancestor::*[.//button or .//*[@role='button']][position()<=12][last()]"
        )
        if await bar.count() > 0:
            return bar.last
    except Exception:
        pass
    return await _prompt_composer_locator(page)


async def _frame_slots_row_locator(page: Any) -> Any | None:
    """Hàng chứa đồng thời Bắt đầu và Kết thúc."""
    try:
        loc = page.locator(
            "xpath=//*[.//*[contains(normalize-space(.), 'Bắt đầu')]"
            " and .//*[contains(normalize-space(.), 'thúc')]]"
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
        return best
    except Exception:
        return None


async def _frame_slot_click_target(el: Any) -> Any | None:
    for xpath in (
        "xpath=ancestor-or-self::button[1]",
        "xpath=ancestor-or-self::*[@role='button'][1]",
        "xpath=ancestor-or-self::*[@tabindex and not(@tabindex='-1')][1]",
        "xpath=ancestor-or-self::div[contains(@class,'cursor-pointer')][1]",
    ):
        try:
            loc = el.locator(xpath)
            if await loc.count() > 0:
                target = loc.first
                if await target.is_visible():
                    return target
        except Exception:
            continue
    try:
        if await el.is_visible():
            return el
    except Exception:
        pass
    return None


def _text_matches_frame_slot(text: str, slot: str) -> bool:
    norm = _normalize_ui_words(text)
    if str(slot or "").strip().lower() == "start":
        return norm in ("bắt đầu", "start") or norm.startswith("bắt đầu")
    if str(slot or "").strip().lower() == "end":
        if norm in ("kết thúc", "end"):
            return True
        return "kết" in norm and "thúc" in norm
    return False


async def _frame_slot_element_locator(page: Any, slot: str) -> Any | None:
    """Ô Bắt đầu / Kết thúc — chỉ tìm trong prompt bar (không quét cả page)."""
    labels = _FRAME_SLOT_START_LABELS if str(slot).lower() == "start" else _FRAME_SLOT_END_LABELS
    scopes: list[Any] = []
    row = await _frame_slots_row_locator(page)
    if row is not None:
        scopes.append(row)
    area = await _prompt_area_locator(page)
    if area is not None:
        scopes.append(area)
    composer = await _prompt_composer_locator(page)
    if composer is not None:
        scopes.append(composer)
    if not scopes:
        return None

    for scope in scopes:
        for label in labels:
            try:
                loc = scope.get_by_text(label, exact=False)
                count = min(await loc.count(), 12)
                for idx in range(count):
                    cand = loc.nth(idx)
                    try:
                        if not await cand.is_visible():
                            continue
                        txt = (await cand.inner_text() or "").strip()
                        if not _text_matches_frame_slot(txt, slot):
                            parent = cand.locator("xpath=ancestor::*[position()<=3][last()]")
                            if await parent.count() > 0:
                                txt = (await parent.first.inner_text() or "").strip()
                            if not _text_matches_frame_slot(txt, slot):
                                continue
                        target = await _frame_slot_click_target(cand)
                        if target is not None:
                            return target
                    except Exception:
                        continue
            except Exception:
                continue

        try:
            loc = scope.locator(
                "button, [role='button'], [role='tab'], div, span, label, a"
            )
            count = min(await loc.count(), 60)
            for idx in range(count):
                cand = loc.nth(idx)
                try:
                    if not await cand.is_visible():
                        continue
                    txt = (await cand.inner_text() or "").strip()
                    if not _text_matches_frame_slot(txt, slot):
                        continue
                    target = await _frame_slot_click_target(cand)
                    if target is not None:
                        return target
                except Exception:
                    continue
        except Exception:
            continue
    return None


async def _frame_slot_button_locator(page: Any, slot: str) -> Any | None:
    """Alias — tìm ô Bắt đầu / Kết thúc trên page."""
    return await _frame_slot_element_locator(page, slot)


async def _click_video_frame_slot(page: Any, slot: str) -> bool:
    """Bấm ô Bắt đầu (startImage) hoặc Kết thúc (endImage)."""
    target = "Bắt đầu" if str(slot or "").strip().lower() == "start" else "Kết thúc"
    btn = await _frame_slot_element_locator(page, slot)
    if btn is None:
        logger.warning("Playwright UI: không thấy ô %s", target)
        return False
    try:
        await btn.scroll_into_view_if_needed(timeout=5000)
        await _human_click(btn,timeout=10000)
        await _ui_delay(f"sau bấm ô {target}")
        logger.info("Playwright UI: đã bấm ô %s", target)
        return True
    except Exception as exc:
        logger.warning("Playwright UI: click ô %s failed: %s", target, exc)
        return False


async def _wait_for_frame_slot_filled(page: Any, slot: str, *, timeout_s: float = 30.0) -> bool:
    deadline = time.monotonic() + max(5.0, timeout_s)
    while time.monotonic() < deadline:
        btn = await _frame_slot_element_locator(page, slot)
        if btn is not None:
            try:
                img = btn.locator("img")
                if await img.count() > 0 and await img.first.is_visible():
                    return True
                txt = (await btn.inner_text() or "").strip()
                if not _text_matches_frame_slot(txt, slot):
                    return True
            except Exception:
                pass
        await asyncio.sleep(0.5)
    return False


async def _wait_for_video_frame_slots(page: Any, *, timeout_s: float = 15.0) -> bool:
    """Chờ UI hiện ô Bắt đầu + Kết thúc sau khi chọn Khung hình."""
    deadline = time.monotonic() + max(3.0, timeout_s)
    while time.monotonic() < deadline:
        start = await _frame_slot_element_locator(page, "start")
        end = await _frame_slot_element_locator(page, "end")
        if start is not None and end is not None:
            logger.info("Playwright UI: đã thấy ô Bắt đầu + Kết thúc")
            return True
        await asyncio.sleep(0.25)
    return False


async def _force_video_component_mode(
    page: Any,
    *,
    variant_count: int = 1,
    video_duration_s: Any = 8,
    dismiss_panel: bool = True,
) -> bool:
    """Chọn Thành phần trong panel đang mở (hoặc mở mới) — không đóng/mở lại giữa chừng."""
    panel = await _refresh_settings_panel(page) or await _open_settings_panel(
        page, generation_kind="video"
    )
    if panel is None:
        return False
    await _select_generation_kind_in_panel(panel, "video")
    panel = await _keep_settings_panel_scope(page, panel, kind="video") or panel
    await _select_video_mode_in_panel(panel, "component", page=page)
    panel = await _refresh_settings_panel(page, panel) or panel
    await _select_variant_count(panel, variant_count, page=page)
    await _select_video_duration(panel, video_duration_s)
    if dismiss_panel:
        await _dismiss_settings_panel(page, generation_kind="video", fast=True)
    await _ui_pause(0.35, "sau ép chọn Thành phần")
    return not await _wait_for_video_frame_slots(page, timeout_s=1.5)


async def _video_component_mode_active(page: Any) -> bool:
    """Thành phần = không hiện ô Bắt đầu/Kết thúc trong prompt bar."""
    return not await _wait_for_video_frame_slots(page, timeout_s=0.8)


async def _force_video_frame_mode(
    page: Any,
    *,
    variant_count: int = 1,
    video_duration_s: Any = 8,
    dismiss_panel: bool = True,
) -> bool:
    """Chọn Khung hình trong panel đang mở (hoặc mở mới)."""
    panel = await _refresh_settings_panel(page) or await _open_settings_panel(
        page, generation_kind="video"
    )
    if panel is None:
        return False
    await _select_generation_kind_in_panel(panel, "video")
    panel = await _keep_settings_panel_scope(page, panel, kind="video") or panel
    if not await _select_video_mode_in_panel(panel, "frame", page=page):
        return False
    panel = await _refresh_settings_panel(page, panel) or panel
    await _select_variant_count(panel, variant_count, page=page)
    await _select_video_duration(panel, video_duration_s)
    if dismiss_panel:
        await _dismiss_settings_panel(page, generation_kind="video")
    await _ui_delay("sau ép chọn Khung hình")
    return await _wait_for_video_frame_slots(page, timeout_s=15.0)


async def _video_frame_slots_visible(page: Any) -> bool:
    """Sau khi chọn Khung hình, prompt bar hiện slot Bắt đầu / Kết thúc."""
    return await _wait_for_video_frame_slots(page, timeout_s=6.0)


async def _is_video_mode_selected_in_panel(panel: Any, video_mode: str) -> bool:
    """Mode Khung hình/Thành phần đang active — đối chiếu cả nút còn lại."""
    mode = str(video_mode or "").strip().lower()
    target = "Khung hình" if mode == "frame" else "Thành phần"
    other = "Thành phần" if mode == "frame" else "Khung hình"
    row = await _video_mode_row_locator(panel)
    scope = row if row is not None else panel
    target_on = await _is_labeled_toggle_selected(scope, target)
    if not target_on:
        return False
    other_on = await _is_labeled_toggle_selected(scope, other)
    if other_on:
        return False
    return True


async def _click_video_mode_by_row_index(panel: Any, video_mode: str) -> bool:
    """Bấm Khung hình (0) hoặc Thành phần (1) theo thứ tự hàng toggle."""
    mode = str(video_mode or "").strip().lower()
    pick_idx = 0 if mode == "frame" else 1
    target = "Khung hình" if mode == "frame" else "Thành phần"
    row = await _video_mode_row_locator(panel)
    if row is None:
        return False
    try:
        buttons = row.locator("button, [role='button'], [role='tab'], [role='radio']")
        count = await buttons.count()
        if count <= pick_idx:
            return False
        cand = buttons.nth(pick_idx)
        if not await cand.is_visible():
            return False
        if await _toggle_looks_selected(cand):
            return True
        await cand.scroll_into_view_if_needed(timeout=5000)
        await _human_click(cand,timeout=10000)
        await _ui_pause(0.4, f"sau bấm mode video {target} (index {pick_idx})")
        return await _is_video_mode_selected_in_panel(panel, mode)
    except Exception:
        return False


def _submit_meta_as_merge_part(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "image_urls": list(meta.get("image_urls") or []),
        "media_ids": list(meta.get("media_ids") or []),
        "media_entries": list(meta.get("media_entries") or []),
        "status": int(meta.get("status") or 200),
    }


async def _absorb_extra_image_submit_responses(
    page: Any,
    meta: dict[str, Any],
    *,
    want: int,
    deadline: float,
) -> dict[str, Any]:
    """Gom thêm response batchGenerateImages khi các variant còn đang generate."""
    merged = dict(meta)
    while time.monotonic() < deadline:
        if await _has_generation_error_card(page):
            break
        remaining_ms = max(200, int((deadline - time.monotonic()) * 1000))
        if remaining_ms <= 0:
            break
        try:
            resp = await page.wait_for_response(
                _is_image_generate_response,
                timeout=min(2500, remaining_ms),
            )
            status, payload = await _extract_response_payload(resp)
            if status >= 400:
                continue
            part = _normalize_image_submit_payload(payload)
            part["status"] = status
            merged = _merge_generation_submit_parts(
                [_submit_meta_as_merge_part(merged), part],
                kind="image",
            )
            if _count_generation_outputs(merged, "image") >= want:
                break
        except Exception:
            await asyncio.sleep(0.45)
    return merged


_IMAGE_VARIANT_SETTLE_S: dict[int, float] = {1: 20.0, 2: 40.0, 3: 60.0, 4: 80.0}


def _image_variant_settle_timeout_s(variant_count: int) -> float:
    want = max(1, min(4, int(variant_count or 1)))
    return _IMAGE_VARIANT_SETTLE_S.get(want, 30.0)


def _variant_settle_timeout_s(
    generation_kind: str,
    variant_count: int,
    timeout_s: int | None,
) -> float:
    want = max(1, min(4, int(variant_count or 1)))
    if timeout_s is not None:
        return max(10.0, float(timeout_s))
    kind = str(generation_kind or "").strip().lower()
    if kind == "video":
        return max(30.0, want * 12.0)
    return _image_variant_settle_timeout_s(want)


async def _settle_variant_grid_with_retry(
    page: Any,
    *,
    generation_kind: str = "auto",
    variant_count: int = 1,
    existing: dict[str, Any] | None = None,
    reason: str = "after_submit",
    timeout_s: int | None = None,
    min_have: int = 0,
) -> dict[str, Any] | None:
    """
    Kiểm tra kết quả sau submit — đủ variant thì trả về ngay.
    Thiếu variant: chờ thêm (ảnh x2–x4 thường lần lượt) — không restart sớm.
    Chỉ restart khi có thẻ lỗi hoặc hết timeout mà chưa có output nào.
    """
    want = max(1, min(4, int(variant_count or 1)))
    kind = str(generation_kind or "").strip().lower()
    if kind == "auto":
        kind = "image"
    meta = dict(existing) if isinstance(existing, dict) else {}
    have = max(_count_generation_outputs(meta, kind), int(min_have or 0))

    if have >= want:
        if await _has_generation_error_card(page):
            await _reraise_as_upload_restart(
                page,
                RuntimeError(f"grid_error_with_partial have={have}/{want}"),
                context=reason,
            )
        return meta or existing

    total_wait = _variant_settle_timeout_s(kind, want, timeout_s)
    deadline = time.monotonic() + total_wait
    logger.info(
        "Playwright UI: chờ variant %s/%s (%s) tối đa %.0fs",
        have,
        want,
        kind,
        total_wait,
    )

    while time.monotonic() < deadline:
        if await _has_generation_error_card(page):
            await _reraise_as_upload_restart(
                page,
                RuntimeError(f"grid_error have={have}/{want}"),
                context=reason,
            )

        if kind == "image" and want > 1:
            meta = await _absorb_extra_image_submit_responses(
                page, meta, want=want, deadline=deadline
            )
        have = _count_generation_outputs(meta, kind)
        if have >= want:
            logger.info("Playwright UI: đủ variant %s/%s", have, want)
            return meta or existing

        await asyncio.sleep(0.45)

    have = _count_generation_outputs(meta, kind)
    if have > 0 and not await _has_generation_error_card(page):
        logger.info(
            "Playwright UI: partial %s/%s variant — không có thẻ lỗi, tiếp tục",
            have,
            want,
        )
        return meta or existing

    await _reraise_as_upload_restart(
        page,
        RuntimeError(f"incomplete_outputs have={have}/{want}"),
        context=reason,
    )


def _normalize_image_submit_payload(payload: Any) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data.get("data")
    if not isinstance(data, dict):
        data = payload if isinstance(payload, dict) else {}
    image_urls = flow_sdk.extract_image_urls(data)
    media_ids = flow_sdk.extract_image_media_ids(data)
    media_entries = flow_sdk.build_image_media_entries(data)
    return {
        "submitted": True,
        "image_urls": image_urls,
        "media_ids": media_ids,
        "media_entries": media_entries,
        "raw": data,
    }


async def _select_video_mode_in_panel(
    panel: Any,
    video_mode: str,
    *,
    page: Any | None = None,
) -> bool:
    mode = str(video_mode or "").strip().lower()
    target = "Khung hình" if mode == "frame" else "Thành phần"
    other = "Thành phần" if mode == "frame" else "Khung hình"
    logger.info("Playwright UI: chọn mode video %s", target)

    if await _is_video_mode_selected_in_panel(panel, mode):
        logger.info("Playwright UI: mode %s đã chọn sẵn — bỏ qua click", target)
        return True

    if await _is_labeled_toggle_selected(
        await _video_mode_row_locator(panel) or panel,
        other,
    ):
        logger.info(
            "Playwright UI: đang ở %s — bấm chuyển sang %s",
            other,
            target,
        )

    if await _click_video_mode_by_row_index(panel, mode):
        return True

    scopes: list[Any] = [panel]
    if page is not None:
        scopes.append(page)

    role_re = re.compile(rf"^{re.escape(target)}$", re.I)
    for scope in scopes:
        for role in ("tab", "radio", "button"):
            try:
                loc = scope.get_by_role(role, name=role_re)
                count = await loc.count()
                for idx in range(count):
                    cand = loc.nth(idx)
                    try:
                        if not await cand.is_visible() or not await cand.is_enabled():
                            continue
                        await cand.scroll_into_view_if_needed(timeout=5000)
                        await _human_click(cand,timeout=10000)
                        await _ui_pause(0.4, f"sau chọn mode video {target} ({role})")
                        if await _is_video_mode_selected_in_panel(panel, mode):
                            return True
                    except Exception:
                        continue
            except Exception:
                continue

    row = await _video_mode_row_locator(panel)
    if row is not None and await _click_row_toggle(row, target):
        await _ui_pause(0.4, f"sau chọn mode video {target}")
        if await _is_video_mode_selected_in_panel(panel, mode):
            return True

    if await _click_row_toggle(panel, target):
        await _ui_pause(0.4, f"sau chọn mode video {target}")
        if await _is_video_mode_selected_in_panel(panel, mode):
            return True

    if row is not None and await _click_exact_label(row, target):
        await _ui_pause(0.4, f"sau chọn mode video {target} (label)")
        if await _is_video_mode_selected_in_panel(panel, mode):
            return True

    if await _is_video_mode_selected_in_panel(panel, mode):
        logger.info("Playwright UI: mode %s active sau click", target)
        return True

    if page is not None:
        await _close_floating_overlays(page, preserve_settings_panel=True)
        refreshed = await _settings_popover_open(page)
        if refreshed is not None:
            panel = refreshed
            return await _select_video_mode_in_panel(panel, video_mode, page=None)

    logger.warning("Playwright UI: không chọn được mode video=%s", mode)
    return False


def _variant_count_label(count: int) -> str:
    """UI Flow: 1x | x2 | x3 | x4."""
    idx = max(1, min(4, int(count or 1))) - 1
    return _VARIANT_COUNT_UI[idx]


async def _variant_count_row_locator(panel: Any) -> Any | None:
    row = await _smallest_visible_locator(
        panel,
        "xpath=.//*[.//*[normalize-space(text())='1x']"
        " and .//*[normalize-space(text())='x2']"
        " and .//*[normalize-space(text())='x3']"
        " and .//*[normalize-space(text())='x4']]",
    )
    if row is not None:
        return row
    return await _smallest_visible_locator(
        panel,
        "xpath=.//*[.//*[normalize-space(text())='1x']"
        " and .//*[normalize-space(text())='x2']]",
    )


async def _is_labeled_toggle_selected(scope: Any, label: str) -> bool:
    target = str(label or "").strip()
    if not target:
        return False
    try:
        loc = scope.locator(
            "button, [role='button'], [role='tab'], [role='radio'], div, span"
        )
        count = await loc.count()
        for idx in range(count):
            cand = loc.nth(idx)
            try:
                if not await cand.is_visible():
                    continue
                txt = (await cand.inner_text() or "").strip()
                if not _label_matches(txt, target):
                    continue
                tag = (await cand.evaluate("el => el.tagName") or "").upper()
                check = cand
                if tag in ("DIV", "SPAN"):
                    check = await _resolve_toggle_click_target(cand)
                if await _toggle_looks_selected(check):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


async def _resolve_settings_panel_scope(page: Any, panel: Any, *, kind: str = "auto") -> Any:
    """Đảm bảo scope panel chứa đủ controls (mode, 1x, duration…)."""
    want_video = str(kind or "").strip().lower() == "video"
    candidates: list[Any] = []
    for cand in (panel, await _settings_popover_open(page)):
        if cand is not None and cand not in candidates:
            candidates.append(cand)
    for cand in candidates:
        try:
            if want_video and await _video_mode_row_locator(cand) is not None:
                return cand
            if await _variant_count_row_locator(cand) is not None:
                return cand
            if not want_video and await _aspect_ratio_toolbar_locator(cand) is not None:
                return cand
        except Exception:
            continue
    return panel or page


async def _select_variant_count(
    panel: Any,
    count: int = 1,
    *,
    page: Any | None = None,
) -> bool:
    """Chọn số lượng 1x | x2 | x3 | x4 theo variant_count từ request."""
    want = max(1, min(4, int(count or 1)))
    target = _variant_count_label(want)
    logger.info("Playwright UI: chọn số lượng %s (variant_count=%s)", target, want)

    scope = panel
    if page is not None:
        scope = await _resolve_settings_panel_scope(page, panel, kind="auto")

    row = await _variant_count_row_locator(scope)
    click_scope = row if row is not None else scope

    if await _select_toggle_in_row(
        click_scope,
        target,
        allowed_labels=_VARIANT_COUNT_UI,
        ordered_labels=_VARIANT_COUNT_UI,
        log_name=f"số lượng {target}",
    ):
        return True

    if want == 1:
        logger.info("Playwright UI: giữ mặc định 1x")
        return True

    logger.warning("Playwright UI: không chọn được số lượng %s", target)
    return False


def _is_single_duration_label(text: str) -> bool:
    compact = re.sub(r"\s+", " ", str(text or "").strip())
    return compact in _VIDEO_DURATIONS


def _duration_label_index(label: str) -> int:
    target = str(label or "8s").strip()
    if target in _VIDEO_DURATIONS:
        return _VIDEO_DURATIONS.index(target)
    return _VIDEO_DURATIONS.index("8s")


async def _duration_row_locator(panel: Any) -> Any | None:
    try:
        loc = panel.locator(
            "xpath=.//*[.//*[normalize-space(text())='4s']"
            " and .//*[normalize-space(text())='6s']"
            " and .//*[normalize-space(text())='8s']]"
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
        return best
    except Exception:
        return None


def _normalize_video_duration_s(value: Any) -> str:
    try:
        sec = int(value or 8)
    except (TypeError, ValueError):
        sec = 8
    if sec <= 4:
        return "4s"
    if sec <= 6:
        return "6s"
    if sec <= 8:
        return "8s"
    return "10s"


async def _click_duration_in_row(row: Any, target: str) -> bool:
    """Chọn 4s/6s/8s trong hàng duration — không bấm nhầm chip Video · 8s."""
    if await _click_row_toggle(row, target):
        return True
    try:
        buttons = row.locator("button, [role='button'], [role='radio']")
        btn_count = await buttons.count()
        pick_idx = _duration_label_index(target)
        if btn_count > pick_idx:
            cand = buttons.nth(pick_idx)
            if await cand.is_visible():
                await _human_click(cand,timeout=10000)
                return True
    except Exception:
        pass
    try:
        loc = row.locator("button, [role='button'], [role='radio'], div, span")
        count = await loc.count()
        for idx in range(count):
            cand = loc.nth(idx)
            try:
                if not await cand.is_visible():
                    continue
                txt = re.sub(r"\s+", " ", (await cand.inner_text() or "").strip())
                if not _is_single_duration_label(txt) or txt != target:
                    continue
                tag = (await cand.evaluate("el => el.tagName") or "").upper()
                if tag in ("DIV", "SPAN"):
                    btn = cand.locator(
                        "xpath=ancestor-or-self::button[1] | ancestor-or-self::*[@role='button'][1] | ancestor-or-self::*[@role='radio'][1]"
                    )
                    if await btn.count() > 0:
                        await _human_click(btn.first,timeout=10000)
                    else:
                        await _human_click(cand,timeout=10000)
                else:
                    await _human_click(cand,timeout=10000)
                return True
            except Exception:
                continue
    except Exception:
        pass
    return False


async def _select_video_duration(panel: Any, duration_s: Any = 8) -> bool:
    target = _normalize_video_duration_s(duration_s)
    logger.info("Playwright UI: chọn thời lượng video %s", target)
    if await _is_labeled_toggle_selected(panel, target):
        logger.info("Playwright UI: thời lượng %s đã chọn sẵn", target)
        return True
    row = await _duration_row_locator(panel)
    if row is not None and await _click_duration_in_row(row, target):
        await _ui_delay(f"sau chọn thời lượng {target}")
        return True
    try:
        radio = panel.get_by_role("radio", name=re.compile(rf"^{re.escape(target)}$", re.I))
        if await radio.count() > 0:
            await _human_click(radio.first,timeout=10000)
            await _ui_delay(f"sau chọn thời lượng {target}")
            return True
    except Exception:
        pass
    logger.warning("Playwright UI: không chọn được thời lượng %s", target)
    return False


async def _close_open_dropdowns(page: Any) -> bool:
    """Đóng menu/listbox/combobox đang mở — trả về True nếu đã bấm Escape."""
    for sel in (
        '[role="listbox"][data-state="open"]',
        '[role="menu"][data-state="open"]',
        '[data-radix-select-content][data-state="open"]',
        '[data-radix-popper-content-wrapper] [role="listbox"]',
    ):
        try:
            loc = page.locator(sel)
            count = await loc.count()
            for idx in range(count):
                cand = loc.nth(idx)
                if await cand.is_visible():
                    await page.keyboard.press("Escape")
                    await asyncio.sleep(0.2)
                    return True
        except Exception:
            continue
    return False


async def _close_floating_overlays(page: Any, *, preserve_settings_panel: bool = False) -> None:
    """Đóng overlay phụ. Không đóng panel settings chính khi đang mở."""
    if preserve_settings_panel or await _settings_panel_visibly_open(page):
        await _close_open_dropdowns(page)
        return
    for _ in range(3):
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.25)
        except Exception:
            pass


def _button_matches_single_ratio(text: str, target: str) -> bool:
    """Nút aspect ratio chỉ chứa đúng 1 label (vd. 9:16), không phải x2/x3/x4."""
    return _toggle_button_label(text, _ASPECT_RATIOS) == target


async def _select_aspect_ratio(panel: Any, aspect_ratio: str) -> bool:
    target = _normalize_aspect_ratio(aspect_ratio)
    toolbar = await _aspect_ratio_toolbar_locator(panel)
    if toolbar is None:
        logger.warning("Playwright UI: không tìm thấy hàng aspect ratio")
        return False
    logger.info("Playwright UI: chọn aspect ratio %s trong toolbar", target)
    return await _select_toggle_in_row(
        toolbar,
        target,
        allowed_labels=_ASPECT_RATIOS,
        ordered_labels=_ASPECT_RATIOS,
        log_name=f"aspect ratio {target}",
    )


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

    row = await _smallest_visible_locator(
        panel,
        "xpath=.//*[.//*[normalize-space(text())='16:9']"
        " and .//*[normalize-space(text())='4:3']"
        " and .//*[normalize-space(text())='1:1']"
        " and .//*[normalize-space(text())='3:4']"
        " and .//*[normalize-space(text())='9:16']"
        " and not(.//*[normalize-space(text())='x2'])]",
    )
    if row is not None:
        return row

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


async def _read_image_model_label(panel: Any) -> str:
    try:
        loc = panel.locator(
            "button[aria-haspopup='listbox'], [role='combobox'], button, [role='button'], div"
        ).filter(has_text=_MODEL_CHIP_PATTERNS)
        count = await loc.count()
        for idx in range(count):
            cand = loc.nth(idx)
            try:
                if not await cand.is_visible():
                    continue
                txt = (await cand.inner_text() or "").strip()
                if _UPLOAD_TEXT.search(txt):
                    continue
                if any(r in txt for r in _ASPECT_RATIOS) or _is_variant_count_label(txt):
                    continue
                low = txt.lower()
                if "nano banana" in low or re.search(r"\bbanana\b", low):
                    return txt
            except Exception:
                continue
    except Exception:
        pass
    return ""


async def _is_image_model_selected(panel: Any, variants: list[str]) -> bool:
    current = await _read_image_model_label(panel)
    if not current:
        try:
            current = (await panel.inner_text() or "").strip()
        except Exception:
            current = ""
    if not current:
        return False
    for name in variants:
        if _label_fuzzy_matches(current, name) or _label_fuzzy_matches(name, current):
            return True
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
                await _human_click(cand,timeout=10000)
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
                    low = txt.lower()
                    looks_image = "nano banana" in low
                    looks_video = any(k in low for k in ("veo 3.1", "omni flash", "lower priority"))
                    if (looks_image or looks_video) and _UPLOAD_TEXT.search(txt) is None:
                        return cand
                except Exception:
                    continue
        except Exception:
            continue
    return page


async def _select_image_model(page: Any, panel: Any, image_model: str) -> bool:
    variants = _image_model_name_variants(image_model)
    target = variants[0] if variants else _normalize_image_model_name(image_model)
    if not target:
        return False

    if await _is_image_model_selected(panel, variants or [target]):
        logger.info("Playwright UI: model image đã đúng %s — bỏ qua", target)
        return True

    if not await _open_model_dropdown_in_panel(panel):
        if await _is_image_model_selected(panel, variants or [target]):
            logger.info("Playwright UI: model image khớp %s (không cần dropdown)", target)
            return True
        logger.warning("Playwright UI: không mở được dropdown model")
        return False

    menu = await _model_dropdown_scope(page)
    if menu is None:
        return False

    for name in variants or [target]:
        if await _click_fuzzy_label(menu, name):
            await _ui_delay(f"sau chọn model {name}")
            logger.info("Playwright UI: đã chọn model %s", name)
            return True
        if await _click_exact_label(menu, name):
            await _ui_delay(f"sau chọn model {name}")
            logger.info("Playwright UI: đã chọn model %s", name)
            return True

    logger.warning("Playwright UI: không chọn được model image=%s", target)
    return False


async def _read_video_model_label(panel: Any) -> str:
    try:
        loc = panel.locator(
            "button[aria-haspopup='listbox'], [role='combobox'], button, [role='button'], div"
        ).filter(has_text=_MODEL_CHIP_VIDEO_PATTERNS)
        count = await loc.count()
        for idx in range(count):
            cand = loc.nth(idx)
            try:
                if not await cand.is_visible():
                    continue
                txt = (await cand.inner_text() or "").strip()
                if any(r in txt for r in _ASPECT_RATIOS) and "veo" not in txt.lower():
                    continue
                if _is_variant_count_label(txt):
                    continue
                low = txt.lower()
                if any(k in low for k in ("veo", "omni", "lite", "quality", "fast", "priority")):
                    return txt
            except Exception:
                continue
    except Exception:
        pass
    try:
        loc = panel.locator(
            "button[aria-haspopup='listbox'], [role='combobox'], button, [role='button'], div"
        )
        count = await loc.count()
        for idx in range(count):
            cand = loc.nth(idx)
            try:
                if not await cand.is_visible():
                    continue
                txt = (await cand.inner_text() or "").strip()
                low = txt.lower()
                if any(k in low for k in ("veo", "omni", "lite", "quality", "fast", "priority")):
                    if not any(r in txt for r in _ASPECT_RATIOS) and not _is_variant_count_label(txt):
                        return txt
            except Exception:
                continue
    except Exception:
        pass
    return ""


async def _is_video_model_selected(panel: Any, variants: list[str]) -> bool:
    current = await _read_video_model_label(panel)
    if not current:
        try:
            current = (await panel.inner_text() or "").strip()
        except Exception:
            current = ""
    if not current:
        return False
    for name in variants:
        if _label_fuzzy_matches(current, name) or _label_fuzzy_matches(name, current):
            return True
    return False


async def _open_video_model_dropdown_in_panel(panel: Any) -> bool:
    try:
        combobox = panel.locator(
            "button[aria-haspopup='listbox'], [role='combobox'], "
            "button:has(i.google-symbols:text-matches('expand_more|arrow_drop_down')), "
            "[role='button'], button, div"
        )
        count = await combobox.count()
        for idx in range(count):
            cand = combobox.nth(idx)
            try:
                if not await cand.is_visible() or not await cand.is_enabled():
                    continue
                txt = (await cand.inner_text() or "").strip().lower()
                if any(r in txt for r in _ASPECT_RATIOS):
                    continue
                if _is_variant_count_label(txt):
                    continue
                if "khung hình" in txt or "thành phần" in txt:
                    continue
                if any(k in txt for k in ("veo", "omni", "lite", "quality", "fast", "priority")):
                    await cand.scroll_into_view_if_needed(timeout=5000)
                    await _human_click(cand,timeout=10000)
                    await _ui_pause(0.35, "sau mở danh sách model video (combobox)")
                    return True
            except Exception:
                continue
    except Exception:
        pass

    try:
        loc = panel.locator("button, [role='button'], [role='combobox'], div")
        count = await loc.count()
        for idx in range(count):
            cand = loc.nth(idx)
            try:
                if not await cand.is_visible() or not await cand.is_enabled():
                    continue
                txt = (await cand.inner_text() or "").strip()
                low = txt.lower()
                if not txt:
                    continue
                if _UPLOAD_TEXT.search(txt):
                    continue
                if any(r in txt for r in _ASPECT_RATIOS):
                    continue
                if _is_variant_count_label(txt):
                    continue
                if "khung hình" in low or "thành phần" in low:
                    continue
                if "veo" in low or "omni" in low or "lite" in low or "quality" in low or "fast" in low:
                    await _human_click(cand,timeout=10000)
                    await _ui_delay("sau mở danh sách model video")
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


async def _select_video_model(page: Any, panel: Any, video_quality: str) -> bool:
    variants = _video_model_name_variants(video_quality)
    target = variants[0] if variants else _normalize_video_model_name(video_quality)
    if await _is_video_model_selected(panel, variants or [target]):
        logger.info("Playwright UI: model video đã đúng %s — bỏ qua", target)
        return True
    if not await _open_video_model_dropdown_in_panel(panel):
        # Dropdown không mở được nhưng label hiện tại có thể đã khớp
        if await _is_video_model_selected(panel, variants or [target]):
            logger.info("Playwright UI: model video khớp %s (không cần dropdown)", target)
            return True
        logger.warning("Playwright UI: không mở được dropdown model video")
        return False
    menu = await _model_dropdown_scope(page)
    if menu is None:
        return False
    for name in variants or [target]:
        if await _click_fuzzy_label(menu, name):
            await _ui_delay(f"sau chọn model video {name}")
            logger.info("Playwright UI: đã chọn model video %s", name)
            await _close_floating_overlays(page, preserve_settings_panel=True)
            return True
        if await _click_exact_label(menu, name):
            await _ui_delay(f"sau chọn model video {name}")
            logger.info("Playwright UI: đã chọn model video %s", name)
            await _close_floating_overlays(page, preserve_settings_panel=True)
            return True
    logger.warning("Playwright UI: không chọn được model video=%s", target)
    return False


async def _settings_panel_visibly_open(page: Any) -> bool:
    """Panel settings thật sự đang mở (hàng Hình ảnh|Video nhìn thấy được)."""
    row = await _panel_kind_row_locator(page)
    if row is None:
        return False
    try:
        if not await row.is_visible():
            return False
    except Exception:
        return False
    panel = await _settings_panel_from_kind_row(page)
    if panel is None:
        return False
    try:
        return await panel.is_visible()
    except Exception:
        return False


async def _dismiss_settings_panel(page: Any, *, generation_kind: str = "auto", fast: bool = False) -> bool:
    """Đóng panel settings (và dropdown model nếu còn mở)."""
    async def _after_dismiss_pause(step: str) -> None:
        if fast:
            await _ui_pause(0.25, step)
        else:
            await _ui_delay(step)

    await _close_floating_overlays(page, preserve_settings_panel=False)
    if not await _settings_panel_visibly_open(page):
        return True

    prompt = await _prompt_input_locator(page)
    if prompt is not None:
        try:
            await _human_click(prompt,timeout=5000)
            await _after_dismiss_pause("sau click prompt đóng panel")
        except Exception:
            pass

    await _close_floating_overlays(page)

    prefer = generation_kind if generation_kind in ("image", "video") else "auto"
    chip = await _model_chip_button_locator(page, prefer=prefer)
    if chip is not None and await _settings_panel_visibly_open(page):
        try:
            await _human_click(chip,timeout=5000)
            await _after_dismiss_pause("toggle chip đóng panel settings")
        except Exception:
            pass

    await _close_floating_overlays(page)

    if not await _settings_panel_visibly_open(page):
        return True

    # Panel detector đôi khi vẫn báo open dù UI đã đóng — kiểm tra chip video.
    if prefer == "video":
        chip2 = await _model_chip_button_locator(page, prefer="video")
        if chip2 is not None:
            try:
                txt = (await chip2.inner_text() or "").strip()
                if "video" in txt.lower() and re.search(r"\b\d+s\b", txt):
                    logger.info("Playwright UI: chip video hiển thị settings — coi như dismiss OK")
                    return True
            except Exception:
                pass

    logger.warning("Playwright UI: panel settings vẫn còn mở sau dismiss")
    return False


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
    variant_count: int = 1,
) -> dict[str, Any]:
    """
    Nhánh image:
    1) mở panel settings (hoặc dùng panel đang mở)
    2) chọn Hình ảnh → ratio → số lượng → model trong cùng panel
    3) đóng panel khi xong.
    """
    target_ratio = _normalize_aspect_ratio(aspect_ratio)
    target_model = _normalize_image_model_name(image_model)

    panel = await _open_settings_panel(page, generation_kind="image")
    if panel is None:
        return {
            "configured": False,
            "kind_selected": False,
            "aspect_ratio_selected": False,
            "image_model_selected": False,
            "panel_dismissed": False,
            "unified_media_ui": True,
            "aspect_ratio": target_ratio,
            "image_model": target_model,
        }

    try:
        panel_txt = ((await panel.inner_text()) or "").strip()[:200]
        logger.info("Playwright UI: panel settings text=%r", panel_txt)
    except Exception:
        pass

    kind_ok = await _select_generation_kind_in_panel(panel, "image")
    panel = await _keep_settings_panel_scope(page, panel, kind="image") or panel
    media_ready = await _ensure_image_media_settings_ready(panel) if kind_ok else False
    panel = await _refresh_settings_panel(page, panel) or panel
    ratio_ok = await _select_aspect_ratio(panel, target_ratio) if media_ready else False
    variant_ok = (
        await _select_variant_count(panel, variant_count, page=page)
        if media_ready
        else False
    )
    panel = await _refresh_settings_panel(page, panel) or panel
    model_ok = await _select_image_model(page, panel, target_model)
    dismissed = await _dismiss_settings_panel(page, generation_kind="image", fast=True)
    chip_ok = await _chip_shows_model(page, target_model) if model_ok else False

    configured = kind_ok and media_ready and variant_ok and ratio_ok and model_ok and dismissed
    if not configured:
        logger.warning(
            "Playwright UI: image settings chưa đủ kind=%s media=%s variant=%s ratio=%s model=%s dismissed=%s chip=%s",
            kind_ok,
            media_ready,
            variant_ok,
            ratio_ok,
            model_ok,
            dismissed,
            chip_ok,
        )

    return {
        "configured": configured,
        "kind_selected": kind_ok,
        "unified_media_ui": True,
        "media_panel_ready": media_ready,
        "variant_selected": variant_ok,
        "aspect_ratio_selected": ratio_ok,
        "image_model_selected": model_ok,
        "panel_dismissed": dismissed,
        "chip_verified": chip_ok,
        "aspect_ratio": target_ratio,
        "image_model": target_model,
        "variant_count": max(1, int(variant_count or 1)),
    }


async def configure_video_generation_ui(
    page: Any,
    *,
    aspect_ratio: str,
    video_quality: str,
    video_mode: str,
    video_duration_s: Any = 8,
    variant_count: int = 1,
) -> dict[str, Any]:
    """
    Nhánh video:
    1) mở panel settings (hoặc dùng panel đang mở)
    2) chọn Video → mode → ratio → số lượng → duration → model trong cùng panel
    3) đóng panel khi xong.
    """
    target_ratio = _normalize_aspect_ratio(aspect_ratio)
    target_model = _normalize_video_model_name(video_quality)
    mode = "frame" if str(video_mode or "").strip().lower() == "frame" else "component"
    logger.info("Playwright UI: cấu hình video — mode UI=%s", mode)

    panel = await _open_settings_panel(page, generation_kind="video")
    if panel is None:
        return {
            "configured": False,
            "kind_selected": False,
            "video_mode_selected": False,
            "aspect_ratio_selected": False,
            "video_model_selected": False,
            "panel_dismissed": False,
            "aspect_ratio": target_ratio,
            "video_model": target_model,
            "video_mode": mode,
        }

    kind_ok = await _select_generation_kind_in_panel(panel, "video")
    panel = await _keep_settings_panel_scope(page, panel, kind="video") or panel
    media_ready = await _ensure_video_panel_ready(panel) if kind_ok else False
    mode_ok = await _select_video_mode_in_panel(panel, mode, page=page) if kind_ok else False
    panel = await _refresh_settings_panel(page, panel) or panel
    ratio_ok = await _select_video_aspect_ratio(panel, target_ratio) if media_ready else False
    variant_ok = (
        await _select_variant_count(panel, variant_count, page=page)
        if media_ready
        else False
    )
    duration_ok = await _select_video_duration(panel, video_duration_s) if media_ready else False
    panel = await _refresh_settings_panel(page, panel) or panel
    model_ok = await _select_video_model(page, panel, video_quality)
    await _close_floating_overlays(page, preserve_settings_panel=True)
    panel = await _refresh_settings_panel(page, panel) or panel
    mode_confirmed_in_panel = mode_ok
    if kind_ok and panel is not None and not mode_ok:
        mode_ok = await _select_video_mode_in_panel(panel, mode, page=page)
        mode_confirmed_in_panel = mode_ok
        await _ui_pause(0.3, f"xác nhận {mode} trước khi đóng panel")
    dismissed = await _dismiss_settings_panel(page, generation_kind="video", fast=True)

    frame_slots_ok = True
    component_mode_ok = True
    if mode == "frame":
        await _ui_pause(0.5, "chờ UI Khung hình sau đóng panel")
        wait_s = 4.0 if mode_confirmed_in_panel else 10.0
        frame_slots_ok = await _wait_for_video_frame_slots(page, timeout_s=wait_s)
        if not frame_slots_ok and not mode_confirmed_in_panel:
            logger.warning("Playwright UI: chưa thấy Bắt đầu/Kết thúc — ép chọn Khung hình lại")
            frame_slots_ok = await _force_video_frame_mode(
                page,
                variant_count=variant_count,
                video_duration_s=video_duration_s,
            )
        elif not frame_slots_ok:
            logger.warning(
                "Playwright UI: Khung hình đã chọn trong panel nhưng slot chưa hiện — chờ thêm"
            )
            await _ui_pause(1.0, "chờ slot Khung hình")
            frame_slots_ok = await _wait_for_video_frame_slots(page, timeout_s=6.0)
        if frame_slots_ok:
            mode_ok = True
    elif mode == "component":
        await _ui_pause(0.25, "chờ UI Thành phần sau đóng panel")
        if mode_confirmed_in_panel:
            component_mode_ok = True
        elif mode_ok:
            component_mode_ok = await _video_component_mode_active(page)
        else:
            still_frame = await _wait_for_video_frame_slots(page, timeout_s=0.8)
            component_mode_ok = not still_frame
        if not component_mode_ok and not mode_confirmed_in_panel:
            logger.warning("Playwright UI: vẫn thấy Khung hình — ép chọn Thành phần")
            component_mode_ok = await _force_video_component_mode(
                page,
                variant_count=variant_count,
                video_duration_s=video_duration_s,
            )
        if component_mode_ok:
            mode_ok = True

    configured = (
        kind_ok
        and media_ready
        and mode_ok
        and ratio_ok
        and variant_ok
        and duration_ok
        and model_ok
        and dismissed
    )
    if mode == "frame" and not frame_slots_ok:
        logger.warning(
            "Playwright UI: Khung hình chưa hiện slot Bắt đầu/Kết thúc — "
            "vẫn tiếp tục, sẽ thử lại trước upload"
        )
    if mode == "component" and not component_mode_ok:
        logger.warning(
            "Playwright UI: Thành phần chưa active — vẫn tiếp tục, sẽ thử lại trước upload"
        )
    if not configured:
        logger.warning(
            "Playwright UI: video settings chưa đủ kind=%s media=%s mode=%s ratio=%s "
            "variant=%s duration=%s model=%s dismissed=%s frame_slots=%s component=%s",
            kind_ok,
            media_ready,
            mode_ok,
            ratio_ok,
            variant_ok,
            duration_ok,
            model_ok,
            dismissed,
            frame_slots_ok,
            component_mode_ok,
        )
    return {
        "configured": configured,
        "kind_selected": kind_ok,
        "media_panel_ready": media_ready,
        "video_mode_selected": mode_ok,
        "aspect_ratio_selected": ratio_ok,
        "variant_selected": variant_ok,
        "duration_selected": duration_ok,
        "video_model_selected": model_ok,
        "panel_dismissed": dismissed,
        "frame_slots_visible": frame_slots_ok,
        "component_mode_active": component_mode_ok,
        "aspect_ratio": target_ratio,
        "video_model": target_model,
        "video_mode": mode,
        "video_duration_s": _normalize_video_duration_s(video_duration_s),
        "variant_count": max(1, int(variant_count or 1)),
    }


async def _attempt_plus_menu_upload(page: Any, *, image_path: Path) -> Any:
    """Upload qua [+] → Tải nội dung (Thành phần / ảnh) — fast input file trước."""
    response_matcher = _upload_image_response_matcher
    if not await _open_image_upload_menu(page):
        raise RuntimeError(f"upload_menu_not_open url={(page.url or '')[:120]}")
    await _ui_pause(0.35, "menu + mở")

    try:
        async with page.expect_response(response_matcher, timeout=120_000) as resp_info:
            if await _set_files_via_hidden_input(page, image_path, fast=True):
                return await resp_info.value
    except Exception as exc:
        logger.info("Playwright UI: plus fast upload: %s", exc)

    try:
        async with page.expect_response(response_matcher, timeout=120_000) as resp_info:
            async with page.expect_file_chooser(timeout=12_000) as fc_info:
                if await _has_upload_item_visible(page):
                    if not await _click_upload_menu_item(page):
                        raise RuntimeError("upload_button_not_found")
                else:
                    raise RuntimeError("upload_menu_not_open")
                chooser = await fc_info.value
                await chooser.set_files(str(image_path))
                await _ui_pause(0.2, "file chooser +")
            return await resp_info.value
    except Exception as exc:
        logger.warning("Playwright UI: plus file chooser failed: %s — thử input lần 2", exc)

    await _open_image_upload_menu(page)
    async with page.expect_response(response_matcher, timeout=120_000) as resp_info:
        if await _has_upload_item_visible(page):
            await _click_upload_menu_item(page)
            await _ui_pause(0.2, "chờ input ẩn +")
        if not await _set_files_via_hidden_input(page, image_path, fast=True):
            raise RuntimeError("file_chooser_timeout")
        return await resp_info.value


async def _attempt_frame_slot_upload(page: Any, *, image_path: Path, slot: str) -> Any:
    """Upload Khung hình: Bắt đầu/Kết thúc → input file ẩn (nhanh) → preview → Thêm vào câu lệnh."""
    response_matcher = _upload_image_response_matcher

    if not await _click_video_frame_slot(page, slot):
        raise RuntimeError(f"frame_slot_not_found:{slot}")
    await _ui_pause(0.45, "modal Khung hình")

    # Fast path: CDP thường không có file chooser — dùng input[type=file] ngay
    try:
        async with page.expect_response(response_matcher, timeout=120_000) as resp_info:
            if await _set_files_via_hidden_input(page, image_path, fast=True):
                return await resp_info.value
    except Exception as exc:
        logger.info("Playwright UI: Khung hình fast upload (%s): %s", slot, exc)

    # Fallback: menuitem upload + file chooser (timeout ngắn hơn)
    if not await _click_video_frame_slot(page, slot):
        raise RuntimeError(f"frame_slot_not_found:{slot}")
    await _ui_pause(0.35, "mở lại modal Khung hình")

    try:
        async with page.expect_response(response_matcher, timeout=120_000) as resp_info:
            async with page.expect_file_chooser(timeout=12_000) as fc_info:
                if await _has_upload_item_visible(page):
                    if not await _click_upload_menu_item(page):
                        raise RuntimeError("frame_upload_button_not_found")
                else:
                    raise RuntimeError("frame_upload_menu_not_open")
                chooser = await fc_info.value
                await chooser.set_files(str(image_path))
                await _ui_pause(0.2, f"file chooser {slot}")
            return await resp_info.value
    except Exception as exc:
        logger.warning(
            "Playwright UI: frame file chooser failed (%s): %s — thử input lần 2",
            slot,
            exc,
        )

    async with page.expect_response(response_matcher, timeout=120_000) as resp_info:
        if not await _set_files_via_hidden_input(page, image_path, fast=True):
            raise RuntimeError("frame_file_chooser_timeout")
        return await resp_info.value


async def upload_image_via_ui(
    page: Any,
    *,
    image_path: Path,
    frame_slot: str | None = None,
) -> dict[str, Any]:
    """
    Upload ảnh qua UI Flow.
    - frame_slot='start' → ô Bắt đầu (startImage)
    - frame_slot='end' → ô Kết thúc (endImage)
    - không có frame_slot → menu [+] (ảnh / Thành phần)
    """
    if await _settings_popover_open(page) is not None:
        await _dismiss_settings_panel(page)

    slot = str(frame_slot or "").strip().lower() or None
    if slot not in (None, "start", "end"):
        slot = None

    async def _finalize_upload_response(resp: Any, *, phase: str) -> dict[str, Any]:
        status, body = await _extract_response_payload(resp)
        if status >= 400:
            err = RuntimeError(f"upload_image_http_{status}: {body}")
            await _reraise_as_upload_restart(page, err, context="upload_http")
        if not await _wait_for_upload_preview(page, filename=image_path.name):
            if slot and await _wait_for_frame_slot_filled(page, slot, timeout_s=8.0):
                logger.info("Playwright UI: ảnh Khung hình gắn trực tiếp vào ô %s", slot)
                return {"status": status, "data": body, "phase": phase, "frame_slot": slot}
            err = RuntimeError(
                f"frame_slot_preview_not_ready:{slot}" if slot else "upload_preview_not_ready"
            )
            await _reraise_as_upload_restart(page, err, context="upload_preview")
        added = await _click_add_to_prompt(page)
        if not added:
            logger.warning(
                "add_to_prompt button not found — ảnh có thể đã được gắn tự động (slot=%s)",
                slot or "-",
            )
        if slot:
            await _wait_for_frame_slot_filled(page, slot, timeout_s=12.0)
        out = {"status": status, "data": body, "phase": phase, "added_to_prompt": added}
        if slot:
            out["frame_slot"] = slot
        return out

    if slot:
        try:
            resp = await _attempt_frame_slot_upload(page, image_path=image_path, slot=slot)
            return await _finalize_upload_response(resp, phase=f"frame_{slot}")
        except UiFlowRestartFromUpload:
            raise
        except Exception as exc:
            logger.warning("Playwright UI: upload Khung hình (%s) lỗi: %s", slot, exc)
            await _reraise_as_upload_restart(page, exc, context=f"frame_{slot}")

    try:
        resp = await _attempt_plus_menu_upload(page, image_path=image_path)
        return await _finalize_upload_response(resp, phase="initial_upload")
    except UiFlowRestartFromUpload:
        raise
    except Exception as exc:
        logger.warning("Playwright UI: upload lỗi ban đầu: %s", exc)
        await _reraise_as_upload_restart(page, exc, context="plus_upload")


async def fill_prompt(page: Any, prompt: str) -> None:
    text = str(prompt or "").strip()
    if not text:
        return

    try:
        loc = page.get_by_placeholder(_PROMPT_PLACEHOLDER)
        if await loc.count() > 0:
            target = loc.last
            await _human_click(target,timeout=5000)
            await target.fill(text, timeout=5000)
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
            await _human_click(target,timeout=8000)
            await _ui_delay("sau focus ô prompt")
            await target.fill(text, timeout=8000)
            await _ui_delay("sau điền prompt")
            return
        except Exception:
            continue

    # Fallback: contenteditable
    editable = page.locator('[contenteditable="true"]').last
    await _human_click(editable,timeout=5000)
    try:
        await editable.fill(text, timeout=5000)
    except Exception:
        await page.keyboard.press("Control+A")
        await page.keyboard.insert_text(text)
    await _ui_delay("sau điền prompt (contenteditable)")


def _is_image_generate_response(resp: Any) -> bool:
    try:
        url = str(resp.url or "")
    except Exception:
        return False
    return bool(_GENERATE_IMAGE_API_PATTERNS.search(url))


def _is_video_generate_response(resp: Any) -> bool:
    try:
        url = str(resp.url or "")
    except Exception:
        return False
    return bool(_GENERATE_VIDEO_API_PATTERNS.search(url))


async def _submit_arrow_button(page: Any) -> Any | None:
    """Nút gửi ở bên phải prompt bar (mũi tên tròn)."""
    chip = await _model_chip_button_locator(page, prefer="auto")
    if chip is not None:
        arrow = await _chip_adjacent_submit_arrow(chip)
        if arrow is not None:
            logger.info("Playwright UI: mũi tên submit kế chip settings")
            return arrow

    arrows = await _all_submit_arrow_buttons(page)
    if not arrows:
        return None
    near_y = await _prompt_bar_y_anchor(page)
    best = arrows[-1]
    if near_y is None or len(arrows) == 1:
        return best
    best_dx = float("inf")
    for cand in arrows:
        try:
            box = await cand.bounding_box()
            if not box:
                continue
            dx = abs(float(box.get("x") or 0))
            dy = abs(float(box.get("y") or 0) - near_y)
            score = dx + dy * 2
            if score < best_dx:
                best_dx = score
                best = cand
        except Exception:
            continue
    return best


async def _generation_error_card_locators(page: Any) -> list[Any]:
    """Thẻ lỗi 「Không thành công」 trên lưới kết quả (x2 → 2 thẻ)."""
    cards: list[Any] = []
    seen: set[int] = set()
    try:
        hits = page.get_by_text(re.compile(r"không thành công", re.I))
        count = await hits.count()
        for idx in range(count):
            node = hits.nth(idx)
            try:
                if not await node.is_visible():
                    continue
                card = None
                for xpath in (
                    "xpath=ancestor::*[.//button or .//*[@role='button']][position()<=12][last()]",
                    "xpath=ancestor::div[position()<=10][last()]",
                ):
                    loc = node.locator(xpath)
                    if await loc.count() == 0:
                        continue
                    cand = loc.first
                    if not await cand.is_visible():
                        continue
                    txt = (await cand.inner_text() or "").strip()
                    if not _ERROR_BANNER_PATTERNS.search(txt):
                        continue
                    card = cand
                    break
                if card is None:
                    continue
                oid = await card.evaluate(
                    "el => (el.innerText||'').length ^ el.getBoundingClientRect().width"
                )
                if oid in seen:
                    continue
                seen.add(oid)
                cards.append(card)
            except Exception:
                continue
    except Exception:
        pass
    return cards


async def _extract_response_payload(resp: Any) -> tuple[int, Any]:
    status = int(resp.status or 0)
    body: Any = None
    try:
        body = await resp.json()
    except Exception:
        body = await resp.text()
    return status, body


def _normalize_video_submit_payload(payload: Any) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data.get("data")
    if not isinstance(data, dict):
        data = payload if isinstance(payload, dict) else {}
    ops = data.get("operations") if isinstance(data, dict) else []
    if not isinstance(ops, list):
        ops = []
    media_ids = flow_sdk.collect_video_poll_media_ids(data, ops)
    return {
        "submitted": True,
        "media_ids": media_ids,
        "raw": data,
    }


def _merge_generation_submit_parts(
    parts: list[dict[str, Any]],
    *,
    kind: str = "image",
) -> dict[str, Any]:
    merged: dict[str, Any] = {
        "submitted": True,
        "image_urls": [],
        "media_ids": [],
        "media_entries": [],
    }
    seen_urls: set[str] = set()
    seen_mids: set[str] = set()
    seen_entry: set[str] = set()

    for part in parts:
        for u in part.get("image_urls") or []:
            s = str(u or "").strip()
            if s and s not in seen_urls:
                seen_urls.add(s)
                merged["image_urls"].append(s)
        for m in part.get("media_ids") or []:
            s = str(m or "").strip()
            if s and s not in seen_mids:
                seen_mids.add(s)
                merged["media_ids"].append(s)
        for e in part.get("media_entries") or []:
            if not isinstance(e, dict):
                continue
            mid = str(e.get("media_id") or e.get("mediaId") or "").strip()
            url = str(e.get("url") or "").strip()
            key = mid or url or str(e)
            if key in seen_entry:
                continue
            seen_entry.add(key)
            merged["media_entries"].append(e)
            if mid:
                seen_mids.add(mid)
            if url:
                seen_urls.add(url)

    if kind == "video" and not merged["media_ids"]:
        for part in parts:
            for m in part.get("media_ids") or []:
                s = str(m or "").strip()
                if s and s not in seen_mids:
                    seen_mids.add(s)
                    merged["media_ids"].append(s)

    merged["status"] = int(parts[-1].get("status") or 200) if parts else 200
    merged["source"] = str(parts[-1].get("source") or "submit_arrow") if parts else "submit_arrow"
    if parts:
        merged["raw"] = parts[-1].get("raw")
    return merged


async def _collect_submit_responses(
    page: Any,
    *,
    submit: Any,
    matcher: Any,
    normalize: Any,
    variant_count: int = 1,
    timeout_s: int | None = None,
    label: str = "image",
    profile_id: str = "",
) -> dict[str, Any]:
    """Bấm submit và gom tới variant_count response batchGenerate* (1–4)."""
    want = max(1, min(4, int(variant_count or 1)))
    submit_timeout_s = (
        int(timeout_s) if timeout_s is not None else int(UI_GENERATION_SUBMIT_TIMEOUT_S)
    )
    timeout_ms = max(30_000, submit_timeout_s * 1000)
    parts: list[dict[str, Any]] = []

    try:
        async with page.expect_response(matcher, timeout=timeout_ms) as resp_info:
            await submit.scroll_into_view_if_needed(timeout=3000)
            await _wait_submit_clickable(submit, timeout_s=min(25.0, submit_timeout_s))
            await _human_click(submit, timeout=8000)
            await _ui_delay(f"sau bấm mũi tên gửi {label}")
        responses = [await resp_info.value]
    except UiFlowRestartFromUpload:
        raise
    except Exception as exc:
        await _reraise_as_upload_restart(page, exc, context=f"submit_click_{label}")

    # Một response batchGenerate* thường đã chứa đủ variant; chờ thêm nếu thiếu (ảnh x2–x4 lâu hơn).
    if want > 1 and len(responses) < want:
        extra_wait = _image_variant_settle_timeout_s(want) if label == "image" else min(12.0, want * 3.0)
        extra_deadline = time.monotonic() + extra_wait
        while len(responses) < want and time.monotonic() < extra_deadline:
            remaining_ms = max(300, int((extra_deadline - time.monotonic()) * 1000))
            try:
                resp = await page.wait_for_response(
                    matcher, timeout=min(remaining_ms, 4000)
                )
                responses.append(resp)
            except Exception:
                break

    for resp in responses:
        status, payload = await _extract_response_payload(resp)
        if status == 403:
            err_msg = f"{label}_submit_http_{status}: {payload}"
            pid = str(profile_id or "").strip()
            if pid:
                from flow2api.services.profile_403_cache import notify_403_cache_pause_if_needed

                await notify_403_cache_pause_if_needed(
                    pid,
                    None,
                    err_msg,
                    [{"http_status": status, "data": payload if isinstance(payload, dict) else {}}],
                )
            raise FlowHttp403Error(err_msg)
        if status >= 400:
            err = RuntimeError(f"{label}_submit_http_{status}: {payload}")
            await _reraise_as_upload_restart(page, err, context=f"submit_http_{label}")
        part = normalize(payload)
        part["status"] = status
        parts.append(part)

    if not parts:
        err = RuntimeError(f"{label}_submit_empty_response")
        await _reraise_as_upload_restart(page, err, context=f"submit_empty_{label}")

    merged = _merge_generation_submit_parts(parts, kind=label)
    logger.info(
        "Playwright UI: submit %s — %s response(s), urls=%s media_ids=%s (variant_count=%s)",
        label,
        len(parts),
        len(merged.get("image_urls") or []),
        len(merged.get("media_ids") or []),
        want,
    )
    return merged


async def _submit_prompt_and_wait_image(
    page: Any,
    *,
    timeout_s: int | None = None,
    variant_count: int = 1,
    profile_id: str = "",
) -> dict[str, Any]:
    """
    Bấm mũi tên gửi và chờ network response ảnh (batchGenerateImages).
    Trả urls/media_ids giống luồng flow_sdk.gen_image hiện tại.
    """
    submit = await _submit_arrow_button(page)
    if submit is None:
        raise RuntimeError("submit_arrow_not_found")

    try:
        out = await _collect_submit_responses(
            page,
            submit=submit,
            matcher=_is_image_generate_response,
            normalize=_normalize_image_submit_payload,
            variant_count=variant_count,
            timeout_s=timeout_s,
            label="image",
            profile_id=profile_id,
        )
        logger.info(
            "Playwright UI: submit image done status=%s urls=%s media_ids=%s",
            out.get("status"),
            len(out.get("image_urls") or []),
            len(out.get("media_ids") or []),
        )
        return out
    except FlowHttp403Error:
        raise
    except UiFlowRestartFromUpload:
        raise
    except Exception as exc:
        await _reraise_as_upload_restart(page, exc, context="submit_image")


async def _submit_prompt_and_wait_video(
    page: Any,
    *,
    timeout_s: int | None = None,
    variant_count: int = 1,
    profile_id: str = "",
) -> dict[str, Any]:
    """Bấm submit video và lấy media_ids từ response submit."""
    submit = await _submit_arrow_button(page)
    if submit is None:
        raise RuntimeError("submit_arrow_not_found")

    try:
        out = await _collect_submit_responses(
            page,
            submit=submit,
            matcher=_is_video_generate_response,
            normalize=_normalize_video_submit_payload,
            variant_count=variant_count,
            timeout_s=timeout_s,
            label="video",
            profile_id=profile_id,
        )
        logger.info(
            "Playwright UI: submit video done status=%s media_ids=%s",
            out.get("status"),
            len(out.get("media_ids") or []),
        )
        return out
    except FlowHttp403Error:
        raise
    except UiFlowRestartFromUpload:
        raise
    except Exception as exc:
        await _reraise_as_upload_restart(page, exc, context="submit_video")


async def prepare_request_on_flow_ui(
    *,
    profile_id: str,
    request_id: str,
    request_type: str,
    prompt: str,
    project_id: str = "",
    image_base64s: list[str] | None = None,
    uploaded_media_ids: list[str] | None = None,
    aspect_ratio: str = "16:9",
    image_model: str = "",
    video_quality: str = "lite_relaxed",
    video_mode: str = "frame",
    video_duration_s: Any = 8,
    variant_count: int = 1,
    proxy_url: str | None = None,
) -> dict[str, Any]:
    """
    Automation: cấu hình UI + upload ảnh qua menu Tải nội dung nghe nhìn lên + điền prompt.
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

    opened_new_project = False
    upload_meta: dict[str, Any] | list[dict[str, Any]] | None = None
    image_settings_meta: dict[str, Any] | None = None
    video_settings_meta: dict[str, Any] | None = None
    ui_generation_meta: dict[str, Any] | None = None
    generation_kind = _normalize_generation_kind(request_type)
    imgs = [x for x in (image_base64s or []) if x]
    variant_count = max(1, min(4, int(variant_count or 1)))
    media_ids: list[str] = []
    prompt_filled = False

    opened_new_project = await ensure_flow_editor_page(page)
    await _ui_pause(0.25, "sau vào editor Flow")

    # Thẻ lỗi từ job trước — dọn nhanh, không chờ response cũ.
    if await _has_generation_error_card(page):
        logger.info("Playwright UI: dọn thẻ lỗi cũ trước khi chạy pipeline")
        await _prepare_restart_from_upload(page)

    async def step_settings() -> None:
        nonlocal image_settings_meta, video_settings_meta
        if generation_kind == "image":
            image_settings_meta = await configure_image_generation_ui(
                page,
                aspect_ratio=aspect_ratio,
                image_model=image_model,
                variant_count=variant_count,
            )
            if not image_settings_meta.get("configured"):
                raise RuntimeError(
                    "image_settings_not_configured "
                    f"ratio={image_settings_meta.get('aspect_ratio_selected')} "
                    f"model={image_settings_meta.get('image_model_selected')} "
                    f"dismissed={image_settings_meta.get('panel_dismissed')}"
                )
            await _ui_pause(0.4, "sau cấu hình image settings")
            return
        vq = video_quality
        if str(vq or "").strip().lower() != "lite_relaxed":
            logger.info(
                "Playwright UI: video model %s chưa ưu tiên, tạm ép về lite_relaxed",
                vq,
            )
            vq = "lite_relaxed"
        video_settings_meta = await configure_video_generation_ui(
            page,
            aspect_ratio=aspect_ratio,
            video_quality=vq,
            video_mode=video_mode,
            video_duration_s=video_duration_s,
            variant_count=variant_count,
        )
        if not video_settings_meta.get("configured"):
            raise RuntimeError(
                "video_settings_not_configured "
                f"kind={video_settings_meta.get('kind_selected')} "
                f"mode={video_settings_meta.get('video_mode_selected')} "
                f"ratio={video_settings_meta.get('aspect_ratio_selected')} "
                f"variant={video_settings_meta.get('variant_selected')} "
                f"duration={video_settings_meta.get('duration_selected')} "
                f"model={video_settings_meta.get('video_model_selected')} "
                f"dismissed={video_settings_meta.get('panel_dismissed')} "
                f"frame_slots={video_settings_meta.get('frame_slots_visible')}"
            )
        await _ui_pause(0.4, "sau cấu hình video settings")

    async def step_upload() -> None:
        nonlocal upload_meta, media_ids, ui_generation_meta, prompt_filled
        upload_meta = await attach_images_via_ui(
            page,
            image_base64s=imgs,
            generation_kind=generation_kind,
            video_mode=str(video_mode or "frame"),
            video_duration_s=video_duration_s,
            video_settings_configured=bool(
                video_settings_meta and video_settings_meta.get("configured")
            ),
            variant_count=variant_count,
        )
        media_ids = [
            str(r.get("media_id") or "").strip()
            for r in upload_meta
            if isinstance(r, dict) and str(r.get("media_id") or "").strip()
        ]
        ui_generation_meta = None
        prompt_filled = False
        logger.info(
            "Playwright UI: đã upload %s ảnh qua UI (Tải nội dung nghe nhìn lên)",
            len(upload_meta),
        )

    async def step_prompt() -> None:
        nonlocal prompt_filled
        await fill_prompt(page, prompt)
        prompt_filled = bool(str(prompt or "").strip())

    async def step_submit() -> None:
        nonlocal ui_generation_meta
        if str(request_type or "").strip().lower() == "gen_image" and is_ui_prep_only():
            ui_generation_meta = await _submit_prompt_and_wait_image(
                page, variant_count=variant_count, profile_id=profile_id
            )
        elif generation_kind == "video" and is_ui_prep_only():
            ui_generation_meta = await _submit_prompt_and_wait_video(
                page, variant_count=variant_count, profile_id=profile_id
            )
        if ui_generation_meta and is_ui_prep_only():
            settled = await _settle_variant_grid_with_retry(
                page,
                generation_kind=generation_kind,
                variant_count=variant_count,
                existing=ui_generation_meta,
                reason="after_submit",
            )
            if settled:
                ui_generation_meta = settled

    pipeline: list[tuple[str, Any]] = [
        ("settings", step_settings),
    ]
    if imgs:
        pipeline.append(("upload", step_upload))
    pipeline.append(("prompt", step_prompt))
    if is_ui_prep_only() and (
        str(request_type or "").strip().lower() == "gen_image" or generation_kind == "video"
    ):
        pipeline.append(("submit", step_submit))

    await _run_ui_step_pipeline(page, pipeline, label=f"rid={request_id[:8]}")

    if not imgs and str(prompt or "").strip():
        logger.info("Playwright UI: không có ảnh — chỉ điền prompt (không bấm [+])")

    return {
        "ui_prep": True,
        "profile_id": profile_id,
        "request_id": request_id,
        "opened_new_project": opened_new_project,
        "prompt_filled": prompt_filled,
        "image_uploaded": bool(media_ids),
        "uploaded_media_ids": media_ids,
        "upload": upload_meta,
        "image_settings": image_settings_meta,
        "video_settings": video_settings_meta,
        "generation_kind": generation_kind,
        "ui_generated": bool(ui_generation_meta and ui_generation_meta.get("submitted")),
        "ui_generation": ui_generation_meta,
        "page_url": page.url,
        "prep_only": is_ui_prep_only(),
        "proxy": proxy_info.get("proxy_display") or proxy_info.get("proxy") or "",
        "proxy_attached": bool(proxy_info.get("proxy_attached")),
        "proxy_assigned": proxy_info.get("proxy_assigned") or "",
    }
