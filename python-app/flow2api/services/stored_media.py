"""Persist generated media locally and expose public links (TTL configurable)."""
from __future__ import annotations

import base64
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any, Optional

import httpx

from flow2api.config import (
    HTTP_HANDLER_TIMEOUT_S,
    MEDIA_STORE_TTL_S,
    OUTPUTS_DIR,
    PUBLIC_BASE_URL,
    VIDEOS_DIR,
)

logger = logging.getLogger(__name__)

_MEDIA_ID_RE = re.compile(r"/media/([^/?#]+)")
_URL_LIKE_PREFIXES = ("http://", "https://", "/", "data:")


def _safe_request_id(request_id: str) -> bool:
    rid = str(request_id or "").strip()
    return bool(rid) and ".." not in rid and "/" not in rid and "\\" not in rid


def public_video_url(request_id: str, index: int = 0) -> str:
    if index <= 0:
        return f"{PUBLIC_BASE_URL}/video/{request_id}"
    return f"{PUBLIC_BASE_URL}/video/{request_id}/{index}"


def public_image_url(request_id: str, index: int = 0) -> str:
    if index <= 0:
        return f"{PUBLIC_BASE_URL}/image/{request_id}"
    return f"{PUBLIC_BASE_URL}/image/{request_id}/{index}"


def output_dir(request_id: str) -> Path:
    return OUTPUTS_DIR / request_id


def local_output_path(request_id: str, filename: str) -> str:
    return f"/outputs/{request_id}/{filename}"


def _extract_media_id(url: str) -> Optional[str]:
    m = _MEDIA_ID_RE.search(str(url or ""))
    return m.group(1) if m else None


def _is_probably_pure_base64(value: str) -> bool:
    s = value.strip()
    if len(s) < 48:
        return False
    return not s.startswith(_URL_LIKE_PREFIXES)


def _decode_image_bytes(value: str) -> Optional[tuple[bytes, str]]:
    s = str(value or "").strip()
    if not s:
        return None
    if s.startswith("data:") and "," in s:
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


def _mime_to_ext(mime: str) -> str:
    if mime == "image/png":
        return "png"
    if mime == "image/webp":
        return "webp"
    return "jpg"


