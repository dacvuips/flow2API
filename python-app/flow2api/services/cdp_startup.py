"""On agent restart: keep Chrome CDPs running and re-attach to those still open."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def reconnect_open_cdps() -> dict[str, Any]:
    """
    Không mở Chrome mới — chỉ kết nối lại các CDP đã sống:
    - Flow Gen / Captcha: probe + auto-attach email nếu thiếu
    - ChatGPT Playwright: warm connect_over_cdp
    Extension Captcha tự reconnect WS (~5s) khi agent lên lại.
    """
    from flow2api.services import system_ops

    flow_alive: list[str] = []
    flow_dead: list[str] = []
    flow_attach: dict[str, Any] = {}
    cgpt_alive: list[str] = []
    cgpt_dead: list[str] = []
    cgpt_connected: list[str] = []
    cgpt_errors: list[dict[str, str]] = []

    # ── Flow CDP (Gen + Captcha) ──────────────────────────────────────────
    try:
        from flow2api.services.flow_cdp_control import auto_attach_emails
        from flow2api.services.flow_cdp_settings import list_flow_cdp_slots

        for slot in list_flow_cdp_slots():
            cdp = slot.cdp_url()
            if system_ops.cdp_endpoint_alive(cdp):
                flow_alive.append(slot.id)
            else:
                flow_dead.append(slot.id)

        if flow_alive:
            try:
                flow_attach = await auto_attach_emails(only_missing=True)
            except Exception as exc:
                logger.warning("startup Flow auto_attach failed: %s", exc)
                flow_attach = {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.warning("startup Flow CDP probe failed: %s", exc)

    # ── ChatGPT Playwright CDP ────────────────────────────────────────────
    try:
        from flow2api.services.chatgpt_playwright import (
            _ensure_slot_page,
            _get_slot_runtime,
        )
        from flow2api.services.chatgpt_pool_settings import list_playwright_slots

        for slot in list_playwright_slots():
            cdp = slot.cdp_url()
            if not system_ops.cdp_endpoint_alive(cdp):
                cgpt_dead.append(slot.id)
                continue
            cgpt_alive.append(slot.id)
            try:
                rt = _get_slot_runtime(slot.id)
                async with rt.lock:
                    await _ensure_slot_page(
                        rt,
                        cdp_url=cdp,
                        user_data_dir=slot.user_data_dir(),
                    )
                cgpt_connected.append(slot.id)
            except Exception as exc:
                logger.warning(
                    "startup ChatGPT CDP reconnect failed slot=%s: %s",
                    slot.id,
                    exc,
                )
                cgpt_errors.append({"slot_id": slot.id, "error": str(exc)})
    except Exception as exc:
        logger.warning("startup ChatGPT CDP probe failed: %s", exc)

    # ── Auto CDP scheduler (nếu đang bật) ─────────────────────────────────
    try:
        from flow2api.services.flow_cdp_auto import ensure_scheduler

        # ensure_scheduler: start loop + bù ngay theo Song song Gen CDP
        ensure_scheduler()
    except Exception as exc:
        logger.debug("ensure_scheduler on startup: %s", exc)

    summary = {
        "ok": True,
        "flow_alive": flow_alive,
        "flow_dead": flow_dead,
        "flow_attach": {
            "attached": len((flow_attach or {}).get("attached") or []),
            "errors": len((flow_attach or {}).get("errors") or []),
        },
        "chatgpt_alive": cgpt_alive,
        "chatgpt_dead": cgpt_dead,
        "chatgpt_connected": cgpt_connected,
        "chatgpt_errors": cgpt_errors,
    }

    parts: list[str] = []
    if flow_alive:
        parts.append(f"Flow CDP sống: {', '.join(flow_alive)}")
    if cgpt_connected:
        parts.append(f"ChatGPT CDP gắn lại: {', '.join(cgpt_connected)}")
    if flow_dead:
        parts.append(f"Flow CDP tắt: {', '.join(flow_dead)}")
    if cgpt_dead:
        parts.append(f"ChatGPT CDP tắt: {', '.join(cgpt_dead)}")
    if not parts:
        parts.append("không có CDP nào đang mở")

    logger.info("startup reconnect CDP — %s", " · ".join(parts))
    return summary
