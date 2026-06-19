"""Persist generated media locally and expose public links (TTL configurable)."""
from __future__ import annotations

import base64
import json
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


def public_media_url(media_id: str) -> str:
    return f"{PUBLIC_BASE_URL}/media/{media_id}"


_STALE_PUBLIC_HOSTS = (
    "https://viettheo.site",
    "http://viettheo.site",
)


def rewrite_public_base_url(url: str) -> str:
    """Fix legacy PUBLIC_BASE_URL host stored in older task results."""
    u = str(url or "").strip()
    if not u or not PUBLIC_BASE_URL:
        return u
    for stale in _STALE_PUBLIC_HOSTS:
        if u == stale or u.startswith(stale + "/"):
            return PUBLIC_BASE_URL + u[len(stale) :]
    return u


def rewrite_result_public_urls(result: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(result, dict):
        return result
    out = dict(result)
    for key in ("video_urls", "image_urls"):
        urls = out.get(key)
        if isinstance(urls, list):
            out[key] = [rewrite_public_base_url(str(u)) for u in urls if str(u or "").strip()]
    if isinstance(out.get("Link"), str):
        out["Link"] = rewrite_public_base_url(out["Link"])
    entries = out.get("media_entries")
    if isinstance(entries, list):
        fixed: list[Any] = []
        for entry in entries:
            if not isinstance(entry, dict):
                fixed.append(entry)
                continue
            item = dict(entry)
            if item.get("url"):
                item["url"] = rewrite_public_base_url(str(item["url"]))
            fixed.append(item)
        out["media_entries"] = fixed
    return out


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
    image_urls = [str(u) for u in (result.get("image_urls") or []) if str(u or "").strip()]
    media_ids = [str(m) for m in (result.get("media_ids") or []) if str(m or "").strip()]
    if image_urls:
        sources: list[tuple[str, str]] = []
        seen: set[str] = set()
        for idx, url in enumerate(image_urls):
            mid = media_ids[idx] if idx < len(media_ids) else (_extract_media_id(url) or "")
            key = mid or url
            if not key or key in seen:
                continue
            seen.add(key)
            if mid:
                seen.add(mid)
            if url:
                seen.add(url)
            sources.append((url, mid))
        return sources

    sources: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(url: str = "", media_id: str = "") -> None:
        u = str(url or "").strip()
        mid = str(media_id or "").strip() or (_extract_media_id(u) or "")
        key = mid or u
        if not key or key in seen:
            return
        seen.add(key)
        if mid:
            seen.add(mid)
        if u:
            seen.add(u)
        sources.append((u, mid))

    for entry in result.get("media_entries") or []:
        if not isinstance(entry, dict):
            continue
        add(
            str(entry.get("url") or entry.get("local_url") or entry.get("local_path") or ""),
            str(entry.get("media_id") or entry.get("mediaId") or ""),
        )
    for mid in media_ids:
        add("", mid)

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


def normalize_publisher_urls(result: dict[str, Any]) -> dict[str, Any]:
    """Keep Google Flow CDN URLs in API fields; drop base64/data-uri placeholders."""
    if not isinstance(result, dict):
        return result
    out = dict(result)
    entries = out.get("media_entries") or []
    if not isinstance(entries, list):
        entries = []

    def _entry_https_url(index: int) -> str:
        if index >= len(entries) or not isinstance(entries[index], dict):
            return ""
        url = entries[index].get("url")
        if url and str(url).startswith(("http://", "https://")):
            return str(url)
        return ""

    image_urls = list(out.get("image_urls") or [])
    if image_urls:
        normalized: list[str] = []
        for idx, raw in enumerate(image_urls):
            url = str(raw or "").strip()
            if url.startswith(("http://", "https://")):
                normalized.append(url)
                continue
            entry_url = _entry_https_url(idx)
            if entry_url:
                normalized.append(entry_url)
        if normalized:
            out["image_urls"] = normalized

    video_urls = list(out.get("video_urls") or [])
    if video_urls:
        normalized = [
            str(url)
            for url in video_urls
            if str(url or "").startswith(("http://", "https://"))
        ]
        if normalized:
            out["video_urls"] = normalized

    link = str(out.get("Link") or "").strip()
    primary = (out.get("video_urls") or out.get("image_urls") or [None])[0]
    if link.startswith(("http://", "https://")):
        out["Link"] = link
    elif primary and str(primary).startswith(("http://", "https://")):
        out["Link"] = str(primary)
    else:
        out.pop("Link", None)

    out.pop("Local", None)
    return out


def _external_https_urls(urls: Any) -> list[str]:
    if not isinstance(urls, list):
        return []
    out: list[str] = []
    for raw in urls:
        u = str(raw or "").strip()
        if u.startswith(("http://", "https://")) and not u.startswith(f"{PUBLIC_BASE_URL}/"):
            out.append(u)
    return out


def apply_video_public_urls(request_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """Expose {PUBLIC_BASE_URL}/video/{request_id} when output MP4 exists on disk."""
    if not _safe_request_id(request_id) or not isinstance(result, dict):
        return result

    out = dict(result)
    out_dir = output_dir(request_id)
    if not resolve_stored_video_path(request_id, 0):
        media_ids = [str(m) for m in (out.get("media_ids") or []) if str(m).strip()]
        for idx, mid in enumerate(media_ids):
            cached = VIDEOS_DIR / f"{mid}.mp4"
            if not cached.is_file():
                continue
            out_dir.mkdir(parents=True, exist_ok=True)
            filename = "video.mp4" if len(media_ids) == 1 else f"{idx}.mp4"
            dest = out_dir / filename
            if not dest.is_file():
                try:
                    shutil.copy2(cached, dest)
                except OSError as exc:
                    logger.warning(
                        "copy cached video failed %s: %s", request_id[:12], exc
                    )
                    continue

    stored = sorted(
        p
        for p in (out_dir.glob("*.mp4") if out_dir.is_dir() else [])
        if p.is_file()
    )
    if not stored:
        single = resolve_stored_video_path(request_id, 0)
        if single:
            stored = [single]

    if not stored:
        return out

    public_urls = [
        public_video_url(request_id, 0 if len(stored) == 1 else idx)
        for idx in range(len(stored))
    ]
    out["video_urls"] = public_urls
    out["Link"] = public_urls[0]
    out["local_files"] = [
        local_output_path(request_id, path.name) for path in stored
    ]

    media_ids = [str(m) for m in (out.get("media_ids") or []) if str(m).strip()]
    entries: list[dict[str, str]] = []
    for idx, pub in enumerate(public_urls):
        entry: dict[str, str] = {"url": pub, "kind": "video"}
        if idx < len(media_ids):
            entry["media_id"] = media_ids[idx]
        entries.append(entry)
    if entries:
        out["media_entries"] = entries

    return out


def finalize_video_result_urls(request_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """Pick playable HTTPS URLs for video tasks (mirror image CDN behavior)."""
    out = rewrite_result_public_urls(dict(result))
    external = _external_https_urls(out.get("video_urls") or [])
    link = str(out.get("Link") or "").strip()
    if link.startswith(("http://", "https://")):
        rewritten_link = rewrite_public_base_url(link)
        if rewritten_link.startswith(("http://", "https://")) and (
            not PUBLIC_BASE_URL or not rewritten_link.startswith(PUBLIC_BASE_URL)
        ):
            external = external or [rewritten_link]

    if resolve_stored_video_path(request_id, 0):
        out = apply_video_public_urls(request_id, out)
    elif external:
        out["video_urls"] = external
        out["Link"] = external[0]
    else:
        media_ids = [str(m) for m in (out.get("media_ids") or []) if str(m).strip()]
        if media_ids:
            pub = public_media_url(media_ids[0])
            out["video_urls"] = [pub]
            out["Link"] = pub
            out["media_entries"] = [
                {"url": pub, "media_id": media_ids[0], "kind": "video"},
            ]

    return normalize_publisher_urls(rewrite_result_public_urls(out))


def _load_request_video_result(request_id: str) -> dict[str, Any] | None:
    from flow2api.services import activity

    row = activity.get_request(request_id)
    if not row or "video" not in str(row.type or "").lower():
        return None
    result = json.loads(row.result_json or "{}")
    return result if isinstance(result, dict) else None


async def materialize_request_video(
    request_id: str,
    index: int = 0,
    result: dict[str, Any] | None = None,
) -> Optional[Path]:
    """Ensure OUTPUTS/{request_id}/*.mp4 exists — fetch via media_id if needed."""
    existing = resolve_stored_video_path(request_id, index)
    if existing:
        return existing

    payload = result if isinstance(result, dict) else _load_request_video_result(request_id)
    if not payload:
        return None

    apply_video_public_urls(request_id, payload)
    existing = resolve_stored_video_path(request_id, index)
    if existing:
        return existing

    if await _persist_videos(request_id, payload):
        existing = resolve_stored_video_path(request_id, index)
        if existing:
            return existing

    media_ids = [str(m) for m in (payload.get("media_ids") or []) if str(m).strip()]
    if not media_ids:
        return None

    targets: list[tuple[int, str]] = []
    if index < len(media_ids):
        targets = [(index, media_ids[index])]
    elif index == 0:
        targets = [(i, mid) for i, mid in enumerate(media_ids)]

    out_dir = output_dir(request_id)
    for idx, mid in targets:
        data = await _video_bytes_from_source(f"/media/{mid}", mid)
        if not data:
            for url in payload.get("video_urls") or []:
                u = str(url or "").strip()
                if u.startswith(("http://", "https://")):
                    data = await _video_bytes_from_source(u, mid)
                    if data:
                        break
        if not data:
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        filename = "video.mp4" if len(media_ids) <= 1 else f"{idx}.mp4"
        dest = out_dir / filename
        dest.write_bytes(data)
        if idx == index:
            return dest

    return resolve_stored_video_path(request_id, index)


async def persist_task_result(
    request_id: str,
    result: dict[str, Any],
    task_type: str,
) -> dict[str, Any]:
    """Cache media on disk but keep publisher URLs in API result fields."""
    if not isinstance(result, dict) or not _safe_request_id(request_id):
        return result

    out = dict(result)
    is_video = "video" in str(task_type or "").lower()
    publisher_image_urls = list(out.get("image_urls") or [])
    publisher_video_urls = list(out.get("video_urls") or [])
    publisher_link = out.get("Link")

    try:
        if is_video:
            local_paths = await _persist_videos(request_id, out)
        else:
            local_paths = await _persist_images(request_id, out)
        if local_paths:
            out["local_files"] = local_paths
    except Exception as exc:
        logger.warning("persist_task_result failed %s: %s", request_id[:12], exc)

    if is_video:
        if not resolve_stored_video_path(request_id, 0):
            materialized = await materialize_request_video(request_id, 0, out)
            if not materialized:
                logger.warning(
                    "video not materialized for %s (media_ids=%s)",
                    request_id[:12],
                    [str(m)[:8] for m in (out.get("media_ids") or [])[:3]],
                )
        out = finalize_video_result_urls(request_id, out)
    else:
        if publisher_video_urls:
            out["video_urls"] = publisher_video_urls
        if publisher_image_urls:
            out["image_urls"] = publisher_image_urls
        if publisher_link:
            out["Link"] = publisher_link

    return normalize_publisher_urls(out)


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


def purge_expired_outputs(
    *,
    protected_request_ids: set[str] | None = None,
) -> int:
    """Remove output folders older than MEDIA_STORE_TTL_S (skip protected request ids)."""
    protected = protected_request_ids or set()
    ttl = max(60, int(MEDIA_STORE_TTL_S or 6 * 3600))
    cutoff = time.time() - ttl
    deleted = 0
    if not OUTPUTS_DIR.is_dir():
        return 0

    for entry in OUTPUTS_DIR.iterdir():
        if not entry.is_dir():
            continue
        if entry.name in protected:
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
