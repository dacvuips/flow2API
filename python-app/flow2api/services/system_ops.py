"""Windows ops: autostart, Chrome profiles, Telegram, proxy pool."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from flow2api.config import APP_ROOT, STORAGE_DIR

logger = logging.getLogger(__name__)

_CONFIG_PATH = STORAGE_DIR / "system_config.json"
_SCRIPTS_DIR = APP_ROOT / "scripts"
_REPO_ROOT = APP_ROOT.parent
_FLOW_URL_DEFAULT = "https://labs.google/fx/vi/tools/flow"
_STARTUP_BAT_NAME = "Flow2API-Autostart.bat"

_LOCK = threading.Lock()
_telegram_offset = 0

DEFAULT_CONFIG: dict[str, Any] = {
    "flow_url": _FLOW_URL_DEFAULT,
    "telegram": {
        "bot_token": "",
        "chat_id": "",
        "enabled": False,
    },
    "proxy_pool": [],
    "proxy_pool_enabled": False,
    "proxy_rotate_enabled": False,
    "proxy_rotate_interval_min": 30,
    "proxy_rotate_offset": 0,
    "proxy_rotate_last_at": 0,
    "windows_autostart": True,
}


def _ensure_storage() -> None:
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    _SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict[str, Any]:
    _ensure_storage()
    with _LOCK:
        if not _CONFIG_PATH.is_file():
            return json.loads(json.dumps(DEFAULT_CONFIG))
        try:
            raw = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            out = json.loads(json.dumps(DEFAULT_CONFIG))
            if isinstance(raw, dict):
                out.update({k: v for k, v in raw.items() if k in DEFAULT_CONFIG})
                if isinstance(raw.get("telegram"), dict):
                    out["telegram"] = {**out["telegram"], **raw["telegram"]}
                if "proxy_pool_enabled" not in raw:
                    out["proxy_pool_enabled"] = bool(raw.get("proxy_pool"))
            return out
        except Exception as exc:
            logger.warning("system_config load failed: %s", exc)
            return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(cfg: dict[str, Any]) -> dict[str, Any]:
    _ensure_storage()
    current = load_config()
    for key in (
        "flow_url",
        "windows_autostart",
        "proxy_pool",
        "proxy_pool_enabled",
        "proxy_rotate_enabled",
        "proxy_rotate_interval_min",
        "proxy_rotate_offset",
        "proxy_rotate_last_at",
    ):
        if key in cfg:
            current[key] = cfg[key]
    if isinstance(cfg.get("telegram"), dict):
        current["telegram"] = {**current.get("telegram", {}), **cfg["telegram"]}
    with _LOCK:
        _CONFIG_PATH.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
    return current


def public_config() -> dict[str, Any]:
    cfg = load_config()
    tg = dict(cfg.get("telegram") or {})
    if tg.get("bot_token"):
        tg["bot_token"] = tg["bot_token"][:8] + "…"
    return {
        "flow_url": cfg.get("flow_url") or _FLOW_URL_DEFAULT,
        "telegram": tg,
        "proxy_pool": list(cfg.get("proxy_pool") or []),
        "proxy_pool_enabled": is_proxy_pool_enabled(cfg),
        "proxy_rotate_enabled": bool(cfg.get("proxy_rotate_enabled")),
        "proxy_rotate_interval_min": max(1, int(cfg.get("proxy_rotate_interval_min") or 30)),
        "proxy_rotate_last_at": float(cfg.get("proxy_rotate_last_at") or 0),
        "windows_autostart": bool(cfg.get("windows_autostart")),
        "autostart_installed": _startup_bat_path().is_file(),
    }


def _startup_dir() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _startup_bat_path() -> Path:
    return _startup_dir() / _STARTUP_BAT_NAME


def _launch_bat_path() -> Path:
    return _SCRIPTS_DIR / "Launch-All-Profiles.bat"


def _chrome_paths() -> list[Path]:
    candidates = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    ]
    return [p for p in candidates if p.is_file()]


def _user_data_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"


def list_chrome_profiles() -> list[str]:
    root = _user_data_dir()
    found: list[str] = []
    if (root / "Default" / "Preferences").is_file():
        found.append("Default")
    if root.is_dir():
        for entry in sorted(root.glob("Profile *")):
            if (entry / "Preferences").is_file():
                found.append(entry.name)
    return found


def ensure_launch_script() -> Path:
    _ensure_storage()
    cfg = load_config()
    flow_url = str(cfg.get("flow_url") or _FLOW_URL_DEFAULT).replace('"', "")
    bat = _launch_bat_path()
    content = f"""@echo off
setlocal enabledelayedexpansion
title Flow2API — Launch Chrome Profiles

set "CHROME_PATH=C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
if not exist "%CHROME_PATH%" set "CHROME_PATH=C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe"
if not exist "%CHROME_PATH%" set "CHROME_PATH=%LOCALAPPDATA%\\Google\\Chrome\\Application\\chrome.exe"
if not exist "%CHROME_PATH%" (
  echo Chrome not found
  exit /b 1
)

