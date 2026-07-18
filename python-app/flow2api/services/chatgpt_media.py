"""Persist ChatGPT result images/files so poll JSON stays small (Cloudflare-safe)."""
from __future__ import annotations

import base64
import logging
import re
from pathlib import Path
from typing import Any

from flow2api.config import STORAGE_DIR

logger = logging.getLogger(__name__)

_DATA_URL_RE = re.compile(
    r"^data:(image|application)/([a-zA-Z0-9.+-]+);base64,(.+)$",
    re.DOTALL,
)


def chatgpt_media_dir(job_id: str) -> Path:
    safe = "".join(c for c in str(job_id or "") if c.isalnum() or c in "-_")[:64]
    return STORAGE_DIR / "chatgpt_media" / (safe or "_")


def _ext_for_mime(mime: str, kind: str = "image") -> str:
    m = (mime or "").lower()
    if "png" in m:
        return "png"
    if "webp" in m:
        return "webp"
    if "gif" in m:
        return "gif"
    if "pdf" in m:
        return "pdf"
    if "jpeg" in m or "jpg" in m:
        return "jpg"
    return "bin" if kind != "image" else "jpg"


def _decode_data_url(data: str) -> tuple[bytes, str] | None:
    raw = str(data or "").strip()
    if not raw.startswith("data:"):
        return None
    m = _DATA_URL_RE.match(raw)
    if not m:
        # fallback: data:image/...;base64,
        try:
            header, _, body = raw.partition(",")
            if not body:
                return None
            mime = "image/jpeg"
            if ":" in header:
                mime = header.split(":", 1)[1].split(";", 1)[0].strip() or mime
            return base64.b64decode(body), mime
        except Exception:
            return None
    mime = f"{m.group(1)}/{m.group(2)}"
    try:
        return base64.b64decode(m.group(3)), mime
    except Exception:
        return None


def _persist_asset_list(
    job_id: str,
    items: list[Any],
    *,
    kind: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    root = chatgpt_media_dir(job_id)
    root.mkdir(parents=True, exist_ok=True)
    for i, item in enumerate(items or []):
        if not isinstance(item, dict):
            continue
        entry = dict(item)
        data = str(entry.get("data") or "")
        decoded = _decode_data_url(data) if data.startswith("data:") else None
        if decoded:
            raw, mime = decoded
            ext = _ext_for_mime(mime, kind)
            name = f"{kind}-{i}.{ext}"
            path = root / name
            try:
                path.write_bytes(raw)
            except Exception as exc:
                logger.warning("persist chatgpt %s failed: %s", name, exc)
                # Drop huge base64 anyway — keep remote URL if any
                entry.pop("data", None)
                out.append(entry)
                continue
            local = f"/api/chatgpt/web/chat/{job_id}/media/{kind}/{i}"
            entry["download_url"] = local
            entry["url"] = local
            entry["mime_type"] = mime or entry.get("mime_type")
            entry.pop("data", None)
            entry["persisted"] = True
            entry["size_bytes"] = len(raw)
        elif data.startswith("data:"):
            # Unparseable / huge — never return through CF poll
            entry.pop("data", None)
        out.append(entry)
    return out


def persist_chatgpt_result_media(job_id: str, result: dict[str, Any] | None) -> dict[str, Any]:
    """Replace inline base64 with local media URLs. Keeps poll/GET responses small."""
    if not isinstance(result, dict):
        return {}
    out = dict(result)
    jid = str(job_id or "").strip()
    if not jid:
        return out
    if isinstance(out.get("images"), list):
        out["images"] = _persist_asset_list(jid, out["images"], kind="image")
    if isinstance(out.get("files"), list):
        out["files"] = _persist_asset_list(jid, out["files"], kind="file")
    return out


def persist_chatgpt_request_images(
    job_id: str, images: list[Any] | None
) -> list[dict[str, Any]]:
    """Persist request/upload images so job list + detail can show input previews."""
    jid = str(job_id or "").strip()
    if not jid:
        return []
    items: list[dict[str, Any]] = []
    for img in images or []:
        if isinstance(img, dict):
            data = str(img.get("data") or img.get("base64") or "").strip()
            if not data:
                continue
            items.append(
                {
                    "data": data,
                    "fileName": img.get("fileName") or img.get("file_name") or "upload.jpg",
                    "mimeType": img.get("mimeType") or img.get("mime_type"),
                    "kind": "request",
                }
            )
        elif isinstance(img, str) and img.strip():
            items.append({"data": img.strip(), "fileName": "upload.jpg", "kind": "request"})
    return _persist_asset_list(jid, items, kind="request")


def sanitize_chatgpt_result_for_poll(result: dict[str, Any] | None) -> dict[str, Any] | None:
    """Ensure no data: URLs leak into poll JSON (defense in depth)."""
    if result is None:
        return None
    if not isinstance(result, dict):
        return result
    out = dict(result)
    for key in ("images", "files"):
        items = out.get(key)
        if not isinstance(items, list):
            continue
        cleaned = []
        for item in items:
            if not isinstance(item, dict):
                continue
            entry = dict(item)
            data = str(entry.get("data") or "")
            if data.startswith("data:"):
                entry.pop("data", None)
            cleaned.append(entry)
        out[key] = cleaned
    return out


def resolve_chatgpt_media_path(job_id: str, kind: str, index: int) -> Path | None:
    root = chatgpt_media_dir(job_id)
    if not root.is_dir():
        return None
    raw = str(kind or "image").lower()
    if raw == "file":
        kind = "file"
    elif raw in ("request", "input"):
        kind = "request"
    else:
        kind = "image"
    idx = max(0, int(index))
    matches = sorted(root.glob(f"{kind}-{idx}.*"))
    if matches:
        return matches[0]
    return None
