"""Dashboard settings: Telegram, proxy, system control."""
from __future__ import annotations

import time

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from flow2api.services import system_ops
from flow2api.services.api_auth import auth_key_id

router = APIRouter(prefix="/api/settings", tags=["settings"])


class TelegramBody(BaseModel):
    bot_token: str | None = None
    chat_id: str | None = None
    enabled: bool | None = None


class ProxyBody(BaseModel):
    proxies: str = ""


class ProxyEnabledBody(BaseModel):
    enabled: bool


class ProxyRotateBody(BaseModel):
    enabled: bool | None = None
    interval_min: int | None = None


class AutostartBody(BaseModel):
    enabled: bool


class ChatgptSettingsBody(BaseModel):
    call_delay_s: float | None = None
    transport: str | None = None
    cdp_url: str | None = None
    chrome_profile: str | None = None
    chrome_user_data_dir: str | None = None
    use_system_chrome_profile: bool | None = None
    headless: bool | None = None


@router.get("/config")
async def get_config(_: int = Depends(auth_key_id)):
    return system_ops.public_config()


@router.post("/chatgpt")
async def save_chatgpt_settings(body: ChatgptSettingsBody, _: int = Depends(auth_key_id)):
    current = system_ops.chatgpt_config()
    patch: dict[str, Any] = {}
    if body.call_delay_s is not None:
        patch["call_delay_s"] = max(0.0, min(600.0, float(body.call_delay_s)))
    if body.transport is not None:
        t = str(body.transport).strip().lower()
        if t not in ("playwright", "extension"):
            raise HTTPException(400, "transport must be playwright or extension")
        patch["transport"] = t
    if body.cdp_url is not None:
        patch["cdp_url"] = str(body.cdp_url).strip()
    if body.chrome_profile is not None:
        patch["chrome_profile"] = str(body.chrome_profile).strip() or "Default"
    if body.chrome_user_data_dir is not None:
        patch["chrome_user_data_dir"] = str(body.chrome_user_data_dir).strip()
    if body.use_system_chrome_profile is not None:
        patch["use_system_chrome_profile"] = bool(body.use_system_chrome_profile)
    if body.headless is not None:
        patch["headless"] = bool(body.headless)
    if not patch:
        return {"ok": True, "chatgpt": current}
    # drop runtime-only list before save
    to_save = {k: v for k, v in {**current, **patch}.items() if k != "chrome_profiles"}
    system_ops.save_config({"chatgpt": to_save})
    return {"ok": True, "chatgpt": system_ops.chatgpt_config(), "message": "Đã lưu cấu hình Chat GPT"}


class LaunchPlaywrightChromeBody(BaseModel):
    profile: str | None = None
    port: int | None = 9222
    use_system_profile: bool | None = None


@router.post("/chatgpt/launch-chrome")
async def launch_chatgpt_chrome(body: LaunchPlaywrightChromeBody | None = None, _: int = Depends(auth_key_id)):
    """Open all Playwright CDP slots (or legacy single launch)."""
    body = body or LaunchPlaywrightChromeBody()
    from flow2api.services.chatgpt_playwright import reset_playwright_browser

    # Prefer multi-slot launch
    result = system_ops.launch_all_playwright_slots()
    await reset_playwright_browser()
    if result.get("launched"):
        return result
    # Fallback legacy single
    result = system_ops.launch_chrome_for_playwright(
        profile=body.profile,
        port=int(body.port or 9222),
        use_system_profile=body.use_system_profile,
    )
    await reset_playwright_browser()
    if not result.get("ok"):
        raise HTTPException(400, result.get("message") or result.get("error") or "chrome_launch_failed")
    return result


@router.post("/telegram")
async def save_telegram(body: TelegramBody, _: int = Depends(auth_key_id)):
    cfg = system_ops.load_config()
    tg = dict(cfg.get("telegram") or {})
    data = body.model_dump(exclude_none=True)
    if data.get("bot_token") in ("", "…", None) and "bot_token" in data:
        data.pop("bot_token", None)
    tg.update(data)
    saved = system_ops.save_config({"telegram": tg})
    return {"ok": True, "telegram": system_ops.public_config().get("telegram")}


