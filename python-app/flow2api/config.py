"""Flow2API configuration (mirrors packaged agent env vars)."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _default_root() -> Path:
    """Directory that owns the app (python-app/ source, or folder of .exe)."""
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _default_frontend(app_root: Path) -> Path:
    env = (os.environ.get("FLOW2API_FRONTEND") or "").strip()
    if env:
        return Path(env)
    # Packaged layout: next to .exe
    packaged = app_root / "frontend"
    if packaged.is_dir() or _is_frozen():
        return packaged
    # Dev layout: repo/frontend next to python-app
    return app_root.parent / "frontend"


def storage_path_config_file(app_root: Path | None = None) -> Path:
    """File written by installer / launcher: one line, absolute storage folder."""
    root = app_root if app_root is not None else Path(os.environ.get("FLOW2API_ROOT", _default_root()))
    return root / "storage_path.txt"


def _read_storage_path_file(app_root: Path) -> Path | None:
    cfg = storage_path_config_file(app_root)
    if not cfg.is_file():
        return None
    try:
        text = cfg.read_text(encoding="utf-8-sig")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip().strip('"').strip("'")
        if line and not line.startswith("#"):
            return Path(line)
    return None


def write_storage_path_file(path: Path | str, app_root: Path | None = None) -> Path:
    """Persist user-chosen storage dir next to the app (installer / reconfigure)."""
    target = Path(path).expanduser().resolve()
    cfg = storage_path_config_file(app_root)
    cfg.write_text(str(target) + "\n", encoding="utf-8")
    return target


def _default_storage(app_root: Path) -> Path:
    env = (os.environ.get("FLOW2API_STORAGE") or "").strip()
    if env:
        return Path(env)
    from_file = _read_storage_path_file(app_root)
    if from_file is not None:
        return from_file
    localapp = (os.environ.get("LOCALAPPDATA") or "").strip()
    # No preference yet: frozen builds fall back to LocalAppData; source → app_root/storage
    if _is_frozen() and localapp:
        return Path(localapp) / "Flow2API" / "storage"
    return app_root / "storage"


ROOT = _default_root()
APP_ROOT = Path(os.environ.get("FLOW2API_ROOT", ROOT))
IS_FROZEN = _is_frozen()

HTTP_HOST = os.environ.get("FLOW2API_HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.environ.get("FLOW2API_HTTP_PORT", "1994"))
# Auto-restart khi sửa code (uvicorn --reload). Bật: set FLOW2API_RELOAD=1
RELOAD = os.environ.get("FLOW2API_RELOAD", "0").strip().lower() in ("1", "true", "yes", "on")
WS_HOST = os.environ.get("FLOW2API_WS_HOST", "127.0.0.1")
WS_PORT = int(os.environ.get("FLOW2API_EXT_WS_PORT", "1609"))

STORAGE_DIR = _default_storage(APP_ROOT)
DB_PATH = Path(os.environ.get("FLOW2API_DB", str(STORAGE_DIR / "flow2api.db")))
FRONTEND_DIR = _default_frontend(APP_ROOT)

GOOGLE_FLOW_API = "https://aisandbox-pa.googleapis.com"
TRPC_BASE = "https://labs.google/fx/api/trpc"
# Public API key used by Google Flow web client (extension injects bearer separately).
GOOGLE_API_KEY = os.environ.get(
    "GOOGLE_API_KEY", "AIzaSyBtrm0o5ab1c-Ec8ZuLcGt3oJAA5VWt3pY"
)

ADMIN_USER = os.environ.get("FLOW2API_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("FLOW2API_ADMIN_PASSWORD", "admin")
ADMIN_SESSION_COOKIE = "flow2api_admin_session"

POLL_INTERVAL_S = float(os.environ.get("FLOW2API_POLL_INTERVAL", "2.5"))
VIDEO_POLL_FIRST_DELAY_S = float(os.environ.get("FLOW2API_VIDEO_POLL_FIRST_DELAY", "20"))
VIDEO_POLL_INTERVAL_S = float(os.environ.get("FLOW2API_VIDEO_POLL_INTERVAL", "10"))
IMAGE_POLL_MAX = int(os.environ.get("FLOW2API_IMAGE_POLL_MAX", "90"))
VIDEO_POLL_MAX = int(os.environ.get("FLOW2API_VIDEO_POLL_MAX", "240"))
VIDEO_POLL_MEDIA_MAX = int(os.environ.get("FLOW2API_VIDEO_POLL_MEDIA_MAX", "20"))
# get_media downloads can be multi-MB over slow paths; 60s was cutting mid-transfer.
GET_MEDIA_TIMEOUT_S = float(os.environ.get("FLOW2API_GET_MEDIA_TIMEOUT_S", "180"))
RECAPTCHA_RETRY_MAX = int(os.environ.get("FLOW2API_RECAPTCHA_RETRY_MAX", "5"))
# Flow ya29 token TTL in DB (Veo3Studio cookieTokenService defaults).
FLOW_ACCESS_TOKEN_TTL_S = int(os.environ.get("FLOW2API_FLOW_TOKEN_TTL_S", str(55 * 60)))
FLOW_ACCESS_TOKEN_FALLBACK_TTL_S = int(
    os.environ.get("FLOW2API_FLOW_TOKEN_FALLBACK_TTL_S", str(5 * 60 * 60))
)
FLOW_ACCESS_TOKEN_MAX_TTL_S = int(
    os.environ.get("FLOW2API_FLOW_TOKEN_MAX_TTL_S", str(24 * 60 * 60))
)
# Refresh stored token when remaining <= this (Veo3Studio 5 min window).
FLOW_ACCESS_TOKEN_REFRESH_BEFORE_S = int(
    os.environ.get("FLOW2API_FLOW_TOKEN_REFRESH_BEFORE_S", str(5 * 60))
)
# Khi save token: access_token_expires_at DB = hạn API thật − offset (default 2h).
# UI / auto-CDP / get_stored_access_token đều dùng đúng hạn DB (không trừ thêm lần nữa).
TOKEN_ACTIVE_DISPLAY_OFFSET_S = int(
    os.environ.get("FLOW2API_TOKEN_ACTIVE_DISPLAY_OFFSET_S", str(2 * 3600))
)
# Direct HTTP lane (parity Veo3Studio googleFetch) — gen without Chrome extension proxy.
FLOW_DIRECT_HTTP_ENABLED = os.environ.get("FLOW2API_DIRECT_HTTP", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
FLOW_HTTP_IMPERSONATE = os.environ.get("FLOW2API_HTTP_IMPERSONATE", "chrome131")
FLOW_HTTP_CHROME_MAJOR = int(os.environ.get("FLOW2API_CHROME_MAJOR", "131"))
FLOW_HTTP_USER_AGENT = os.environ.get("FLOW2API_HTTP_USER_AGENT", "").strip()
HTTP_404_MAX_ATTEMPTS = int(os.environ.get("FLOW2API_HTTP_404_MAX_ATTEMPTS", "3"))
POLICY_REJECTION_ERROR_MSG = (
    "Google refused to create the image/video because it violates the content policy. "
    "Please replace the content or images."
)
HTTP_404_POLICY_ERROR_MSG = POLICY_REJECTION_ERROR_MSG
WORKER_MAX_CONCURRENT = int(os.environ.get("FLOW2API_MAX_CONCURRENT", "1"))
WORKER_TASK_STAGGER_S = float(os.environ.get("FLOW2API_TASK_STAGGER_S", "0"))
ACTIVITY_LIST_LIMIT = int(os.environ.get("FLOW2API_ACTIVITY_LIST_LIMIT", "100"))
ACTIVITY_META_LIMIT = int(os.environ.get("FLOW2API_ACTIVITY_META_LIMIT", "10000"))
ACTIVITY_PAGE_SIZE = int(os.environ.get("FLOW2API_ACTIVITY_PAGE_SIZE", "20"))
TASK_RUNNING_TIMEOUT_S = int(os.environ.get("FLOW2API_TASK_RUNNING_TIMEOUT_S", "1200"))
TASK_TIMEOUT_ERROR = "task_timeout_20m"
TASK_TIMEOUT_ERROR_MSG = (
    "Timeout: quá 20 phút không hoàn thành, job đã kết thúc."
)
# Auto-retry task_timeout_20m once while task age since created_at is within
# (running timeout + this grace). Default 20m + 10m → first 20m timeout can retry once.
TASK_TIMEOUT_RETRY_GRACE_S = int(os.environ.get("FLOW2API_TASK_TIMEOUT_RETRY_GRACE_S", "600"))
TASK_TIMEOUT_RETRY_MAX = int(os.environ.get("FLOW2API_TASK_TIMEOUT_RETRY_MAX", "1"))
WORKER_NUDGE_INTERVAL_S = int(os.environ.get("FLOW2API_WORKER_NUDGE_INTERVAL_S", "120"))
WORKER_NUDGE_STUCK_S = int(os.environ.get("FLOW2API_WORKER_NUDGE_STUCK_S", "120"))
# Fail HTTP handlers before Cloudflare 524 (~100s); return JSON 503 instead.
HTTP_HANDLER_TIMEOUT_S = float(os.environ.get("FLOW2API_HTTP_HANDLER_TIMEOUT_S", "25"))
HEALTH_CACHE_TTL_S = float(os.environ.get("FLOW2API_HEALTH_CACHE_TTL_S", "3"))
PURGE_INTERVAL_S = int(os.environ.get("FLOW2API_PURGE_INTERVAL_S", "300"))
# Public links in API responses: https://{host}/video/{id}
# Priority: Host learned from tunnel/proxy request > FLOW2API_PUBLIC_BASE_URL > default fallback.
_PUBLIC_BASE_URL_ENV = os.environ.get("FLOW2API_PUBLIC_BASE_URL", "").strip().rstrip("/")
_PUBLIC_BASE_URL_DEFAULT = os.environ.get(
    "FLOW2API_PUBLIC_BASE_URL_DEFAULT", "https://flow2.viettheo.site"
).strip().rstrip("/")
_learned_public_base_url: str = ""

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "[::1]", "::1"})


def _hostname_without_port(host: str) -> str:
    """Strip :port from Host / X-Forwarded-Host (keep [ipv6] form)."""
    h = str(host or "").strip().lower()
    if not h:
        return ""
    if h.startswith("["):
        end = h.find("]")
        if end != -1:
            return h[: end + 1]
        return h
    # hostname:port or ipv4:port — not bare ipv6 (no brackets, multiple colons).
    if h.count(":") == 1:
        return h.rsplit(":", 1)[0]
    return h


def _is_public_hostname(host: str) -> bool:
    h = _hostname_without_port(host)
    if not h or h in _LOCAL_HOSTS:
        return False
    if h.endswith(".localhost"):
        return False
    # Bare IPv4 / bracketed IPv6 — not a public tunnel hostname.
    if h.replace(".", "").isdigit():
        return False
    if h.startswith("[") and "]" in h:
        return False
    return True


def _scheme_from_cf_visitor(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        import json

        data = json.loads(raw)
        scheme = str(data.get("scheme") or "").strip().lower()
        if scheme in ("http", "https"):
            return scheme
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass
    return ""


def learn_public_base_url_from_headers(headers: Any) -> None:
    """Remember public origin from Cloudflare Tunnel / reverse-proxy headers."""
    global _learned_public_base_url

    get = headers.get if hasattr(headers, "get") else lambda _k, _d="": ""

    host = str(
        get("x-forwarded-host") or get("host") or ""
    ).split(",")[0].strip()
    if not _is_public_hostname(host):
        return

    proto = str(
        get("x-forwarded-proto")
        or _scheme_from_cf_visitor(get("cf-visitor"))
        or "https"
    ).split(",")[0].strip().lower()
    if proto not in ("http", "https"):
        proto = "https"

    base = f"{proto}://{host}".rstrip("/")
    if base == _learned_public_base_url:
        return
    _learned_public_base_url = base
    logging.getLogger(__name__).info("Learned public base URL from request: %s", base)


def get_public_base_url() -> str:
    if _learned_public_base_url:
        return _learned_public_base_url
    if _PUBLIC_BASE_URL_ENV:
        return _PUBLIC_BASE_URL_ENV
    return _PUBLIC_BASE_URL_DEFAULT


# Backward-compatible name — call get_public_base_url() for fresh value.
PUBLIC_BASE_URL = get_public_base_url()
# How long generated outputs stay on disk (default 6 hours).
MEDIA_STORE_TTL_S = int(os.environ.get("FLOW2API_MEDIA_STORE_TTL_S", str(6 * 3600)))

# Post-generation watermark gateway.
# Images: Erasio reverse-blend (all-tool remove-flow-watermark).
# Videos: OpenMark fixed-region inpaint/crop (unchanged).
# After media is cached, files are cleaned in-place before status=done.
# Client URLs (/image/{id}, /video/{id}) and result shape stay the same.
WATERMARK_CLEAN_ENABLED = os.environ.get("FLOW2API_WATERMARK_CLEAN", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
# Video: "inpaint" (Telea on logo rectangle) or "crop" (trim right/bottom + scale).
WATERMARK_VIDEO_MODE = os.environ.get("FLOW2API_WATERMARK_VIDEO_MODE", "crop").strip().lower()
# Normalized right,bottom crop when mode=crop (Flow/Veo edge logo default).
WATERMARK_VIDEO_CROP = os.environ.get("FLOW2API_WATERMARK_VIDEO_CROP", "0.035,0.034").strip()
# Keep original file if cleaner fails (recommended for production).
WATERMARK_FAIL_SOFT = os.environ.get("FLOW2API_WATERMARK_FAIL_SOFT", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
WATERMARK_STRIP_IMAGE_METADATA = os.environ.get(
    "FLOW2API_WATERMARK_STRIP_METADATA", "1"
).strip().lower() in ("1", "true", "yes", "on")

VIDEOS_DIR = STORAGE_DIR / "videos"
INPUTS_DIR = STORAGE_DIR / "inputs"
OUTPUTS_DIR = STORAGE_DIR / "outputs"
VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
INPUTS_DIR.mkdir(parents=True, exist_ok=True)
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
STORAGE_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
