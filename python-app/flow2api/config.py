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
# ~30 phút với POLL_INTERVAL_S mặc định 2.5s
_DEFAULT_POLL_MAX = max(90, int(1800 / max(0.5, POLL_INTERVAL_S)))
IMAGE_POLL_MAX = int(os.environ.get("FLOW2API_IMAGE_POLL_MAX", str(_DEFAULT_POLL_MAX)))
VIDEO_POLL_MAX = int(os.environ.get("FLOW2API_VIDEO_POLL_MAX", str(_DEFAULT_POLL_MAX)))
VIDEO_POLL_MEDIA_MAX = int(os.environ.get("FLOW2API_VIDEO_POLL_MEDIA_MAX", "20"))
RECAPTCHA_RETRY_MAX = int(os.environ.get("FLOW2API_RECAPTCHA_RETRY_MAX", "10"))
HTTP_404_MAX_ATTEMPTS = int(os.environ.get("FLOW2API_HTTP_404_MAX_ATTEMPTS", "3"))
HTTP_404_POLICY_ERROR_MSG = (
    "Vui lòng kiểm tra lại prompt và ảnh đã vi phạm chinh sách!"
)
WORKER_MAX_CONCURRENT = int(os.environ.get("FLOW2API_MAX_CONCURRENT", "1"))
WORKER_TASK_STAGGER_S = float(os.environ.get("FLOW2API_TASK_STAGGER_S", "0"))
ACTIVITY_LIST_LIMIT = int(os.environ.get("FLOW2API_ACTIVITY_LIST_LIMIT", "100"))
ACTIVITY_META_LIMIT = int(os.environ.get("FLOW2API_ACTIVITY_META_LIMIT", "10000"))
ACTIVITY_PAGE_SIZE = int(os.environ.get("FLOW2API_ACTIVITY_PAGE_SIZE", "20"))
TASK_RUNNING_TIMEOUT_S = int(os.environ.get("FLOW2API_TASK_RUNNING_TIMEOUT_S", "1800"))
TASK_TIMEOUT_ERROR = "task_timeout_30m"
TASK_TIMEOUT_ERROR_MSG = (
    "Timeout: quá 30 phút không hoàn thành, job đã kết thúc."
)
# Chờ response generate sau khi bấm submit trên UI Playwright (giây).
UI_GENERATION_SUBMIT_TIMEOUT_S = int(
    os.environ.get(
        "FLOW2API_UI_GENERATION_SUBMIT_TIMEOUT_S",
        str(TASK_RUNNING_TIMEOUT_S),
    )
)
WORKER_NUDGE_INTERVAL_S = int(os.environ.get("FLOW2API_WORKER_NUDGE_INTERVAL_S", "120"))
WORKER_NUDGE_STUCK_S = int(os.environ.get("FLOW2API_WORKER_NUDGE_STUCK_S", "120"))
DEFAULT_PROFILE_CLEAR_INTERVAL_S = int(
    os.environ.get("FLOW2API_PROFILE_CLEAR_INTERVAL_S", "300")
)
DEFAULT_PROFILE_403_CACHE_MINUTES = int(
    os.environ.get("FLOW2API_PROFILE_403_CACHE_MINUTES", "30")
)
POST_CLEAR_403_RETRY_MAX = int(os.environ.get("FLOW2API_POST_CLEAR_403_RETRY_MAX", "3"))
POST_CLEAR_403_DELAY_S = int(os.environ.get("FLOW2API_POST_CLEAR_403_DELAY_S", "30"))
POST_SUCCESS_CLEAR_DELAY_S = int(os.environ.get("FLOW2API_POST_SUCCESS_CLEAR_DELAY_S", "20"))
POST_CLEAR_COOLDOWN_S = int(os.environ.get("FLOW2API_POST_CLEAR_COOLDOWN_S", "10"))
# Sau clear data: chờ N giây rồi giả lập lăn chuột (2 xuống + 2 lên) trên tab Flow.
POST_CLEAR_SCROLL_ENABLED = os.environ.get("FLOW2API_POST_CLEAR_SCROLL_ENABLED", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
POST_CLEAR_SCROLL_DELAY_S = int(os.environ.get("FLOW2API_POST_CLEAR_SCROLL_DELAY_S", "10"))
POST_CLEAR_SCROLL_STEPS = int(os.environ.get("FLOW2API_POST_CLEAR_SCROLL_STEPS", "2"))
# Fail HTTP handlers before Cloudflare 524 (~100s); return JSON 503 instead.
HTTP_HANDLER_TIMEOUT_S = float(os.environ.get("FLOW2API_HTTP_HANDLER_TIMEOUT_S", "25"))
HEALTH_CACHE_TTL_S = float(os.environ.get("FLOW2API_HEALTH_CACHE_TTL_S", "3"))
PURGE_INTERVAL_S = int(os.environ.get("FLOW2API_PURGE_INTERVAL_S", "300"))
# Public links in API responses: https://{host}/video/{id}
# Priority: Host learned from tunnel/proxy request > FLOW2API_PUBLIC_BASE_URL > default fallback.
_PUBLIC_BASE_URL_ENV = os.environ.get("FLOW2API_PUBLIC_BASE_URL", "").strip().rstrip("/")
_PUBLIC_BASE_URL_DEFAULT = os.environ.get(
    "FLOW2API_PUBLIC_BASE_URL_DEFAULT", f"http://127.0.0.1:{HTTP_PORT}"
).strip().rstrip("/")
_learned_public_base_url: str = ""

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "[::1]"})


