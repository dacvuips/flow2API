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
        out["result"] = slim_result_for_list(result)
    return out