set "USER_DATA=%LOCALAPPDATA%\\Google\\Chrome\\User Data"
set "FLOW_URL={flow_url}"

if exist "%USER_DATA%\\Default\\Preferences" (
  echo Opening Default
  start "" "%CHROME_PATH%" --profile-directory="Default" --hide-crash-restore-bubble --disable-session-crashed-bubble "%FLOW_URL%"
  ping 127.0.0.1 -n 3 > nul
)

for /D %%D in ("%USER_DATA%\\Profile *") do (
  if exist "%%D\\Preferences" (
    set "prof=%%~nxD"
    echo Opening !prof!
    start "" "%CHROME_PATH%" --profile-directory="!prof!" --hide-crash-restore-bubble --disable-session-crashed-bubble "%FLOW_URL%"
    ping 127.0.0.1 -n 3 > nul
  )
)
echo Done.
"""
    bat.write_text(content, encoding="utf-8", newline="\r\n")
    return bat


def launch_all_profiles() -> dict[str, Any]:
    bat = ensure_launch_script()
    profiles = list_chrome_profiles()
    if not profiles:
        return {"ok": False, "error": "no_chrome_profiles", "message": "Không tìm thấy Chrome profile nào"}
    if not _chrome_paths():
        return {"ok": False, "error": "chrome_not_found", "message": "Không tìm thấy chrome.exe"}
    subprocess.Popen(
        ["cmd", "/c", str(bat)],
        cwd=str(_SCRIPTS_DIR),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return {
        "ok": True,
        "message": f"Đã kích hoạt {len(profiles)} Chrome profile → Flow",
        "profiles": profiles,
    }


def close_all_chrome() -> dict[str, Any]:
    subprocess.run(
        ["taskkill", "/F", "/IM", "chrome.exe", "/T"],
        capture_output=True,
        text=True,
    )
    return {"ok": True, "message": "Đã đóng toàn bộ Chrome"}


def set_windows_autostart(enabled: bool) -> dict[str, Any]:
    cfg = save_config({"windows_autostart": enabled})
    startup = _startup_bat_path()
    if not enabled:
        if startup.is_file():
            startup.unlink(missing_ok=True)
        return {"ok": True, "enabled": False, "message": "Đã TẮT khởi động cùng Windows"}
    ensure_launch_script()
    run_bat = APP_ROOT / "run.bat"
    agent_bat = f'@echo off\r\nstart "" /min cmd /c "cd /d "{APP_ROOT}" && run.bat"\r\nping 127.0.0.1 -n 18 > nul\r\ncall "{_launch_bat_path()}"\r\n'
    startup.parent.mkdir(parents=True, exist_ok=True)
    startup.write_text(agent_bat, encoding="utf-8", newline="\r\n")
    save_config({"windows_autostart": True})
    return {"ok": True, "enabled": True, "message": "Đã BẬT khởi động cùng Windows (agent + mở profile)"}


def get_windows_autostart() -> dict[str, Any]:
    cfg = load_config()
    return {
        "enabled": bool(cfg.get("windows_autostart")) and _startup_bat_path().is_file(),
        "installed": _startup_bat_path().is_file(),
    }


def is_proxy_pool_enabled(cfg: dict[str, Any] | None = None) -> bool:
    cfg = cfg if cfg is not None else load_config()
    return bool(cfg.get("proxy_pool_enabled"))


def parse_proxy_pool(text: str) -> list[str]:
    lines = []
    for line in (text or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


def next_proxy_for_profile(profile_index: int = 0) -> str:
    return _proxy_url_for_index(load_config(), profile_index)


def _proxy_url_for_index(cfg: dict[str, Any], profile_index: int) -> str:
    if not is_proxy_pool_enabled(cfg):
        return ""
    pool = cfg.get("proxy_pool") or []
    if not pool:
        return ""
    offset = int(cfg.get("proxy_rotate_offset") or 0)
    idx = (profile_index + offset) % len(pool)
    return str(pool[idx])


def proxy_rotate_interval_seconds(cfg: dict[str, Any] | None = None) -> int:
    cfg = cfg if cfg is not None else load_config()
    return max(60, int(cfg.get("proxy_rotate_interval_min") or 30) * 60)


async def maybe_rotate_proxies() -> bool:
    cfg = load_config()
    if not is_proxy_pool_enabled(cfg) or not cfg.get("proxy_rotate_enabled"):
        return False
    pool = cfg.get("proxy_pool") or []
    if len(pool) < 2:
        return False
    interval_s = proxy_rotate_interval_seconds(cfg)
    now = time.time()
    last_at = float(cfg.get("proxy_rotate_last_at") or 0)
    if last_at <= 0:
        save_config({"proxy_rotate_last_at": now})
        return False
    if now - last_at < interval_s:
        return False
    new_offset = int(cfg.get("proxy_rotate_offset") or 0) + 1
    save_config({"proxy_rotate_offset": new_offset, "proxy_rotate_last_at": now})
    await push_proxy_to_extensions()
    logger.info(
        "proxy rotated offset=%s interval=%sm profiles=%s",
        new_offset,
        int(cfg.get("proxy_rotate_interval_min") or 30),
        len(pool),
    )
    return True


async def proxy_rotate_loop() -> None:
    while True:
        try:
            await maybe_rotate_proxies()
        except Exception as exc:
            logger.warning("proxy rotate tick failed: %s", exc)
        await asyncio.sleep(30)


def _profile_proxy_index(profile_id: str) -> int:
    from flow2api.services.extension_pool import get_extension_pool

    idx = 0
    for session in get_extension_pool().list_sessions():
        if session.profile_id.startswith("_"):
            continue
        if session.profile_id == profile_id:
            return idx
        idx += 1
    return 0


async def broadcast_system(payload: dict) -> None:
    from flow2api.services.extension_pool import get_extension_pool

    await get_extension_pool().broadcast(payload)


async def force_refresh_all() -> dict[str, Any]:
    await broadcast_system({"type": "system_force_refresh"})
    return {"ok": True, "message": "Đã gửi lệnh F5 toàn bộ tab Flow"}


async def push_proxy_to_session(session: Any, profile_index: int | None = None) -> None:
    cfg = load_config()
    idx = profile_index if profile_index is not None else _profile_proxy_index(session.profile_id)
    proxy_url = _proxy_url_for_index(cfg, idx)
    try:
        await session.send_json({"type": "system_set_proxy", "proxyUrl": proxy_url})
    except Exception as exc:
        logger.warning("proxy push failed %s: %s", session.profile_id[:8], exc)


async def push_proxy_to_extensions() -> None:
    idx = 0
    from flow2api.services.extension_pool import get_extension_pool

    for session in get_extension_pool().list_sessions():
        if session.profile_id.startswith("_"):
            continue
        await push_proxy_to_session(session, idx)
        idx += 1


def _extension_push_config() -> dict[str, Any]:
    cfg = load_config()
    return {
        "flowUrl": cfg.get("flow_url") or _FLOW_URL_DEFAULT,
    }


def telegram_send(text: str) -> bool:
    cfg = load_config()
    tg = cfg.get("telegram") or {}
    token = str(tg.get("bot_token") or "").strip()
    chat_id = str(tg.get("chat_id") or "").strip()
    if not token or not chat_id:
        return False
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        body = urlencode({"chat_id": chat_id, "text": text[:4000]}).encode("utf-8")
        req = Request(url, data=body, method="POST")
        with urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception as exc:
        logger.warning("telegram send failed: %s", exc)
        return False


def telegram_get_updates() -> list[dict]:
    global _telegram_offset
    cfg = load_config()
    token = str((cfg.get("telegram") or {}).get("bot_token") or "").strip()
    if not token:
        return []
    try:
        qs = urlencode({"offset": _telegram_offset, "timeout": 0})
        url = f"https://api.telegram.org/bot{token}/getUpdates?{qs}"
        with urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        results = data.get("result") or []
        if results:
            _telegram_offset = int(results[-1].get("update_id", 0)) + 1
        return results
    except Exception:
        return []


async def _handle_telegram_command(text: str) -> None:
    cmd = (text or "").strip().split()[0].lower()
    if cmd in ("/status", "/status@flow2api_bot"):
        from flow2api.services.extension_pool import get_extension_pool

        pool = get_extension_pool()
        online = sum(1 for s in pool.list_sessions() if s.is_ready())
        telegram_send(f"Flow2API\nOnline profiles: {online}")
    elif cmd in ("/launch",):
        r = launch_all_profiles()
        telegram_send(r.get("message") or "launch")
    elif cmd in ("/kill",):
        r = close_all_chrome()
        telegram_send(r.get("message") or "kill")
    elif cmd in ("/refresh", "/restart"):
        await force_refresh_all()
        telegram_send("🔄 Đã force refresh tab Flow")


async def telegram_poll_loop() -> None:
    while True:
        try:
            cfg = load_config()
            tg = cfg.get("telegram") or {}
            if tg.get("enabled") and tg.get("bot_token") and tg.get("chat_id"):
                allowed = str(tg.get("chat_id"))
                for upd in telegram_get_updates():
                    msg = upd.get("message") or {}
                    chat = str((msg.get("chat") or {}).get("id") or "")
                    if chat != allowed:
                        continue
                    text = msg.get("text") or ""
                    if text.startswith("/"):
                        await _handle_telegram_command(text)
        except Exception as exc:
            logger.debug("telegram poll: %s", exc)
        await asyncio.sleep(3)
