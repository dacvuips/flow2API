"""Windows ops: autostart, Chrome profiles, Telegram, proxy pool."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from flow2api.config import APP_ROOT, CDP_USER_DATA_DIR, CHROME_CDP_START_MINIMIZED, STORAGE_DIR

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
    "profile_proxy_disabled": [],
    "windows_autostart": True,
    "playwright_flow_chrome_profile": "Default",
    "playwright_flow_cdp_port": 9236,
    "playwright_flow_email": "",
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
        "profile_proxy_disabled",
        "playwright_flow_chrome_profile",
        "playwright_flow_cdp_port",
        "playwright_flow_email",
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
        "proxy_rotate_offset": int(cfg.get("proxy_rotate_offset") or 0),
        "proxy_rotate_status": proxy_rotate_status(cfg),
        "windows_autostart": bool(cfg.get("windows_autostart")),
        "autostart_installed": _startup_bat_path().is_file(),
        "playwright_flow_chrome_profile": get_playwright_flow_chrome_profile(),
        "playwright_flow_cdp_port": get_playwright_flow_cdp_port(),
        "playwright_flow_email": get_playwright_flow_email(),
        "playwright_profile_map": list_playwright_profile_map(),
    }


def _startup_dir() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _startup_bat_path() -> Path:
    return _startup_dir() / _STARTUP_BAT_NAME


def _launch_bat_path() -> Path:
    return _SCRIPTS_DIR / "Launch-All-Profiles.bat"


def _chrome_cdp_shared_flags() -> list[str]:
    """Flag Chrome CDP dùng chung — không gồm user-data-dir / remote-debugging-port."""
    flags = [
        "--remote-debugging-address=127.0.0.1",
        "--remote-allow-origins=*",
        "--hide-crash-restore-bubble",
        "--disable-session-crashed-bubble",
        "--disable-restore-session-state",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if CHROME_CDP_START_MINIMIZED:
        flags.append("--start-minimized")
    return flags


def _chrome_windows_start_cmd() -> str:
    return "start /min" if CHROME_CDP_START_MINIMIZED else "start"


def _chrome_paths() -> list[Path]:
    candidates = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    ]
    return [p for p in candidates if p.is_file()]


def _user_data_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"


def _cdp_user_data_dir() -> Path:
    """Non-standard Chrome user-data-dir (bắt buộc từ Chrome 136+ để CDP hoạt động)."""
    return Path(CDP_USER_DATA_DIR)


def _mirror_dir_robocopy(src: Path, dst: Path, *, exclude_cache: bool = True) -> None:
    if not src.is_dir():
        raise RuntimeError(f"mirror_src_missing:{src}")
    dst.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        shutil.copytree(src, dst, dirs_exist_ok=True)
        return
    cmd = [
        "robocopy",
        str(src),
        str(dst),
        "/MIR",
        "/R:2",
        "/W:2",
        "/NFL",
        "/NDL",
        "/NJH",
        "/NJS",
        "/NC",
        "/NS",
    ]
    if exclude_cache:
        cmd.extend(["/XD", "Cache", "Code Cache", "GPUCache", "Service Worker"])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode >= 8:
        raise RuntimeError(f"robocopy_failed:{result.returncode}")


def _cdp_profile_ready(chrome_dir: str) -> bool:
    dst_root = _cdp_user_data_dir()
    return (
        (dst_root / "Local State").is_file()
        and (dst_root / chrome_dir / "Preferences").is_file()
    )


def sync_chrome_profile_for_cdp(chrome_dir: str, *, force: bool = False) -> Path:
    """
    Đồng bộ profile Chrome sang thư mục user-data CDP riêng.
    Chrome 136+ bỏ qua --remote-debugging-port khi dùng User Data mặc định.

    Lưu ý: cookie/session Google mã hóa theo user-data-dir — bản sao không
    dùng được session Chrome desktop. Chỉ sync lần đầu (hoặc force); sau đó
    giữ profile CDP để không phải đăng nhập lại mỗi lần mở.
    """
    chrome_dir = str(chrome_dir or "").strip()
    if not chrome_dir:
        raise RuntimeError("profile_required")
    dst_root = _cdp_user_data_dir()
    if not force and _cdp_profile_ready(chrome_dir):
        logger.info("CDP profile %s đã có tại %s — bỏ qua sync", chrome_dir, dst_root)
        return dst_root

    src_root = _user_data_dir()
    src_prof = src_root / chrome_dir
    if not (src_prof / "Preferences").is_file():
        raise RuntimeError(f"profile_not_found:{chrome_dir}")

    dst_root.mkdir(parents=True, exist_ok=True)
    dst_ls = dst_root / "Local State"
    if not dst_ls.is_file():
        src_ls = src_root / "Local State"
        if src_ls.is_file():
            shutil.copy2(src_ls, dst_ls)
    logger.info("Bootstrap CDP profile %s -> %s (force=%s)", chrome_dir, dst_root, force)
    _mirror_dir_robocopy(src_prof, dst_root / chrome_dir)
    return dst_root


def ensure_cdp_profile_ready(chrome_dir: str, *, force: bool = False) -> tuple[Path, bool]:
    """Chuẩn bị user-data CDP. Trả về (đường dẫn, đã_sync)."""
    if force or not _cdp_profile_ready(chrome_dir):
        return sync_chrome_profile_for_cdp(chrome_dir, force=force), True
    return _cdp_user_data_dir(), False


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


def _chrome_profile_sort_key(name: str) -> tuple[int, int | str]:
    if name == "Default":
        return (0, 0)
    if name.startswith("Profile "):
        try:
            return (1, int(name.split(" ", 1)[1]))
        except ValueError:
            pass
    return (2, name)


def sorted_chrome_profiles() -> list[str]:
    return sorted(list_chrome_profiles(), key=_chrome_profile_sort_key)


def read_chrome_profile_email(chrome_dir: str) -> str:
    prefs_path = _user_data_dir() / chrome_dir / "Preferences"
    if not prefs_path.is_file():
        return ""
    try:
        raw = json.loads(prefs_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(raw, dict):
        return ""
    account_info = raw.get("account_info")
    if isinstance(account_info, list):
        for item in account_info:
            if isinstance(item, dict):
                email = str(item.get("email") or "").strip()
                if "@" in email:
                    return email
    elif isinstance(account_info, dict):
        email = str(account_info.get("email") or "").strip()
        if "@" in email:
            return email
    profile = raw.get("profile")
    if isinstance(profile, dict):
        name = str(profile.get("name") or "").strip()
        if "@" in name:
            return name
    return ""


def chrome_cdp_port_map() -> dict[str, int]:
    from flow2api.config import CDP_BASE_PORT

    profiles = sorted_chrome_profiles()
    return {name: CDP_BASE_PORT + idx for idx, name in enumerate(profiles)}


def _playwright_cdp_port_fallback() -> int:
    """Port CDP khi không map được profile Chrome (chưa cài / profile chưa có)."""
    cfg = load_config()
    try:
        manual = int(cfg.get("playwright_flow_cdp_port") or 0)
        if manual:
            return max(1024, min(65535, manual))
    except (TypeError, ValueError):
        pass
    env = str(os.environ.get("FLOW2API_PLAYWRIGHT_FLOW_CDP_PORT", "") or "").strip()
    if env:
        try:
            return max(1024, min(65535, int(env)))
        except ValueError:
            pass
    from flow2api.config import CDP_BASE_PORT

    return CDP_BASE_PORT


def get_cdp_port_for_chrome_dir(chrome_dir: str) -> int:
    chrome_dir = str(chrome_dir or "").strip()
    if not chrome_dir:
        return _playwright_cdp_port_fallback()
    mapped = chrome_cdp_port_map().get(chrome_dir)
    if mapped is not None:
        return mapped
    return _playwright_cdp_port_fallback()


def email_to_chrome_dir(email: str) -> str | None:
    needle = str(email or "").strip().lower()
    if not needle or "@" not in needle:
        return None
    for chrome_dir in sorted_chrome_profiles():
        em = read_chrome_profile_email(chrome_dir).strip().lower()
        if em and em == needle:
            return chrome_dir
    return None


def get_cdp_port_for_email(email: str) -> int | None:
    chrome_dir = email_to_chrome_dir(email)
    if not chrome_dir:
        return None
    return get_cdp_port_for_chrome_dir(chrome_dir)


def get_playwright_flow_email() -> str:
    cfg = load_config()
    saved = str(cfg.get("playwright_flow_email") or "").strip()
    if saved:
        return saved
    chrome_dir = get_playwright_flow_chrome_profile()
    return read_chrome_profile_email(chrome_dir)


def get_cdp_port_for_extension_profile(profile_id: str) -> int:
    pid = str(profile_id or "").strip()
    if pid:
        try:
            from flow2api.services.extension_pool import get_extension_pool

            session = get_extension_pool().get(pid)
            if session:
                email = str(session.email or "").strip()
                if email:
                    port = get_cdp_port_for_email(email)
                    if port is not None:
                        return port
        except Exception:
            pass
    return get_playwright_flow_cdp_port()


def list_playwright_profile_map() -> list[dict[str, Any]]:
    port_map = chrome_cdp_port_map()
    rows: list[dict[str, Any]] = []
    for chrome_dir in sorted_chrome_profiles():
        email = read_chrome_profile_email(chrome_dir)
        rows.append(
            {
                "chrome_dir": chrome_dir,
                "email": email,
                "cdp_port": port_map.get(chrome_dir),
            }
        )
    return rows


def list_playwright_extension_map() -> list[dict[str, Any]]:
    try:
        from flow2api.services.extension_pool import get_extension_pool

        sessions = get_extension_pool().list_sessions()
    except Exception:
        sessions = []
    rows: list[dict[str, Any]] = []
    for session in sessions:
        pid = str(session.profile_id or "").strip()
        if not pid or pid.startswith("_"):
            continue
        email = str(session.email or "").strip()
        chrome_dir = email_to_chrome_dir(email) if email else None
        port = get_cdp_port_for_email(email) if email else None
        rows.append(
            {
                "profile_id": pid,
                "email": email,
                "display_name": session.display_name(),
                "connected": session.connected,
                "ready": session.is_ready(),
                "chrome_dir": chrome_dir,
                "cdp_port": port,
            }
        )
    rows.sort(key=lambda x: (not x.get("ready"), x.get("display_name") or ""))
    return rows


def resolve_playwright_target(*, flow_email: str = "", flow_chrome_profile: str = "") -> tuple[str, int, str]:
    email = str(flow_email or "").strip()
    profile = str(flow_chrome_profile or "").strip()
    if email:
        chrome_dir = email_to_chrome_dir(email)
        if not chrome_dir:
            raise ValueError(f"invalid_chrome_email:{email}")
        return chrome_dir, get_cdp_port_for_chrome_dir(chrome_dir), email
    if profile:
        known = set(list_chrome_profiles())
        if known and profile not in known:
            raise ValueError(f"invalid_chrome_profile:{profile}")
        return profile, get_cdp_port_for_chrome_dir(profile), read_chrome_profile_email(profile)
    profile = get_playwright_flow_chrome_profile()
    return profile, get_cdp_port_for_chrome_dir(profile), read_chrome_profile_email(profile)


def get_playwright_flow_chrome_profile() -> str:
    cfg = load_config()
    saved_email = str(cfg.get("playwright_flow_email") or "").strip()
    if saved_email:
        chrome_dir = email_to_chrome_dir(saved_email)
        if chrome_dir:
            return chrome_dir
    saved_profile = str(cfg.get("playwright_flow_chrome_profile") or "").strip()
    if saved_profile:
        known = set(list_chrome_profiles())
        if not known or saved_profile in known:
            return saved_profile
    env = str(os.environ.get("FLOW2API_FLOW_CHROME_PROFILE", "") or "").strip()
    if env:
        return env
    return "Default"


def get_playwright_flow_cdp_port() -> int:
    cfg = load_config()
    saved_email = str(cfg.get("playwright_flow_email") or "").strip()
    if saved_email:
        auto = get_cdp_port_for_email(saved_email)
        if auto is not None:
            return auto
    saved_profile = str(cfg.get("playwright_flow_chrome_profile") or "").strip()
    if saved_profile:
        known = set(list_chrome_profiles())
        if known and saved_profile in known:
            return get_cdp_port_for_chrome_dir(saved_profile)
    return _playwright_cdp_port_fallback()


def save_playwright_settings(
    *,
    flow_chrome_profile: str = "",
    flow_cdp_port: int | None = None,
    flow_email: str = "",
) -> dict[str, Any]:
    email = str(flow_email or "").strip()
    if email:
        profile, port, resolved_email = resolve_playwright_target(flow_email=email)
    else:
        profile, port, resolved_email = resolve_playwright_target(
            flow_chrome_profile=flow_chrome_profile or get_playwright_flow_chrome_profile(),
        )
    if flow_cdp_port is not None and not email:
        try:
            port = max(1024, min(65535, int(flow_cdp_port)))
        except (TypeError, ValueError):
            pass
    saved = save_config(
        {
            "playwright_flow_chrome_profile": profile,
            "playwright_flow_cdp_port": port,
            "playwright_flow_email": resolved_email or email or "",
        }
    )
    ensure_flow_launch_script()
    return saved


def _flow_launch_bat_path() -> Path:
    return _SCRIPTS_DIR / "Launch-Flow-Profile.bat"


def ensure_flow_launch_script() -> Path:
    """Script chi mo 1 Chrome profile (Flow / Playwright) — cau hinh tu dashboard."""
    _ensure_storage()
    cfg = load_config()
    flow_url = str(cfg.get("flow_url") or _FLOW_URL_DEFAULT).replace('"', "")
    flow_profile = get_playwright_flow_chrome_profile().replace('"', "")
    flow_cdp = get_playwright_flow_cdp_port()
    cdp_user_data = str(_cdp_user_data_dir()).replace('"', "")
    cdp_flags = " ".join(_chrome_cdp_shared_flags())
    start_cmd = _chrome_windows_start_cmd()
    bat = _flow_launch_bat_path()
    content = f"""@echo off
