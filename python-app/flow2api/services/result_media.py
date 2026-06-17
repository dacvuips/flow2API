"""Embed local/remote media as pure base64 in API responses (no data: URI prefix)."""
from __future__ import annotations

import base64
import logging
import re
from typing import Any, Optional

import httpx

from flow2api.config import HTTP_HANDLER_TIMEOUT_S, VIDEOS_DIR

logger = logging.getLogger(__name__)

_MEDIA_ID_RE = re.compile(r"/media/([^/?#]+)")
_URL_LIKE_PREFIXES = ("http://", "https://", "/", "data:")


def _extract_media_id(url: str) -> Optional[str]:
    m = _MEDIA_ID_RE.search(url)
    return m.group(1) if m else None


def _is_probably_pure_base64(value: str) -> bool:
    s = value.strip()
    if len(s) < 48:
        return False
    if s.startswith(_URL_LIKE_PREFIXES):
        return False
    return True


def _strip_data_uri(value: str) -> str:
    if value.startswith("data:") and "," in value:
        return value.split(",", 1)[1]
    return value


def _read_local_video_bytes(media_id: str) -> Optional[bytes]:
    path = VIDEOS_DIR / f"{media_id}.mp4"
    if path.is_file():
        return path.read_bytes()
    return None


async def _fetch_url_bytes(url: str) -> Optional[bytes]:
    try:
        cap = max(10.0, min(60.0, float(HTTP_HANDLER_TIMEOUT_S or 25) * 2))
        async with httpx.AsyncClient(timeout=cap, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code == 200 and resp.content:
                return resp.content
    except Exception as exc:
        logger.warning("fetch media bytes failed %s: %s", url[:80], exc)
    return None


async def source_to_pure_base64(url: str) -> Optional[str]:
    if not url or not isinstance(url, str):
        return None
    u = url.strip()
    if not u:
        return None
    if _is_probably_pure_base64(u):
        return _strip_data_uri(u)

    media_id = _extract_media_id(u)
    if media_id:
        local = _read_local_video_bytes(media_id)
        if local:
            return base64.b64encode(local).decode("ascii")

    if u.startswith(("http://", "https://")):
        data = await _fetch_url_bytes(u)
        if data:
            return base64.b64encode(data).decode("ascii")
        return None

    if u.startswith("/media/"):
        mid = _extract_media_id(u)
        if mid:
            local = _read_local_video_bytes(mid)
            if local:
                return base64.b64encode(local).decode("ascii")
    return None


async def _urls_to_base64(urls: Any) -> list[str]:
    if not isinstance(urls, list):
        return []
    out: list[str] = []
    for item in urls:
        if not item:
            continue
        b64 = await source_to_pure_base64(str(item))
        if b64:
            out.append(b64)
    return out


async def _media_ids_to_base64(media_ids: Any) -> list[str]:
    if not isinstance(media_ids, list):
        return []
    out: list[str] = []
    for mid in media_ids:
        if not mid:
            continue
        local = _read_local_video_bytes(str(mid))
        if local:
            out.append(base64.b64encode(local).decode("ascii"))
    return out


async def _media_id_image_to_base64(media_id: str) -> Optional[str]:
    from flow2api.services.flow_client import get_flow_client
    from flow2api.services.flow_sdk import parse_get_media_image

    client = get_flow_client()
    if not client.connected:
        return None
    try:
        resp = await client.get_media(media_id)
        url, raw, _mime = parse_get_media_image(resp if isinstance(resp, dict) else {})
        if raw:
            return base64.b64encode(raw).decode("ascii")
        if url:
            data = await _fetch_url_bytes(url)
            if data:
                return base64.b64encode(data).decode("ascii")
    except Exception as exc:
        logger.warning("get_media image embed failed %s: %s", media_id[:12], exc)
    return None


async def _image_media_ids_to_base64(media_ids: Any) -> list[str]:
    if not isinstance(media_ids, list):
        return []
    out: list[str] = []
    for mid in media_ids:
        if not mid:
            continue
        b64 = await _media_id_image_to_base64(str(mid))
        if b64:
            out.append(b64)
    return out


async def embed_result_base64(result: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(result, dict):
        return result
    out = dict(result)

    video_sources: list[str] = []
    if isinstance(out.get("video_urls"), list):
        video_sources.extend(str(u) for u in out["video_urls"] if u)
    if isinstance(out.get("Link"), str) and out["Link"]:
        video_sources.append(out["Link"])
    if isinstance(out.get("local_files"), list):
        video_sources.extend(str(u) for u in out["local_files"] if u)
    if not video_sources and isinstance(out.get("media_ids"), list):
        video_b64 = await _media_ids_to_base64(out["media_ids"])
    else:
        video_b64 = await _urls_to_base64(video_sources)

    image_b64 = await _urls_to_base64(out.get("image_urls"))
    if not image_b64 and isinstance(out.get("media_ids"), list):
        image_b64 = await _image_media_ids_to_base64(out["media_ids"])

    if video_b64:
        out["video_urls"] = video_b64
    else:
        out.pop("video_urls", None)
    if image_b64:
        out["image_urls"] = image_b64
    else:
        out.pop("image_urls", None)

    for key in ("local_files", "Link"):
        out.pop(key, None)

    entries = out.get("media_entries")
    if isinstance(entries, list):
        slim_entries: list[Any] = []
        for entry in entries:
            if isinstance(entry, dict):
                item = {k: v for k, v in entry.items() if k not in ("url", "local_url", "local_path")}
                slim_entries.append(item)
            else:
                slim_entries.append(entry)
        out["media_entries"] = slim_entries

    return out


def slim_result_for_list(result: Any) -> Any:
    if not isinstance(result, dict):
        return result
    out = dict(result)
    for key in ("video_urls", "image_urls", "local_files", "Link", "raw"):
        out.pop(key, None)
    return out


def _is_local_served_url(url: str) -> bool:
    from flow2api.config import PUBLIC_BASE_URL

    u = str(url or "").strip()
    if not u:
        return True
    if u.startswith(("/outputs/", "/video/", "/image/", "/media/", "/inputs/")):
        return True
    if PUBLIC_BASE_URL and u.startswith(PUBLIC_BASE_URL):
        return True
    return False


def result_for_external_api(result: dict[str, Any]) -> dict[str, Any]:
    """API payload for external callers — publisher HTTPS URLs only."""
    from flow2api.services.stored_media import normalize_publisher_urls

    out = normalize_publisher_urls(dict(result))
    for key in ("local_files", "Local", "raw"):
        out.pop(key, None)

    for key in ("image_urls", "video_urls"):
        urls = out.get(key)
        if not isinstance(urls, list):
            continue
        filtered = [str(u) for u in urls if not _is_local_served_url(str(u))]
        if filtered:
            out[key] = filtered
        else:
            out.pop(key, None)

    link = out.get("Link")
    if _is_local_served_url(str(link or "")):
        out.pop("Link", None)

    entries = out.get("media_entries")
    if isinstance(entries, list):
        slim: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            url = entry.get("url")
            if not url or _is_local_served_url(str(url)):
                continue
            if not str(url).startswith(("http://", "https://")):
                continue
            item: dict[str, Any] = {
                "url": str(url),
                "kind": entry.get("kind") or entry.get("mediaType") or "image",
            }
            mid = entry.get("media_id") or entry.get("mediaId")
            if mid:
                item["media_id"] = mid
            slim.append(item)
        if slim:
            out["media_entries"] = slim
        else:
            out.pop("media_entries", None)

    if isinstance(out.get("media_ids"), list):
        out["media_ids"] = [str(m) for m in out["media_ids"] if str(m).strip()]
        if not out["media_ids"]:
            out.pop("media_ids", None)

    pid = str(out.get("project_id") or "").strip()
    if pid:
        out["project_id"] = pid
    else:
        out.pop("project_id", None)

    return out


def _local_video_exists(media_id: str) -> bool:
    return (VIDEOS_DIR / f"{media_id}.mp4").is_file()


def _list_preview_url_allowed(url: str, kind: str = "") -> bool:
    """Skip bare /media/{uuid} placeholders that are not cached locally."""
    u = str(url or "").strip()
    if not u:
        return False
    if u.startswith(("http://", "https://", "/inputs/", "/outputs/", "/video/", "/image/")):
        return True
    mid = _extract_media_id(u)
    if mid and u.startswith("/media/"):
        kind_l = str(kind or "").lower()
        if kind_l == "video" or u.endswith(".mp4"):
            return _local_video_exists(mid)
        return False
    return True


def preview_items_from_result(result: dict, task_type: str = "") -> list[dict[str, str]]:
    """Lightweight media previews for task list (URLs only, no base64)."""
    if not isinstance(result, dict):
        return []
    is_video = "video" in str(task_type or "").lower()
    default_kind = "video" if is_video else "image"
    items: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    seen_mids: set[str] = set()

    def add(url: str, kind: str = "", media_id: str = "", *, local: bool = False) -> None:
        u = str(url or "").strip()
        if not u:
            return
        if u.startswith("data:") or _is_probably_pure_base64(u):
            return
        mid = str(media_id or "").strip() or (_extract_media_id(u) or "")
        if mid and mid in seen_mids:
            return
        if u in seen_urls:
            return
        k = kind or default_kind
        if not _list_preview_url_allowed(u, k):
            return
        seen_urls.add(u)
        if mid:
            seen_mids.add(mid)
        item: dict[str, str] = {"url": u, "kind": k}
        if local or u.startswith(("/outputs/", "/video/", "/image/")):
            item["local"] = "1"
        if mid and _local_video_exists(mid):
            item["media_id"] = mid
            item["local"] = "1"
        elif mid and u.startswith(("http://", "https://")):
            item["media_id"] = mid
        items.append(item)

    local_files = [str(u) for u in (result.get("local_files") or []) if str(u or "").strip()]
    for u in local_files:
        add(u, default_kind, local=True)
    if local_files:
        return items[:1]

    image_urls = result.get("image_urls") or []
    media_ids = result.get("media_ids") or []
    for i, u in enumerate(image_urls):
        mid = str(media_ids[i] if i < len(media_ids) else "").strip()
        add(str(u), "image", mid)
    for u in result.get("video_urls") or []:
        add(str(u), "video")
    if result.get("Link"):
        add(str(result["Link"]), default_kind)

    if not items:
        for entry in result.get("media_entries") or []:
            if not isinstance(entry, dict):
                continue
            mid = str(entry.get("media_id") or entry.get("mediaId") or "").strip()
            kind_raw = str(entry.get("kind") or entry.get("mediaType") or default_kind).lower()
            kind = "video" if "video" in kind_raw else "image"
            if entry.get("url"):
                add(str(entry["url"]), kind, mid)
            elif entry.get("local_url"):
                add(str(entry["local_url"]), kind, mid)
            elif entry.get("local_path"):
                add(str(entry["local_path"]), kind, mid)
            elif mid:
                add(f"/media/{mid}", kind, mid)

        for mid in result.get("media_ids") or []:
            mid_s = str(mid or "").strip()
            if mid_s:
                add(f"/media/{mid_s}", default_kind, mid_s)

    return items[:1]


def _decode_image_bytes(b64: str) -> tuple[bytes, str] | None:
    s = str(b64 or "").strip()
    if not s:
        return None
    if s.startswith("data:"):
        header, _, payload = s.partition(",")
        mime = "image/jpeg"
        lower = header.lower()
        if "png" in lower:
            mime = "image/png"
        elif "webp" in lower:
            mime = "image/webp"
        try:
            return base64.b64decode(payload), mime
        except Exception:
            return None
    if _is_probably_pure_base64(s):
        try:
            raw = base64.b64decode(s)
            mime = (
                "image/png"
                if raw[:8] == b"\x89PNG\r\n\x1a\n"
                else "image/jpeg"
            )
            return raw, mime
        except Exception:
            return None
    return None


def persist_input_previews(
    request_id: str,
    image_base64s: list[str],
    *,
    max_items: int = 3,
) -> list[str]:
    """Save input images locally for lightweight list previews."""
    from flow2api.config import INPUTS_DIR

    if not image_base64s:
        return []
    out_dir = INPUTS_DIR / request_id
    urls: list[str] = []
    for idx, b64 in enumerate(image_base64s[:max_items]):
        decoded = _decode_image_bytes(b64)
        if not decoded:
            continue
        raw, mime = decoded
        ext = (
            "png"
            if mime == "image/png"
            else ("webp" if mime == "image/webp" else "jpg")
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{idx}.{ext}"
        (out_dir / filename).write_bytes(raw)
        urls.append(f"/inputs/{request_id}/{filename}")
    return urls


def load_input_base64s_from_storage(request_id: str, *, max_items: int = 3) -> list[str]:
    """Restore input images from local preview files for manual retry."""
    from flow2api.config import INPUTS_DIR

    folder = INPUTS_DIR / request_id
    if not folder.is_dir():
        return []
    allowed = {".jpg", ".jpeg", ".png", ".webp"}
    files = sorted(
        (p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in allowed),
        key=lambda p: p.name,
    )
    out: list[str] = []
    for path in files[:max_items]:
        raw = path.read_bytes()
        ext = path.suffix.lower()
        mime = {
            ".png": "image/png",
            ".webp": "image/webp",
        }.get(ext, "image/jpeg")
        b64 = base64.b64encode(raw).decode("ascii")
        out.append(f"data:{mime};base64,{b64}")
    return out


_MEDIA_REF_KEYS = ("start_media_id", "end_media_id", "reference_media_ids")
_PROFILE_ASSIGN_KEYS = ("profile_id", "profile_label", "profile_email")


def _restore_input_images(params: dict[str, Any], request_id: str) -> dict[str, Any]:
    out = dict(params or {})
    if not (out.get("image_base64s") or out.get("imageBase64s")):
        restored = load_input_base64s_from_storage(request_id)
        if restored:
            out["image_base64s"] = restored
    return out


def prepare_params_for_worker_requeue(
    params: dict[str, Any],
    request_id: str,
    *,
    prompt: str = "",
) -> dict[str, Any]:
    """Requeue same task id — refresh Google media refs, restore input images."""
    out = dict(params or {})
    for key in _MEDIA_REF_KEYS:
        out.pop(key, None)
    for key in _PROFILE_ASSIGN_KEYS:
        out.pop(key, None)
    out.pop("running_started_at", None)
    out.pop("retry_exclude_profile_id", None)
    if prompt and not str(out.get("prompt") or "").strip():
        out["prompt"] = prompt
    return _restore_input_images(out, request_id)


def prepare_params_for_manual_retry(params: dict[str, Any], request_id: str) -> dict[str, Any]:
    """Reset stale Google media refs and restore upload bytes for retry."""
    out = dict(params or {})
    for key in (
        *_MEDIA_REF_KEYS,
        "recaptcha_retry_count",
        "get_media_404_retry_count",
        "upload_internal_retry_count",
        "extension_timeout_retry_count",
        "prominent_people_retry_count",
        "invalid_argument_retry_count",
        "trpc_401_retry_count",
        "retry_not_before",
        "running_started_at",
        "running_timeout_retry_count",
        "retry_exclude_profile_id",
    ):
        out.pop(key, None)
    for key in _PROFILE_ASSIGN_KEYS:
        out.pop(key, None)
    return _restore_input_images(out, request_id)


def input_preview_items_from_params(params: dict) -> list[dict[str, str]]:
    """Lightweight input previews for task list (URLs only)."""
    if not isinstance(params, dict):
        return []
    items: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(url: str, media_id: str = "") -> None:
        u = str(url or "").strip()
        if not u or u in seen:
            return
        if u.startswith("data:") or _is_probably_pure_base64(u):
            return
        seen.add(u)
        item: dict[str, str] = {"url": u, "kind": "image"}
        mid = str(media_id or "").strip() or (_extract_media_id(u) or "")
        if mid:
            item["media_id"] = mid
        items.append(item)

    preview_urls = [
        str(u).strip()
        for u in (params.get("input_preview_urls") or [])
        if str(u or "").strip()
    ]
    for u in preview_urls:
        add(u)

    return items[:3]


async def with_base64_media(
    payload: dict[str, Any],
    *,
    embed: bool = True,
) -> dict[str, Any]:
    out = dict(payload)
    result = out.get("result")
    if not isinstance(result, dict):
        return out
    if embed:
        out["result"] = await embed_result_base64(result)
    else:
        out["result"] = result_for_external_api(result)
    return out
