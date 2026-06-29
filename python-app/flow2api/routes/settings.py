"""Dashboard settings: Telegram, proxy, system control."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from flow2api.services import system_ops

router = APIRouter(prefix="/api/settings", tags=["settings"])


class TelegramBody(BaseModel):
    bot_token: str | None = None
    chat_id: str | None = None
    enabled: bool | None = None


class ProxyBody(BaseModel):
    proxies: str = ""


class AutostartBody(BaseModel):
    enabled: bool


@router.get("/config")
async def get_config():
    return system_ops.public_config()


@router.post("/telegram")
async def save_telegram(body: TelegramBody):
    cfg = system_ops.load_config()
    tg = dict(cfg.get("telegram") or {})
    data = body.model_dump(exclude_none=True)
    if data.get("bot_token") in ("", "…", None) and "bot_token" in data:
        data.pop("bot_token", None)
    tg.update(data)
    saved = system_ops.save_config({"telegram": tg})
    return {"ok": True, "telegram": system_ops.public_config().get("telegram")}


@router.post("/telegram/test")
async def test_telegram():
    ok = system_ops.telegram_send("✅ Flow2API Telegram đã kết nối.")
    return {"ok": ok, "message": "Đã gửi tin nhắn test" if ok else "Gửi thất bại — kiểm tra token/chat id"}


@router.post("/proxy")
async def save_proxy(body: ProxyBody):
    pool = system_ops.parse_proxy_pool(body.proxies)
    saved = system_ops.save_config({"proxy_pool": pool})
    await system_ops.push_proxy_to_extensions()
    return {"ok": True, "count": len(pool), "proxy_pool": saved.get("proxy_pool")}


@router.get("/autostart")
async def get_autostart():
    return system_ops.get_windows_autostart()


@router.post("/autostart")
async def set_autostart(body: AutostartBody):
    return system_ops.set_windows_autostart(body.enabled)


@router.get("/profiles")
async def list_profiles():
    return {"profiles": system_ops.list_chrome_profiles()}


@router.post("/system/force-refresh")
async def system_force_refresh():
    return await system_ops.force_refresh_all()


@router.post("/system/launch-profiles")
async def system_launch_profiles():
    return system_ops.launch_all_profiles()


@router.post("/system/close-chrome")
async def system_close_chrome():
    return system_ops.close_all_chrome()