setlocal
title Flow2API — Launch Flow Profile

set "CHROME_PATH=C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
if not exist "%CHROME_PATH%" set "CHROME_PATH=C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe"
if not exist "%CHROME_PATH%" set "CHROME_PATH=%LOCALAPPDATA%\\Google\\Chrome\\Application\\chrome.exe"
if not exist "%CHROME_PATH%" (
  echo Chrome not found
  exit /b 1
)

set "USER_DATA={cdp_user_data}"
set "FLOW_URL={flow_url}"
set "FLOW_PROFILE={flow_profile}"
set "FLOW_CDP={flow_cdp}"

if not exist "%USER_DATA%\\%FLOW_PROFILE%\\Preferences" (
  echo Profile %FLOW_PROFILE% not found in CDP user-data — chay launch-chrome-cdp.bat de dong bo
  exit /b 1
)

echo Opening Flow profile %FLOW_PROFILE% CDP=%FLOW_CDP% (CDP user-data)
{start_cmd} "" "%CHROME_PATH%" --user-data-dir="%USER_DATA%" --profile-directory="%FLOW_PROFILE%" --remote-debugging-port=%FLOW_CDP% {cdp_flags} "%FLOW_URL%"
echo Done. Kiem tra: http://localhost:%FLOW_CDP%/json/version
"""
    bat.write_text(content, encoding="utf-8", newline="\r\n")
    return bat


def close_all_chrome() -> dict[str, Any]:
    for exe in ("chrome.exe", "GoogleCrashHandler.exe"):
        subprocess.run(
            ["taskkill", "/F", "/IM", exe, "/T"],
            capture_output=True,
            text=True,
        )
    return {"ok": True, "message": "Đã đóng toàn bộ Chrome"}


def is_chrome_running() -> bool:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        out = (result.stdout or "").lower()
        if "no tasks" in out:
            return False
        return "chrome.exe" in out
    except Exception:
        return False


def _clear_chrome_singleton_locks(user_data_root: Path | None = None) -> None:
    roots = [user_data_root] if user_data_root else [_user_data_dir(), _cdp_user_data_dir()]
    for root in roots:
        if not root:
            continue
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "DevToolsActivePort"):
            path = root / name
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.debug("clear singleton %s: %s", name, exc)


def ensure_chrome_fully_closed(*, max_wait_s: int = 25) -> bool:
    """Đóng hết Chrome + xóa singleton lock — bắt buộc trước khi bật --remote-debugging-port."""
    deadline = time.time() + max(5, max_wait_s)
    while time.time() < deadline:
        if is_chrome_running():
            close_all_chrome()
            time.sleep(1.5)
            continue
        _clear_chrome_singleton_locks()
        time.sleep(1.0)
        if not is_chrome_running():
            return True
    running = is_chrome_running()
    if running:
        logger.warning("Chrome vẫn còn chạy sau ensure_chrome_fully_closed")
    return not running


def _prime_chrome_flow_startup(chrome_dir: str, flow_url: str, *, user_data_root: Path | None = None) -> None:
    """
    Chrome bị taskkill thoát 'unclean' → lần mở sau restore hết tab cũ (+ URL launch = 3 tab Flow).
    Ép mở đúng 1 URL Flow khi khởi động.
    """
    root = user_data_root or _cdp_user_data_dir()
    prefs_path = root / chrome_dir / "Preferences"
    if not prefs_path.is_file():
        return
    try:
        raw = json.loads(prefs_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("prime chrome prefs read failed %s: %s", chrome_dir, exc)
        return
    if not isinstance(raw, dict):
        raw = {}
    session = raw.setdefault("session", {})
    if not isinstance(session, dict):
        session = {}
        raw["session"] = session
    session["restore_on_startup"] = 4
    session["startup_urls"] = [flow_url]
    raw["exit_type"] = "Normal"
    raw["exited_cleanly"] = True
    try:
        prefs_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.warning("prime chrome prefs write failed %s: %s", chrome_dir, exc)


def get_chrome_process_cmdlines() -> list[str]:
    if os.name != "nt":
        return []
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" "
                "| Select-Object -ExpandProperty CommandLine",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]
    except Exception as exc:
        logger.debug("get_chrome_process_cmdlines: %s", exc)
        return []


def chrome_has_cdp_flag(cdp_port: int) -> bool:
    needle = f"--remote-debugging-port={cdp_port}"
    for line in get_chrome_process_cmdlines():
        if needle in line.replace(" ", ""):
            return True
        if f"--remote-debugging-port={cdp_port}" in line:
            return True
    return False


def diagnose_chrome_cdp(cdp_port: int) -> str:
    if not is_chrome_running():
        return "Khong co chrome.exe — Chrome chua duoc mo."
    if chrome_has_cdp_flag(cdp_port):
        try:
            import httpx

            for h in ("localhost", "127.0.0.1"):
                try:
                    resp = httpx.get(f"http://{h}:{cdp_port}/json/version", timeout=2.0)
                    if resp.status_code == 200:
                        return f"CDP OK tren {h}:{cdp_port}."
                except Exception:
                    continue
        except Exception:
            pass
        return (
            f"Chrome co co CDP port {cdp_port} nhung port chua len. "
            "Neu dung User Data mac dinh: Chrome 136+ chan CDP — can launch-chrome-cdp.bat "
            "(dong bo profile sang thu muc CDP rieng)."
        )
    return (
        "Chrome dang chay nhung KHONG co --remote-debugging-port "
        "(mo bang icon desktop hoac gan instance cu). "
        "Task Manager -> End task tat ca chrome.exe -> chay lai launch-chrome-cdp.bat."
    )


def _launch_chrome_cdp_process(*, chrome_dir: str, cdp_port: int, flow_url: str) -> None:
    paths = _chrome_paths()
    if not paths:
        raise RuntimeError("chrome_not_found")
    chrome = paths[0]

    if is_chrome_running():
        ensure_chrome_fully_closed(max_wait_s=20)

    user_data, _ = ensure_cdp_profile_ready(chrome_dir)
    prefs = user_data / chrome_dir / "Preferences"
    if not prefs.is_file():
        raise RuntimeError(f"profile_not_found:{chrome_dir}")

    _clear_chrome_singleton_locks(user_data)
    _prime_chrome_flow_startup(chrome_dir, flow_url, user_data_root=user_data)

    # Windows: `start` + bat đáng tin cậy hơn Popen (tránh Chrome gắn instance không có CDP).
    if os.name == "nt":
        bat = ensure_flow_launch_script()
        logger.info("Launch Chrome via bat profile=%s port=%s bat=%s", chrome_dir, cdp_port, bat)
        subprocess.Popen(
            ["cmd", "/c", str(bat)],
            cwd=str(_SCRIPTS_DIR),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        time.sleep(4)
        if chrome_has_cdp_flag(cdp_port):
            return
        logger.warning("Bat launch: Chrome chua co CDP flag — thu Popen truc tiep")

    args = [
        str(chrome),
        f"--user-data-dir={user_data}",
        f"--profile-directory={chrome_dir}",
        f"--remote-debugging-port={cdp_port}",
        *_chrome_cdp_shared_flags(),
        flow_url,
    ]
    logger.info("Launch Chrome CDP direct profile=%s port=%s user_data=%s", chrome_dir, cdp_port, user_data)
    subprocess.Popen(args, cwd=str(_SCRIPTS_DIR), close_fds=True)


def launch_flow_chrome_profile(
    *,
    flow_email: str = "",
    profile_id: str = "",
    kill_chrome_first: bool = True,
    wait_for_cdp: bool = False,
) -> dict[str, Any]:
    email = str(flow_email or "").strip()
    if not email and profile_id:
        try:
            from flow2api.services.extension_pool import get_extension_pool

            session = get_extension_pool().get(str(profile_id).strip())
            if session:
                email = str(session.email or "").strip()
        except Exception:
            pass
    if email:
        try:
            save_playwright_settings(flow_email=email)
        except ValueError as exc:
            msg = str(exc)
            if msg.startswith("invalid_chrome_email:"):
                return {
                    "ok": False,
                    "error": "invalid_chrome_email",
                    "message": f"Không tìm thấy Chrome profile cho email «{msg.split(':', 1)[-1]}»",
                }
            return {"ok": False, "error": "invalid_playwright_target", "message": msg}
    else:
        ensure_flow_launch_script()

    profile = get_playwright_flow_chrome_profile()
    port = get_playwright_flow_cdp_port()
    email = get_playwright_flow_email()
    if profile not in list_chrome_profiles():
        return {
            "ok": False,
            "error": "profile_not_found",
            "message": f"Không tìm thấy Chrome profile «{profile}»",
        }
    if not _chrome_paths():
        return {"ok": False, "error": "chrome_not_found", "message": "Không tìm thấy chrome.exe"}

    cfg = load_config()
    flow_url = str(cfg.get("flow_url") or _FLOW_URL_DEFAULT)

    if kill_chrome_first:
        if not ensure_chrome_fully_closed():
            return {
                "ok": False,
                "error": "chrome_still_running",
                "message": "Không đóng hết Chrome — tắt thủ công rồi chạy lại launch-chrome-cdp.bat",
            }
    elif is_chrome_running():
        if not ensure_chrome_fully_closed():
            return {
                "ok": False,
                "error": "chrome_still_running",
                "message": (
                    "Chrome đang chạy không có CDP — đóng hết Chrome "
                    "(kể cả icon taskbar) rồi chạy lại launch-chrome-cdp.bat"
                ),
            }

    try:
        _launch_chrome_cdp_process(chrome_dir=profile, cdp_port=port, flow_url=flow_url)
    except RuntimeError as exc:
        return {"ok": False, "error": "launch_failed", "message": str(exc)}

    ensure_flow_launch_script()
    if not wait_for_cdp:
        return {
            "ok": True,
            "message": f"Đã khởi động Chrome {profile} ({email or '—'}) — chờ CDP port {port}",
            "profile": profile,
            "email": email,
            "cdp_port": port,
            "cdp_url": f"http://localhost:{port}/json/version",
            "killed_chrome_first": kill_chrome_first,
        }

    from flow2api.services.playwright_pool import wait_cdp_port_blocking

    ready, cdp_host = wait_cdp_port_blocking(port, max_wait_s=90, verbose=True)
    if not ready:
        return {
            "ok": False,
            "error": "cdp_timeout",
            "message": (
                f"Chrome đã mở {profile} nhưng CDP chưa lên tại localhost:{port}. "
                f"Thử mở http://localhost:{port}/json/version trong trình duyệt."
            ),
            "profile": profile,
            "email": email,
            "cdp_port": port,
        }
    cdp_url = f"http://{cdp_host}:{port}/json/version"
    return {
        "ok": True,
        "message": f"Đã mở {profile} ({email or '—'}) → Flow (CDP {cdp_host}:{port})",
        "profile": profile,
        "email": email,
        "cdp_port": port,
        "cdp_host": cdp_host,
        "cdp_url": cdp_url,
        "killed_chrome_first": kill_chrome_first,
    }


def launch_flow_chrome_for_extension(
    profile_id: str,
    *,
    kill_chrome_first: bool = True,
    wait_for_cdp: bool = False,
) -> dict[str, Any]:
    return launch_flow_chrome_profile(
        profile_id=profile_id,
        kill_chrome_first=kill_chrome_first,
        wait_for_cdp=wait_for_cdp,
    )


def ensure_launch_script() -> Path:
    _ensure_storage()
    cfg = load_config()
    flow_url = str(cfg.get("flow_url") or _FLOW_URL_DEFAULT).replace('"', "")
    from flow2api.config import CDP_BASE_PORT

    flow_profile = get_playwright_flow_chrome_profile().replace('"', "")
    flow_cdp = get_playwright_flow_cdp_port()
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
set "FLOW_PROFILE={flow_profile}"
set "FLOW_CDP={flow_cdp}"
set "CDP_PORT={CDP_BASE_PORT}"

rem === Flow / Playwright: profile co dinh, port co dinh (mo TRUOC) ===
if exist "%USER_DATA%\\%FLOW_PROFILE%\\Preferences" (
  echo Opening Flow profile !FLOW_PROFILE! CDP=!FLOW_CDP!
  start "" "%CHROME_PATH%" --user-data-dir="%USER_DATA%" --profile-directory="%FLOW_PROFILE%" --remote-debugging-port=!FLOW_CDP! --remote-debugging-address=127.0.0.1 --remote-allow-origins=* --hide-crash-restore-bubble --disable-session-crashed-bubble "%FLOW_URL%"
  ping 127.0.0.1 -n 5 > nul
) else (
  echo CANH BAO: Khong tim thay profile %FLOW_PROFILE% — Playwright port %FLOW_CDP% se khong co.
)

if !FLOW_CDP! equ {CDP_BASE_PORT} set /a CDP_PORT+=1

if exist "%USER_DATA%\\Default\\Preferences" (
  if /I not "Default"=="%FLOW_PROFILE%" (
    echo Opening Default CDP=!CDP_PORT!
    start "" "%CHROME_PATH%" --user-data-dir="%USER_DATA%" --profile-directory="Default" --remote-debugging-port=!CDP_PORT! --remote-debugging-address=127.0.0.1 --remote-allow-origins=* --hide-crash-restore-bubble --disable-session-crashed-bubble "%FLOW_URL%"
    set /a CDP_PORT+=1
    ping 127.0.0.1 -n 3 > nul
  )
)

for /D %%D in ("%USER_DATA%\\Profile *") do (
  if exist "%%D\\Preferences" (
    set "prof=%%~nxD"
    if /I not "!prof!"=="%FLOW_PROFILE%" (
      echo Opening !prof! CDP=!CDP_PORT!
      start "" "%CHROME_PATH%" --user-data-dir="%USER_DATA%" --profile-directory="!prof!" --remote-debugging-port=!CDP_PORT! --remote-debugging-address=127.0.0.1 --remote-allow-origins=* --hide-crash-restore-bubble --disable-session-crashed-bubble "%FLOW_URL%"
      set /a CDP_PORT+=1
      ping 127.0.0.1 -n 3 > nul
    )
  )
)
echo Done. Flow/Playwright: %FLOW_PROFILE% @ port %FLOW_CDP%
echo Kiem tra: http://127.0.0.1:%FLOW_CDP%/json/version
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


def set_windows_autostart(enabled: bool) -> dict[str, Any]:
    cfg = save_config({"windows_autostart": enabled})
    startup = _startup_bat_path()
    if not enabled:
        if startup.is_file():
            startup.unlink(missing_ok=True)
        return {"ok": True, "enabled": False, "message": "Đã TẮT khởi động cùng Windows"}
    ensure_flow_launch_script()
    run_bat = APP_ROOT / "run.bat"
    agent_bat = f'@echo off\r\nstart "" /min cmd /c "cd /d "{APP_ROOT}" && run.bat"\r\nping 127.0.0.1 -n 18 > nul\r\ncall "{_flow_launch_bat_path()}"\r\n'
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


def _profile_proxy_disabled_list(cfg: dict[str, Any] | None = None) -> list[str]:
    cfg = cfg if cfg is not None else load_config()
    raw = cfg.get("profile_proxy_disabled") or []
    if not isinstance(raw, list):
        return []
    return sorted({str(x) for x in raw if x and not str(x).startswith("_")})


def is_profile_proxy_attach_enabled(profile_id: str, cfg: dict[str, Any] | None = None) -> bool:
    cfg = cfg if cfg is not None else load_config()
    if not is_proxy_pool_enabled(cfg):
        return False
    pid = str(profile_id or "").strip()
    if not pid or pid.startswith("_"):
        return False
    return pid not in _profile_proxy_disabled_list(cfg)


def is_profile_proxy_pool_eligible(profile_id: str, cfg: dict[str, Any] | None = None) -> bool:
    """Profile nhận slot proxy từ pool khi bật gắn IP và đang nhận phân bổ job."""
    if not is_profile_proxy_attach_enabled(profile_id, cfg):
        return False
    from flow2api.services.worker_settings import is_profile_dispatch_enabled

    return is_profile_dispatch_enabled(profile_id)


async def set_profile_proxy_attach_enabled(profile_id: str, enabled: bool) -> dict[str, Any]:
    pid = str(profile_id or "").strip()
    if not pid or pid.startswith("_"):
        raise ValueError("invalid_profile_id")
    cfg = load_config()
    disabled = _profile_proxy_disabled_list(cfg)
    if enabled:
        disabled = [x for x in disabled if x != pid]
    elif pid not in disabled:
        disabled.append(pid)
    save_config({"profile_proxy_disabled": disabled})
    await push_proxy_to_extensions()
    return {
        "profile_id": pid,
        "proxy_attach_enabled": is_profile_proxy_attach_enabled(pid),
    }


def parse_proxy_pool(text: str) -> list[str]:
    lines = []
    for line in (text or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


def format_proxy_public(proxy_url: str) -> dict[str, Any]:
    raw = str(proxy_url or "").strip()
    if not raw:
        return {"proxy": "", "proxy_display": "", "proxy_attached": False}
    parts = raw.split(":")
    host = parts[0] if parts else ""
    port = parts[1] if len(parts) > 1 else ""
    display = f"{host}:{port}" if host and port else host
    return {"proxy": display, "proxy_display": display, "proxy_attached": bool(display)}


def parse_proxy_for_playwright(proxy_url: str) -> dict[str, str] | None:
    """host:port[:user[:pass]] → Playwright launch/connect proxy dict."""
    raw = str(proxy_url or "").strip()
    if not raw:
        return None
    parts = raw.split(":")
    host = parts[0] if parts else ""
    port = parts[1] if len(parts) > 1 else "80"
    if not host:
        return None
    out: dict[str, str] = {"server": f"http://{host}:{port}"}
    if len(parts) > 2 and parts[2]:
        out["username"] = parts[2]
    if len(parts) > 3 and parts[3]:
        out["password"] = parts[3]
    return out


def resolve_profile_proxy_url(profile_id: str) -> str:
    """Proxy hiệu lực cho profile (đã gắn hoặc slot pool)."""
    pid = str(profile_id or "").strip()
    if not pid:
        return ""
    try:
        from flow2api.services.extension_pool import get_extension_pool

        session = get_extension_pool().get(pid)
        if session:
            pending = getattr(session, "pending_proxy_url", None)
            if pending is not None:
                return str(pending or "")
            applied = str(getattr(session, "applied_proxy_url", "") or "")
            if applied:
                return applied
    except Exception:
        pass
    return proxy_url_for_profile_id(pid)


def proxy_url_for_profile_id(profile_id: str) -> str:
    cfg = load_config()
    if not is_profile_proxy_pool_eligible(profile_id, cfg):
        return ""
    return _proxy_url_for_index(cfg, _profile_proxy_index(profile_id))


def proxy_rotate_status(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg if cfg is not None else load_config()
    attach = is_proxy_pool_enabled(cfg)
    enabled = bool(cfg.get("proxy_rotate_enabled"))
    pool = cfg.get("proxy_pool") or []
    interval_min = max(1, int(cfg.get("proxy_rotate_interval_min") or 30))
    interval_s = proxy_rotate_interval_seconds(cfg)
    last_at = float(cfg.get("proxy_rotate_last_at") or 0)
    offset = int(cfg.get("proxy_rotate_offset") or 0)
    now = time.time()
    rotated = offset > 0

    if not enabled:
        return {
            "active": False,
            "rotated": rotated,
            "rotate_count": offset,
            "seconds_remaining": None,
            "interval_sec": interval_s,
            "last_rotated_at": last_at if rotated else 0,
            "status": "disabled",
            "status_text": "Xoay proxy đang tắt",
        }
    if not attach:
        return {
            "active": False,
            "rotated": rotated,
            "rotate_count": offset,
            "seconds_remaining": None,
            "interval_sec": interval_s,
            "last_rotated_at": last_at if rotated else 0,
            "status": "need_attach",
            "status_text": "Bật gắn proxy vào profile để xoay",
        }
    if len(pool) < 2:
        return {
            "active": False,
            "rotated": rotated,
            "rotate_count": offset,
            "seconds_remaining": None,
            "interval_sec": interval_s,
            "last_rotated_at": last_at if rotated else 0,
            "status": "need_proxies",
            "status_text": "Cần ít nhất 2 proxy trong pool",
        }

    if last_at <= 0:
        remaining = interval_s
        status = "arming"
        status_text = "Đang khởi động bộ đếm — chưa xoay"
    else:
        remaining = max(0, int(last_at + interval_s - now))
        if not rotated:
            status = "arming"
            status_text = "Chưa xoay lần nào — đang đếm"
        elif remaining <= 0:
            status = "due"
            status_text = f"Đã xoay {offset} lần — sắp xoay tiếp"
        else:
            status = "countdown"
            status_text = f"Đã xoay {offset} lần — lần xoay gần nhất đã áp dụng"

    return {
        "active": True,
        "rotated": rotated,
        "rotate_count": offset,
        "seconds_remaining": remaining,
        "interval_sec": interval_s,
        "last_rotated_at": last_at,
        "next_at": (last_at + interval_s) if last_at > 0 else now + interval_s,
        "status": status,
        "status_text": status_text,
    }


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
    await _apply_proxy_rotation(cfg, now)
    return True


async def rotate_proxies_now() -> dict[str, Any]:
    cfg = load_config()
    if not is_proxy_pool_enabled(cfg):
        return {
            "ok": False,
            "error": "proxy_pool_disabled",
            "message": "Bật gắn proxy vào profile trước",
        }
    pool = cfg.get("proxy_pool") or []
    if len(pool) < 2:
        return {
            "ok": False,
            "error": "need_proxies",
            "message": "Cần ít nhất 2 proxy trong pool để xoay IP",
        }
    now = time.time()
    new_offset = await _apply_proxy_rotation(cfg, now)
    pub = public_config()
    return {
        "ok": True,
        "message": f"Đã xoay IP ngay (lần {new_offset})",
        "proxy_rotate_offset": new_offset,
        "proxy_rotate_status": pub.get("proxy_rotate_status"),
    }


async def _apply_proxy_rotation(cfg: dict[str, Any], now: float) -> int:
    new_offset = int(cfg.get("proxy_rotate_offset") or 0) + 1
    save_config({"proxy_rotate_offset": new_offset, "proxy_rotate_last_at": now})
    await push_proxy_to_extensions(defer_if_busy=True)
    pool = cfg.get("proxy_pool") or []
    logger.info(
        "proxy rotated offset=%s interval=%sm pool=%s (busy profiles deferred)",
        new_offset,
        int(cfg.get("proxy_rotate_interval_min") or 30),
        len(pool),
    )
    return new_offset


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
        if is_profile_proxy_pool_eligible(session.profile_id):
            idx += 1
    return 0


async def broadcast_system(payload: dict) -> None:
    from flow2api.services.extension_pool import get_extension_pool

    await get_extension_pool().broadcast(payload)


async def force_refresh_all() -> dict[str, Any]:
    await broadcast_system({"type": "system_force_refresh"})
    return {"ok": True, "message": "Đã gửi lệnh F5 toàn bộ tab Flow"}


async def apply_proxy_to_session(session: Any, proxy_url: str) -> None:
    """Push proxy to Chrome extension immediately."""
    old = str(getattr(session, "applied_proxy_url", "") or "")
    new = str(proxy_url or "")
    session.applied_proxy_url = new
    try:
        await session.send_json({"type": "system_set_proxy", "proxyUrl": proxy_url})
    except Exception as exc:
        logger.warning("proxy push failed %s: %s", session.profile_id[:8], exc)
        return
    if old != new:
        try:
            from flow2api.services.playwright_pool import get_playwright_pool, is_playwright_enabled

            if is_playwright_enabled():
                await get_playwright_pool().invalidate_profile(session.profile_id)
        except Exception as exc:
            logger.debug("playwright proxy invalidate failed: %s", exc)


async def ensure_profile_proxy_applied(
    profile_id: str,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """
    Gắn proxy pool vào Chrome profile trước Playwright/UI (kể cả khi đang có job).
    Playwright CDP dùng chung Chrome → traffic đi qua proxy extension đã set.
    """
    from flow2api.services.extension_pool import get_extension_pool

    pid = str(profile_id or "").strip()
    assigned = proxy_url_for_profile_id(pid) if is_profile_proxy_pool_eligible(pid) else ""
    ext = get_extension_pool().get(pid)
    if ext and ext.connected:
        should_push = force or ext.pending_proxy_url is not None
        if not should_push and assigned and not ext.applied_proxy_url:
            should_push = True
        if should_push:
            await push_proxy_to_session(ext, defer_if_busy=False)
        effective = str(ext.applied_proxy_url or assigned or "")
    else:
        effective = assigned

    pub = format_proxy_public(effective)
    pub["proxy_assigned"] = format_proxy_public(assigned).get("proxy_display") or ""
    pub["proxy_pool_enabled"] = is_proxy_pool_enabled()
    pub["proxy_attach_enabled"] = is_profile_proxy_attach_enabled(pid)
    return pub


async def push_proxy_to_session(
    session: Any,
    profile_index: int | None = None,
    *,
    defer_if_busy: bool = True,
) -> bool:
    """Assign proxy from pool; defer Chrome apply while profile has active jobs."""
    cfg = load_config()
    if not is_profile_proxy_pool_eligible(session.profile_id, cfg):
        proxy_url = ""
        session.pending_proxy_url = None
    else:
        idx = profile_index if profile_index is not None else _profile_proxy_index(session.profile_id)
        proxy_url = _proxy_url_for_index(cfg, idx)
    if defer_if_busy and int(getattr(session, "active_jobs", 0) or 0) > 0:
        session.pending_proxy_url = proxy_url
        logger.info(
            "proxy deferred profile=%s active_jobs=%s",
            session.profile_id[:12],
            session.active_jobs,
        )
        return False
    session.pending_proxy_url = None
    await apply_proxy_to_session(session, proxy_url)
    return True


async def push_proxy_to_extensions(*, defer_if_busy: bool = True) -> None:
    idx = 0
    from flow2api.services.extension_pool import get_extension_pool

    for session in get_extension_pool().list_sessions():
        if session.profile_id.startswith("_"):
            continue
        if is_profile_proxy_pool_eligible(session.profile_id):
            await push_proxy_to_session(session, idx, defer_if_busy=defer_if_busy)
            idx += 1
        else:
            await push_proxy_to_session(session, defer_if_busy=defer_if_busy)


async def sync_profile_clear_settings(session: Any) -> dict | None:
    """Clear Data is event-driven; ensure legacy periodic clear is stopped."""
    if not getattr(session, "connected", False):
        return None
    pid = str(getattr(session, "profile_id", "") or "")
    if not pid or pid.startswith("_"):
        return None
    try:
        return await session.clear_control("stop")
    except Exception as exc:
        logger.warning("sync clear settings failed %s: %s", pid[:12], exc)
        return None


async def maybe_clear_profile_when_idle(
    session: Any,
    *,
    trigger: str,
    project_id: str | None = None,
) -> dict | None:
    """Clear labs.google site data for a Flow project when profile has no active jobs."""
    from flow2api.services.worker_settings import is_profile_clear_enabled

    if trigger not in ("success",):
        return None
    if not getattr(session, "connected", False):
        return None
    pid = str(getattr(session, "profile_id", "") or "")
    if not pid or pid.startswith("_"):
        return None
    if int(getattr(session, "active_jobs", 0) or 0) > 0:
        return None
    if not is_profile_clear_enabled(pid):
        return None
    proj = str(project_id or "").strip()
    if not proj:
        logger.warning("profile clear skipped profile=%s: missing project_id", pid[:12])
        return None
    from flow2api.config import POST_CLEAR_COOLDOWN_S, POST_SUCCESS_CLEAR_DELAY_S

    delay_s = max(0, int(POST_SUCCESS_CLEAR_DELAY_S)) if trigger == "success" else 0
    cleared = False
    try:
        if delay_s > 0:
            if hasattr(session, "extend_dispatch_hold"):
                session.extend_dispatch_hold(delay_s)
            logger.info(
                "profile clear chờ %ss sau task thành công profile=%s project=%s",
                delay_s,
                pid[:12],
                proj[:12],
            )
            await asyncio.sleep(delay_s)
            if int(getattr(session, "active_jobs", 0) or 0) > 0:
                logger.info(
                    "profile clear bỏ qua profile=%s: có job mới trong lúc chờ",
                    pid[:12],
                )
                if hasattr(session, "release_dispatch_hold"):
                    session.release_dispatch_hold()
                return None
            if not getattr(session, "connected", False):
                if hasattr(session, "release_dispatch_hold"):
                    session.release_dispatch_hold()
                return None
        result = await apply_profile_clear_now(session, project_id=proj)
        cleared = True
        logger.info(
            "profile clear after %s project=%s profile=%s ok=%s",
            trigger,
            proj[:12],
            pid[:12],
            result.get("ok"),
        )
        return result
    except Exception as exc:
        logger.warning(
            "profile clear failed profile=%s project=%s trigger=%s: %s",
            pid[:12],
            proj[:12],
            trigger,
            exc,
        )
        return None
    finally:
        if cleared and hasattr(session, "extend_dispatch_hold"):
            cooldown = max(0, int(POST_CLEAR_COOLDOWN_S))
            if cooldown > 0:
                session.extend_dispatch_hold(cooldown)
                logger.info(
                    "profile %s chờ %ss sau clear trước khi nhận task mới",
                    pid[:12],
                    cooldown,
                )


async def apply_profile_clear_now(
    session: Any,
    *,
    project_id: str | None = None,
) -> dict:
    proj = str(project_id or "").strip()
    if not proj:
        raise ValueError("missing_project_id")
    return await session.clear_control(
        "now",
        project_id=proj,
        timeout=45.0,
    )


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
        r = launch_flow_chrome_profile()
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