@router.post("/telegram/test")
async def test_telegram(_: int = Depends(auth_key_id)):
    ok = system_ops.telegram_send("✅ Flow2API Telegram đã kết nối.")
    return {"ok": ok, "message": "Đã gửi tin nhắn test" if ok else "Gửi thất bại — kiểm tra token/chat id"}


@router.post("/proxy")
async def save_proxy(body: ProxyBody, _: int = Depends(auth_key_id)):
    pool = system_ops.parse_proxy_pool(body.proxies)
    saved = system_ops.save_config({"proxy_pool": pool})
    return {"ok": True, "count": len(pool), "proxy_pool": saved.get("proxy_pool")}


@router.post("/proxy/enabled")
async def set_proxy_enabled(body: ProxyEnabledBody, _: int = Depends(auth_key_id)):
    saved = system_ops.save_config({"proxy_pool_enabled": body.enabled})
    await system_ops.push_proxy_to_extensions()
    state = "bật gắn proxy vào profile" if body.enabled else "tắt gắn proxy (đã gỡ khỏi Chrome)"
    return {
        "ok": True,
        "proxy_pool_enabled": system_ops.is_proxy_pool_enabled(saved),
        "message": f"Đã {state}",
    }


@router.post("/proxy/rotate")
async def set_proxy_rotate(body: ProxyRotateBody, _: int = Depends(auth_key_id)):
    cfg = system_ops.load_config()
    patch: dict[str, Any] = {}
    if body.enabled is not None:
        was_enabled = bool(cfg.get("proxy_rotate_enabled"))
        patch["proxy_rotate_enabled"] = body.enabled
        if body.enabled and not was_enabled:
            patch["proxy_rotate_last_at"] = time.time()
    if body.interval_min is not None:
        patch["proxy_rotate_interval_min"] = max(1, min(1440, int(body.interval_min)))
    saved = system_ops.save_config(patch)
    pub = system_ops.public_config()
    if body.enabled:
        msg = f"Đã bật xoay proxy mỗi {pub['proxy_rotate_interval_min']} phút"
    elif body.enabled is False:
        msg = "Đã tắt xoay proxy"
    elif body.interval_min is not None:
        msg = f"Thời gian xoay: {pub['proxy_rotate_interval_min']} phút"
    else:
        msg = "Đã lưu cấu hình xoay proxy"
    return {
        "ok": True,
        "proxy_rotate_enabled": bool(saved.get("proxy_rotate_enabled")),
        "proxy_rotate_interval_min": pub["proxy_rotate_interval_min"],
        "message": msg,
    }


@router.post("/proxy/rotate-now")
async def rotate_proxy_now(_: int = Depends(auth_key_id)):
    from flow2api.services.dashboard_events import events
    from flow2api.services.extension_pool import get_extension_pool

    result = await system_ops.rotate_proxies_now()
    if not result.get("ok"):
        raise HTTPException(400, result.get("message") or result.get("error"))
    events.publish("profile_proxy_changed", {"manual": True})
    return {
        **result,
        "profiles": get_extension_pool().list_public(),
    }


@router.get("/autostart")
async def get_autostart(_: int = Depends(auth_key_id)):
    return system_ops.get_windows_autostart()


@router.post("/autostart")
async def set_autostart(body: AutostartBody, _: int = Depends(auth_key_id)):
    return system_ops.set_windows_autostart(body.enabled)


@router.get("/profiles")
async def list_profiles(_: int = Depends(auth_key_id)):
    return {"profiles": system_ops.list_chrome_profiles()}


@router.post("/system/force-refresh")
async def system_force_refresh(_: int = Depends(auth_key_id)):
    return await system_ops.force_refresh_all()


@router.post("/system/launch-profiles")
async def system_launch_profiles(_: int = Depends(auth_key_id)):
    return system_ops.launch_all_profiles()


@router.post("/system/close-chrome")
async def system_close_chrome(_: int = Depends(auth_key_id)):
    return system_ops.close_all_chrome()