async def _fetch_url_bytes(url: str) -> Optional[bytes]:
    try:
        cap = max(10.0, min(120.0, float(HTTP_HANDLER_TIMEOUT_S or 25) * 4))
        async with httpx.AsyncClient(timeout=cap, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code == 200 and resp.content:
                return resp.content
    except Exception as exc:
        logger.warning("fetch media bytes failed %s: %s", url[:80], exc)
    return None


def _read_cached_video_bytes(media_id: str) -> Optional[bytes]:
    path = VIDEOS_DIR / f"{media_id}.mp4"
    if path.is_file():
        return path.read_bytes()
    return None


async def _video_bytes_from_source(url: str, media_id: str = "") -> Optional[bytes]:
    u = str(url or "").strip()
    mid = media_id or (_extract_media_id(u) or "")
    if mid:
        local = _read_cached_video_bytes(mid)
        if local:
            return local
    if u.startswith("/media/") and mid:
        local = _read_cached_video_bytes(mid)
        if local:
            return local
    if u.startswith(("http://", "https://")):
        return await _fetch_url_bytes(u)
    if mid:
        from flow2api.services.flow_client import get_flow_client
        from flow2api.services.flow_sdk import try_fetch_media_video_url

        client = get_flow_client()
        if client.connected:
            try:
                fetched = await try_fetch_media_video_url(client, mid)
            except Exception as exc:
                logger.warning("get_media video fetch failed %s: %s", mid[:12], exc)
                fetched = None
            if fetched:
                if fetched.startswith("/media/"):
                    return _read_cached_video_bytes(mid)
                if fetched.startswith(("http://", "https://")):
                    return await _fetch_url_bytes(fetched)
    return None


async def _image_bytes_from_source(url: str, media_id: str = "") -> Optional[tuple[bytes, str]]:
    decoded = _decode_image_bytes(url)
    if decoded:
        return decoded
    u = str(url or "").strip()
    mid = media_id or (_extract_media_id(u) or "")
    if u.startswith(("http://", "https://")):
        data = await _fetch_url_bytes(u)
        if data:
            mime = (
                "image/png"
                if data[:8] == b"\x89PNG\r\n\x1a\n"
                else "image/jpeg"
            )
            return data, mime
    if mid:
        from flow2api.services.flow_client import get_flow_client
        from flow2api.services.flow_sdk import parse_get_media_image

        client = get_flow_client()
        if client.connected:
            try:
                resp = await client.get_media(mid)
                remote, raw, mime = parse_get_media_image(resp if isinstance(resp, dict) else {})
                if raw:
                    return raw, mime
                if remote:
                    data = await _fetch_url_bytes(remote)
                    if data:
                        return data, mime
            except Exception as exc:
                logger.warning("get_media image fetch failed %s: %s", mid[:12], exc)
    return None


def _collect_video_sources(result: dict[str, Any]) -> list[tuple[str, str]]:
    sources: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(url: str = "", media_id: str = "") -> None:
        u = str(url or "").strip()
        mid = str(media_id or "").strip() or (_extract_media_id(u) or "")
        key = mid or u
        if not key or key in seen:
            return
        seen.add(key)
        sources.append((u, mid))

    for u in result.get("video_urls") or []:
        add(str(u))
    if isinstance(result.get("Link"), str):
        add(result["Link"])
    for u in result.get("local_files") or []:
        add(str(u))
    for mid in result.get("media_ids") or []:
        add(f"/media/{mid}", str(mid))

    return sources


def _collect_image_sources(result: dict[str, Any]) -> list[tuple[str, str]]:
    sources: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(url: str = "", media_id: str = "") -> None:
        u = str(url or "").strip()
        mid = str(media_id or "").strip() or (_extract_media_id(u) or "")
        key = mid or u
        if not key or key in seen:
            return
        seen.add(key)
        sources.append((u, mid))

    for u in result.get("image_urls") or []:
        add(str(u))
    for entry in result.get("media_entries") or []:
        if not isinstance(entry, dict):
            continue
        add(
            str(entry.get("url") or entry.get("local_url") or entry.get("local_path") or ""),
            str(entry.get("media_id") or entry.get("mediaId") or ""),
        )
    media_ids = result.get("media_ids") or []
    image_urls = result.get("image_urls") or []
    for idx, mid in enumerate(media_ids):
        url = image_urls[idx] if idx < len(image_urls) else ""
        add(str(url), str(mid))

    return sources


async def _persist_videos(request_id: str, result: dict[str, Any]) -> list[str]:
    sources = _collect_video_sources(result)
    if not sources:
        return []

    out_dir = output_dir(request_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []

    for idx, (url, mid) in enumerate(sources):
        data = await _video_bytes_from_source(url, mid)
        if not data:
            continue
        filename = "video.mp4" if len(sources) == 1 else f"{idx}.mp4"
        (out_dir / filename).write_bytes(data)
        saved.append(local_output_path(request_id, filename))

    return saved


async def _persist_images(request_id: str, result: dict[str, Any]) -> list[str]:
    sources = _collect_image_sources(result)
    if not sources:
        return []

    out_dir = output_dir(request_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []

    for idx, (url, mid) in enumerate(sources):
        decoded = await _image_bytes_from_source(url, mid)
        if not decoded:
            continue
        raw, mime = decoded
        ext = _mime_to_ext(mime)
        filename = f"{idx}.{ext}"
        (out_dir / filename).write_bytes(raw)
        saved.append(local_output_path(request_id, filename))

    return saved


async def persist_task_result(
    request_id: str,
    result: dict[str, Any],
    task_type: str,
) -> dict[str, Any]:
    """Save media to disk and rewrite result fields as public links."""
    if not isinstance(result, dict) or not _safe_request_id(request_id):
        return result

    out = dict(result)
    is_video = "video" in str(task_type or "").lower()

    try:
        if is_video:
            local_paths = await _persist_videos(request_id, out)
            if local_paths:
                links = [public_video_url(request_id, i) for i in range(len(local_paths))]
                out["Link"] = links[0]
                out["Local"] = links[0]
                out["video_urls"] = links
                out["local_files"] = local_paths
        else:
            local_paths = await _persist_images(request_id, out)
            if local_paths:
                links = [public_image_url(request_id, i) for i in range(len(local_paths))]
                out["image_urls"] = links
                out["Link"] = links[0]
                out["Local"] = links[0]
                out["local_files"] = local_paths
    except Exception as exc:
        logger.warning("persist_task_result failed %s: %s", request_id[:12], exc)

    return out


def resolve_stored_video_path(request_id: str, index: int = 0) -> Optional[Path]:
    if not _safe_request_id(request_id):
        return None
    d = output_dir(request_id)
    if not d.is_dir():
        return None
    single = d / "video.mp4"
    if index <= 0 and single.is_file():
        return single
    numbered = d / f"{index}.mp4"
    if numbered.is_file():
        return numbered
    mp4s = sorted(d.glob("*.mp4"))
    if mp4s and 0 <= index < len(mp4s):
        return mp4s[index]
    return None


def resolve_stored_image_path(request_id: str, index: int = 0) -> Optional[Path]:
    if not _safe_request_id(request_id):
        return None
    d = output_dir(request_id)
    if not d.is_dir():
        return None
    exts = {".jpg", ".jpeg", ".png", ".webp"}
    images = sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() in exts)
    if images and 0 <= index < len(images):
        return images[index]
    return None


def delete_output_dir(request_id: str) -> None:
    if not _safe_request_id(request_id):
        return
    dir_path = output_dir(request_id)
    if not dir_path.is_dir():
        return
    try:
        shutil.rmtree(dir_path)
        logger.info("purged output dir %s", request_id[:12])
    except OSError as exc:
        logger.warning("failed to delete output dir %s: %s", request_id[:12], exc)


def purge_expired_outputs() -> int:
    """Remove output folders older than MEDIA_STORE_TTL_S."""
    ttl = max(60, int(MEDIA_STORE_TTL_S or 6 * 3600))
    cutoff = time.time() - ttl
    deleted = 0
    if not OUTPUTS_DIR.is_dir():
        return 0

    for entry in OUTPUTS_DIR.iterdir():
        if not entry.is_dir():
            continue
        try:
            mtimes = [f.stat().st_mtime for f in entry.rglob("*") if f.is_file()]
            mtime = max(mtimes) if mtimes else entry.stat().st_mtime
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        try:
            shutil.rmtree(entry)
            deleted += 1
            logger.info("purged expired output %s", entry.name[:12])
        except OSError as exc:
            logger.warning("failed to purge output %s: %s", entry.name[:12], exc)
    return deleted
