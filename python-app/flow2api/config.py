"""Flow2API configuration (mirrors packaged agent env vars)."""
from __future__ import annotations

import os
from pathlib import Path

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
RECAPTCHA_RETRY_MAX = int(os.environ.get("FLOW2API_RECAPTCHA_RETRY_MAX", "10"))
WORKER_MAX_CONCURRENT = int(os.environ.get("FLOW2API_MAX_CONCURRENT", "1"))
WORKER_TASK_STAGGER_S = float(os.environ.get("FLOW2API_TASK_STAGGER_S", "0"))
ACTIVITY_LIST_LIMIT = int(os.environ.get("FLOW2API_ACTIVITY_LIST_LIMIT", "100"))
ACTIVITY_PAGE_SIZE = int(os.environ.get("FLOW2API_ACTIVITY_PAGE_SIZE", "20"))
TASK_RUNNING_TIMEOUT_S = int(os.environ.get("FLOW2API_TASK_RUNNING_TIMEOUT_S", "300"))
TASK_RUNNING_TIMEOUT_MAX_RETRIES = int(
    os.environ.get("FLOW2API_TASK_RUNNING_TIMEOUT_MAX_RETRIES", "3")
)
WORKER_NUDGE_INTERVAL_S = int(os.environ.get("FLOW2API_WORKER_NUDGE_INTERVAL_S", "120"))
WORKER_NUDGE_STUCK_S = int(os.environ.get("FLOW2API_WORKER_NUDGE_STUCK_S", "120"))
# Fail HTTP handlers before Cloudflare 524 (~100s); return JSON 503 instead.
HTTP_HANDLER_TIMEOUT_S = float(os.environ.get("FLOW2API_HTTP_HANDLER_TIMEOUT_S", "25"))
HEALTH_CACHE_TTL_S = float(os.environ.get("FLOW2API_HEALTH_CACHE_TTL_S", "3"))
PURGE_INTERVAL_S = int(os.environ.get("FLOW2API_PURGE_INTERVAL_S", "300"))

VIDEOS_DIR = STORAGE_DIR / "videos"
INPUTS_DIR = STORAGE_DIR / "inputs"
VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
INPUTS_DIR.mkdir(parents=True, exist_ok=True)
STORAGE_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