def _host_without_port(host: str) -> str:
    h = str(host or "").strip().lower()
    if not h:
        return ""
    if h.startswith("[") and "]" in h:
        return h.split("]", 1)[0] + "]"
    if ":" in h:
        return h.rsplit(":", 1)[0]
    return h


def _is_public_hostname(host: str) -> bool:
    h = _host_without_port(host)
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
    learned = _learned_public_base_url
    if learned:
        try:
            from urllib.parse import urlparse

            host = _host_without_port(urlparse(learned).hostname or "")
            if host in _LOCAL_HOSTS and learned.lower().startswith("https://"):
                learned = ""
        except Exception:
            pass
    if learned:
        return learned
    if _PUBLIC_BASE_URL_ENV:
        return _PUBLIC_BASE_URL_ENV
    return _PUBLIC_BASE_URL_DEFAULT


# Backward-compatible name — call get_public_base_url() for fresh value.
PUBLIC_BASE_URL = get_public_base_url()
# How long generated outputs stay on disk (default 6 hours).
MEDIA_STORE_TTL_S = int(os.environ.get("FLOW2API_MEDIA_STORE_TTL_S", str(6 * 3600)))

# Playwright UI automation (CDP attach to Chrome profiles launched with --remote-debugging-port)
PLAYWRIGHT_ENABLED = os.environ.get("FLOW2API_PLAYWRIGHT_ENABLED", "0").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
UI_AUTOMATION_ENABLED = os.environ.get("FLOW2API_UI_AUTOMATION", "0").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
# Giai đoạn thử: chỉ upload ảnh + điền prompt, chưa bấm Generate (API gen cũng bị bỏ qua).
UI_PREP_ONLY = os.environ.get("FLOW2API_UI_PREP_ONLY", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
# Delay ngẫu nhiên giữa mỗi thao tác UI Playwright (giây).
UI_ACTION_DELAY_MIN_S = float(os.environ.get("FLOW2API_UI_ACTION_DELAY_MIN_S", "0"))
UI_ACTION_DELAY_MAX_S = float(os.environ.get("FLOW2API_UI_ACTION_DELAY_MAX_S", "1.5"))
# Giả lập chuột di chuyển nhẹ trước khi click (giống người dùng).
UI_MOUSE_NUDGE_ENABLED = os.environ.get("FLOW2API_UI_MOUSE_NUDGE", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
UI_MOUSE_NUDGE_PX = float(os.environ.get("FLOW2API_UI_MOUSE_NUDGE_PX", "10"))
# Chờ ảnh preview trong modal thư viện trước khi bấm "Thêm vào câu lệnh".
UI_UPLOAD_PREVIEW_TIMEOUT_S = float(os.environ.get("FLOW2API_UI_UPLOAD_PREVIEW_TIMEOUT_S", "120"))
# Số lần tự retry upload ảnh qua Playwright khi lỗi.
UI_UPLOAD_RETRY_MAX = int(os.environ.get("FLOW2API_UI_UPLOAD_RETRY_MAX", "3"))
# Sau khi bấm Retry trên UI lỗi upload — chờ trước khi thử lại (giây).
UI_UPLOAD_ERROR_RETRY_WAIT_S = float(
    os.environ.get("FLOW2API_UI_UPLOAD_ERROR_RETRY_WAIT_S", "30")
)
# Số vòng retry nút Refresh trên thẻ 「Không thành công」 (variant x2–x4).
UI_GRID_VARIANT_RETRY_MAX = int(os.environ.get("FLOW2API_UI_GRID_VARIANT_RETRY_MAX", "3"))
# Chờ variant xuất hiện sau submit (ảnh/video x2–x4) — hết giờ trả partial, không restart pipeline.
UI_VARIANT_SETTLE_TIMEOUT_S = int(os.environ.get("FLOW2API_UI_VARIANT_SETTLE_TIMEOUT_S", "60"))
# Trước/sau poll video: gom thêm media_id từ trang Flow — ngắn hơn settle sau submit.
UI_VARIANT_DISCOVER_TIMEOUT_S = int(os.environ.get("FLOW2API_UI_VARIANT_DISCOVER_TIMEOUT_S", "12"))
CDP_BASE_PORT = int(os.environ.get("FLOW2API_CDP_BASE_PORT", "9236"))
# Port CDP cố định cho profile Chrome có tab Flow (Default = 9236).
PLAYWRIGHT_FLOW_CDP_PORT = int(
    os.environ.get("FLOW2API_PLAYWRIGHT_FLOW_CDP_PORT", str(CDP_BASE_PORT))
)
# Profile Chrome mở Flow / Playwright — mở đầu tiên với PLAYWRIGHT_FLOW_CDP_PORT.
FLOW_CHROME_PROFILE = os.environ.get("FLOW2API_FLOW_CHROME_PROFILE", "Default").strip()
CDP_CONNECT_TIMEOUT_S = float(os.environ.get("FLOW2API_CDP_CONNECT_TIMEOUT_S", "15"))
CDP_PROBE_RETRIES = int(os.environ.get("FLOW2API_CDP_PROBE_RETRIES", "10"))
# Tự mở lại Chrome với CDP khi job Playwright không thấy port (sẽ đóng Chrome cũ trước).
CDP_AUTO_LAUNCH = os.environ.get("FLOW2API_CDP_AUTO_LAUNCH", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
CDP_AUTO_LAUNCH_WAIT_S = int(os.environ.get("FLOW2API_CDP_AUTO_LAUNCH_WAIT_S", "120"))
# Host kiểm tra CDP — thử localhost trước (Windows đôi khi localhost ≠ 127.0.0.1 do IPv6).
CDP_PROBE_HOSTS = tuple(
    h.strip()
    for h in os.environ.get("FLOW2API_CDP_PROBE_HOSTS", "localhost,127.0.0.1").split(",")
    if h.strip()
) or ("localhost", "127.0.0.1")
# Chrome 136+ không cho CDP trên User Data mặc định — dùng thư mục riêng (bản sao profile).
CDP_USER_DATA_DIR = Path(
    os.environ.get("FLOW2API_CDP_USER_DATA_DIR", str(STORAGE_DIR / "chrome-cdp-user-data"))
)
# Chrome hoặc Chromium (máy mới thường dùng Chromium).
CHROME_EXECUTABLE = os.environ.get("FLOW2API_CHROME_PATH", "").strip()
CHROME_USER_DATA_DIR = os.environ.get("FLOW2API_CHROME_USER_DATA_DIR", "").strip()
# Mở Chrome CDP thu nhỏ (taskbar) — Playwright vẫn điều khiển qua CDP, không cần cửa sổ DevTools.
CHROME_CDP_START_MINIMIZED = os.environ.get("FLOW2API_CHROME_START_MINIMIZED", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

VIDEOS_DIR = STORAGE_DIR / "videos"
INPUTS_DIR = STORAGE_DIR / "inputs"
OUTPUTS_DIR = STORAGE_DIR / "outputs"
VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
INPUTS_DIR.mkdir(parents=True, exist_ok=True)
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
STORAGE_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
