"""Flow2API configuration (mirrors packaged agent env vars)."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
APP_ROOT = Path(os.environ.get("FLOW2API_ROOT", ROOT))

HTTP_HOST = os.environ.get("FLOW2API_HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.environ.get("FLOW2API_HTTP_PORT", "1994"))
# Auto-restart khi sửa code (uvicorn --reload). Bật: set FLOW2API_RELOAD=1
RELOAD = os.environ.get("FLOW2API_RELOAD", "0").strip().lower() in ("1", "true", "yes", "on")
WS_HOST = os.environ.get("FLOW2API_WS_HOST", "127.0.0.1")
WS_PORT = int(os.environ.get("FLOW2API_EXT_WS_PORT", "1609"))

DB_PATH = Path(os.environ.get("FLOW2API_DB", APP_ROOT / "storage" / "flow2api.db"))
STORAGE_DIR = Path(os.environ.get("FLOW2API_STORAGE", APP_ROOT / "storage"))
FRONTEND_DIR = Path(os.environ.get("FLOW2API_FRONTEND", APP_ROOT.parent / "frontend"))

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
IMAGE_POLL_MAX = int(os.environ.get("FLOW2API_IMAGE_POLL_MAX", "90"))
VIDEO_POLL_MAX = int(os.environ.get("FLOW2API_VIDEO_POLL_MAX", "240"))
VIDEO_POLL_MEDIA_MAX = int(os.environ.get("FLOW2API_VIDEO_POLL_MEDIA_MAX", "20"))
RECAPTCHA_RETRY_MAX = int(os.environ.get("FLOW2API_RECAPTCHA_RETRY_MAX", "5"))
PROFILE_DEFAULT_ERROR_COOLDOWN_S = int(
    os.environ.get("FLOW2API_PROFILE_DEFAULT_ERROR_COOLDOWN_S", "600")
)
PROFILE_POST_CLEAR_WAIT_S = int(
    os.environ.get("FLOW2API_PROFILE_POST_CLEAR_WAIT_S", "10")
)
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
WORKER_NUDGE_INTERVAL_S = int(os.environ.get("FLOW2API_WORKER_NUDGE_INTERVAL_S", "120"))
WORKER_NUDGE_STUCK_S = int(os.environ.get("FLOW2API_WORKER_NUDGE_STUCK_S", "120"))
DEFAULT_PROFILE_CLEAR_INTERVAL_S = int(
    os.environ.get("FLOW2API_PROFILE_CLEAR_INTERVAL_S", "5")
)
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

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "[::1]"})


def _is_public_hostname(host: str) -> bool:
    h = str(host or "").strip().lower()
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

VIDEOS_DIR = STORAGE_DIR / "videos"
INPUTS_DIR = STORAGE_DIR / "inputs"
OUTPUTS_DIR = STORAGE_DIR / "outputs"
VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
INPUTS_DIR.mkdir(parents=True, exist_ok=True)
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
STORAGE_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
