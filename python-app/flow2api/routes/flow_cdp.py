"""Flow CDP slots API — parallel to extension Bridge / Captcha Center."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from flow2api.config import HTTP_HANDLER_TIMEOUT_S
from flow2api.services import system_ops
from flow2api.services.api_auth import auth_key_id
from flow2api.services.flow_cdp_control import (
    auto_attach_emails,
    clear_slot_data,
    click_selector,
    delete_slot_fully,
    flow_cdp_public_status,
    logout_flow,
    open_flow_login,
    schedule_auto_attach,
    sync_session,
)
from flow2api.services.flow_cdp_settings import (
    add_flow_cdp_slot,
    get_flow_cdp_settings,
    get_flow_cdp_slot,
    update_flow_cdp_slot,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/flow-cdp", tags=["flow-cdp"])


class SlotCreateBody(BaseModel):
    label: str | None = None
    port: int | None = None
    role: Literal["bridge", "center"] = "bridge"


class SlotUpdateBody(BaseModel):
    label: str | None = None
    port: int | None = None
    role: Literal["bridge", "center"] | None = None
    email: str | None = None


class ClearDataBody(BaseModel):
    wipe_disk: bool = False


class ClickBody(BaseModel):
    selector: str = Field(min_length=1)
    timeout_ms: int = Field(default=15_000, ge=1000, le=120_000)


def _profiles_payload() -> list[dict[str, Any]]:
    return flow_cdp_public_status()


@router.get("/slots")
async def flow_cdp_slots_list(
    auto_attach: bool = True,
    _: int = Depends(auth_key_id),
):
    # auto_attach chạy nền — tránh block list → Cloudflare 524
    if auto_attach:
        async def _bg_attach() -> None:
            try:
                await auto_attach_emails(only_missing=True)
            except Exception as exc:
                logger.debug("auto_attach on list failed: %s", exc)

        asyncio.create_task(_bg_attach())
    return {
        "ok": True,
        "slots": _profiles_payload(),
        "settings": get_flow_cdp_settings().to_dict(),
        "profiles": _profiles_payload(),
        "hint": (
            "Thêm CDP → Mở CDP → Login Google → tắt CDP. "
            "Chỉ lưu email + profile (Chrome user-data). "
            "Cookies/token lấy khi Sync session hoặc Auto generate."
        ),
    }


@router.post("/slots/auto-attach")
async def flow_cdp_auto_attach(
    force: bool = False,
    _: int = Depends(auth_key_id),
):
    result = await auto_attach_emails(only_missing=not force, force=force)
    return result


@router.post("/slots")
async def flow_cdp_slots_create(
    body: SlotCreateBody | None = None,
    _: int = Depends(auth_key_id),
):
    body = body or SlotCreateBody()
    try:
        slot = add_flow_cdp_slot(label=body.label, port=body.port, role=body.role)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # Tạo FlowProfile ngay — chưa lấy cookies; dùng sau khi login + automation
    try:
        from flow2api.services.flow_profile_service import ensure_profile_row

        ensure_profile_row(slot.profile_id(), profile_label=slot.label or slot.id)
    except Exception as exc:
        logger.warning("ensure profile on add CDP failed slot=%s: %s", slot.id, exc)
    # CDP mới = standby: tắt Nhận job. Chỉ «Chạy CDP / auto» mới bật.
    if (slot.role or "bridge") != "center":
        try:
            from flow2api.services.flow_cdp_auto import mark_cdp_profile_standby

            mark_cdp_profile_standby(slot.profile_id(), reason=f"add_slot:{slot.id}")
        except Exception as exc:
            logger.warning("standby on add CDP failed slot=%s: %s", slot.id, exc)
    return {
        "ok": True,
        "slot": slot.to_dict(),
        "profiles": _profiles_payload(),
        "message": (
            f"Đã thêm Flow CDP {slot.id} (:{slot.port}) · standby (chưa nhận job) · "
            "Mở CDP → login → Sync; bật nhận job qua «Chạy CDP tiếp theo» / Auto"
        ),
    }


@router.put("/slots/{slot_id}")
async def flow_cdp_slots_update(
    slot_id: str,
    body: SlotUpdateBody,
    _: int = Depends(auth_key_id),
):
    try:
        slot = update_flow_cdp_slot(
            slot_id,
            label=body.label,
            port=body.port,
            role=body.role,
            email=body.email,
        )
    except KeyError:
        raise HTTPException(404, "slot_not_found") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # Lưu tay email/@ vào FlowProfile (không sync cookies)
    email = str(body.email or "").strip()
    if not email and body.label and "@" in str(body.label):
        email = str(body.label).strip()
    if email and "@" in email:
        try:
            from flow2api.services.flow_cdp_control import _attach_email_to_slot

            _attach_email_to_slot(slot.id, email, profile_id=slot.profile_id())
            slot = get_flow_cdp_slot(slot_id) or slot
        except Exception as exc:
            logger.warning("attach email on slot update failed: %s", exc)
    return {"ok": True, "slot": slot.to_dict(), "profiles": _profiles_payload()}


@router.delete("/slots/{slot_id}")
async def flow_cdp_slots_delete(slot_id: str, _: int = Depends(auth_key_id)):
    """Xóa đúng nghĩa: đóng CDP, wipe disk, xóa DB profile, retire port/id."""
    try:
        result = await delete_slot_fully(slot_id)
    except KeyError:
        raise HTTPException(404, "slot_not_found") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not result.get("ok"):
        err = str(result.get("error") or "delete_failed")
        raise HTTPException(404 if err == "slot_not_found" else 400, err)
    return {**result, "profiles": _profiles_payload()}


@router.post("/slots/{slot_id}/launch")
async def flow_cdp_slots_launch(slot_id: str, _: int = Depends(auth_key_id)):
    result = system_ops.launch_flow_cdp_slot(slot_id)
    if not result.get("ok"):
        raise HTTPException(400, result.get("message") or result.get("error") or "launch_failed")
    await schedule_auto_attach(slot_id)
    return {**result, "profiles": _profiles_payload()}


@router.post("/slots/launch-all")
async def flow_cdp_slots_launch_all(_: int = Depends(auth_key_id)):
    result = system_ops.launch_all_flow_cdp_slots()
    from flow2api.services.flow_cdp_settings import list_flow_cdp_slots

    for slot in list_flow_cdp_slots():
        await schedule_auto_attach(slot.id)
    return {**result, "profiles": _profiles_payload()}


@router.post("/slots/{slot_id}/open-flow")
async def flow_cdp_open_flow(slot_id: str, _: int = Depends(auth_key_id)):
    try:
        result = await open_flow_login(slot_id)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        logger.exception("open-flow failed slot=%s", slot_id)
        raise HTTPException(500, str(exc)) from exc
    await schedule_auto_attach(slot_id)
    return {**result, "profiles": _profiles_payload()}


@router.post("/slots/{slot_id}/sync")
async def flow_cdp_sync(slot_id: str, _: int = Depends(auth_key_id)):
    # Cap dưới Cloudflare ~100s để trả JSON 503 thay vì CF 524 HTML
    timeout = max(20.0, min(80.0, float(HTTP_HANDLER_TIMEOUT_S or 25) * 3))
    try:
        result = await asyncio.wait_for(sync_session(slot_id), timeout=timeout)
    except asyncio.TimeoutError:
        raise HTTPException(
            503,
            "sync_timeout — CDP/Flow phản hồi chậm. Thử Sync qua http://127.0.0.1 "
            "hoặc đợi Flow load xong rồi Sync lại.",
        ) from None
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        logger.exception("sync failed slot=%s", slot_id)
        raise HTTPException(500, str(exc)) from exc
    return {**result, "profiles": _profiles_payload()}


@router.post("/slots/{slot_id}/logout")
async def flow_cdp_logout(slot_id: str, _: int = Depends(auth_key_id)):
    try:
        result = await logout_flow(slot_id)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        logger.exception("logout failed slot=%s", slot_id)
        raise HTTPException(500, str(exc)) from exc
    return {**result, "profiles": _profiles_payload()}


@router.post("/slots/{slot_id}/clear-data")
async def flow_cdp_clear_data(
    slot_id: str,
    body: ClearDataBody | None = None,
    _: int = Depends(auth_key_id),
):
    body = body or ClearDataBody()
    try:
        result = await clear_slot_data(slot_id, wipe_disk=body.wipe_disk)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        logger.exception("clear-data failed slot=%s", slot_id)
        raise HTTPException(500, str(exc)) from exc
    if not result.get("ok"):
        raise HTTPException(400, result.get("message") or result.get("error") or "clear_failed")
    return {**result, "profiles": _profiles_payload()}


@router.post("/slots/{slot_id}/click")
async def flow_cdp_click(
    slot_id: str,
    body: ClickBody,
    _: int = Depends(auth_key_id),
):
    try:
        result = await click_selector(slot_id, body.selector, timeout_ms=body.timeout_ms)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        logger.exception("click failed slot=%s", slot_id)
        raise HTTPException(500, str(exc)) from exc
    return {**result, "profiles": _profiles_payload()}


# ── Auto schedule (Cấu hình tự động) ─────────────────────────────────────────


class AutoSettingsBody(BaseModel):
    enabled: bool | None = None
    parallel_gen: int | None = None
    parallel_center: int | None = None
    min_active_hours: float | None = None
    sync_delay_s: float | None = None
    job_parallel: int | None = None
    flow_url: str | None = None
    slot_order: list[str] | None = None
    slot_enabled: dict[str, bool] | None = None


class AutoEnabledBody(BaseModel):
    enabled: bool = False


@router.get("/auto")
async def flow_cdp_auto_get(_: int = Depends(auth_key_id)):
    from flow2api.services.flow_cdp_auto import auto_status, ensure_scheduler

    ensure_scheduler()
    return auto_status()


@router.put("/auto")
async def flow_cdp_auto_put(body: AutoSettingsBody, _: int = Depends(auth_key_id)):
    from flow2api.services.flow_cdp_auto import save_settings_and_nudge

    fields = body.model_dump(exclude_none=True)
    return await save_settings_and_nudge(**fields)


@router.post("/auto/enabled")
async def flow_cdp_auto_enabled(body: AutoEnabledBody, _: int = Depends(auth_key_id)):
    from flow2api.services.flow_cdp_auto import set_enabled

    return await set_enabled(body.enabled)


@router.post("/auto/run/{slot_id}")
async def flow_cdp_auto_run_one(slot_id: str, _: int = Depends(auth_key_id)):
    from flow2api.services.flow_cdp_auto import run_one_now

    result = await run_one_now(slot_id)
    if not result.get("ok"):
        raise HTTPException(400, result.get("message") or result.get("error") or "run_failed")
    return result


@router.post("/auto/run-next")
async def flow_cdp_auto_run_next(_: int = Depends(auth_key_id)):
    """Chạy CDP Gen tiếp theo trên danh sách: mở CDP → Sync cookies → Nhận job."""
    from flow2api.services.flow_cdp_auto import run_next_now

    result = await run_next_now()
    if not result.get("ok"):
        raise HTTPException(400, result.get("message") or result.get("error") or "run_failed")
    return result
