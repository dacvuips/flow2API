"""Synchronous watermark-clean for API clients (base64 in → base64/url out)."""
from __future__ import annotations

import base64
import logging
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Literal

from flow2api.config import (
    WATERMARK_CLEAN_ENABLED,
    WATERMARK_STRIP_IMAGE_METADATA,
    WATERMARK_VIDEO_CROP,
    WATERMARK_VIDEO_MODE,
)
from flow2api.short_id import new_request_id
from flow2api.services.stored_media import (
    local_output_path,
    output_dir,
    public_image_url,
    public_video_url,
)

logger = logging.getLogger(__name__)

Kind = Literal["image", "video"]
ReturnMode = Literal["base64", "url", "both"]

_DATA_URL_RE = re.compile(
    r"^data:(?P<mime>[\w.+/-]+);base64,(?P<data>.+)$",
    re.IGNORECASE | re.DOTALL,
)
_IMAGE_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"RIFF", "image/webp", ".webp"),  # refined below
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
)
_MAX_IMAGE_BYTES = 30 * 1024 * 1024  # 30 MB
_MAX_VIDEO_BYTES = 120 * 1024 * 1024  # 120 MB


def _strip_data_url(value: str) -> tuple[str, str | None]:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("empty media base64")
    match = _DATA_URL_RE.match(raw)
    if match:
        return match.group("data"), match.group("mime").lower()
    # pure base64 — strip whitespace/newlines
    compact = re.sub(r"\s+", "", raw)
    return compact, None


def decode_media_base64(value: str) -> tuple[bytes, str | None]:
    payload, mime = _strip_data_url(value)
    try:
        data = base64.b64decode(payload, validate=False)
    except Exception as exc:
        raise ValueError("invalid base64 media") from exc
    if not data:
        raise ValueError("decoded media is empty")
    return data, mime


def sniff_kind_and_mime(data: bytes, hint_mime: str | None = None) -> tuple[Kind, str, str]:
    """Return (kind, mime_type, file_suffix)."""
    if hint_mime:
        hm = hint_mime.lower().split(";")[0].strip()
        if hm.startswith("image/"):
            ext = {
                "image/jpeg": ".jpg",
                "image/jpg": ".jpg",
                "image/png": ".png",
                "image/webp": ".webp",
                "image/gif": ".gif",
            }.get(hm, ".jpg")
            return "image", hm if hm != "image/jpg" else "image/jpeg", ext
        if hm.startswith("video/"):
            ext = {
                "video/mp4": ".mp4",
                "video/webm": ".webm",
                "video/quicktime": ".mov",
            }.get(hm, ".mp4")
            return "video", hm, ext

    if data[:3] == b"\xff\xd8\xff":
        return "image", "image/jpeg", ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image", "image/png", ".png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image", "image/webp", ".webp"
    if data[:4] == b"GIF8":
        return "image", "image/gif", ".gif"
    # ISO BMFF: ftyp box (mp4/mov/m4v)
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "video", "video/mp4", ".mp4"
    if data[:4] == b"\x1aE\xdf\xa3":
        return "video", "video/webm", ".webm"
    # Fallback: treat as image jpeg (most Flow exports)
    return "image", "image/jpeg", ".jpg"


def _parse_crop(value: str) -> tuple[float, float]:
    right_s, bottom_s = (item.strip() for item in str(value or "").split(",", 1))
    right, bottom = float(right_s), float(bottom_s)
    return right, bottom


def _clean_image_bytes(data: bytes) -> dict[str, Any]:
    from flow2api.services.watermark_image_erasio import remove_image_watermark
    import io

    result = remove_image_watermark(data, flow_known_watermark=True)
    buf = result["buffer"]
    mime = str(result.get("mime_type") or "image/jpeg")
    if WATERMARK_STRIP_IMAGE_METADATA and result.get("cleaned"):
        try:
            from PIL import Image

            with Image.open(io.BytesIO(buf)) as im:
                out = io.BytesIO()
                if "png" in mime:
                    im.save(out, format="PNG")
                    mime = "image/png"
                elif "webp" in mime:
                    im.save(out, format="WEBP", quality=95)
                    mime = "image/webp"
                else:
                    im.convert("RGB").save(out, format="JPEG", quality=95)
                    mime = "image/jpeg"
                buf = out.getvalue()
        except Exception:
            pass
    return {
        "buffer": buf,
        "mime_type": mime,
        "cleaned": bool(result.get("cleaned")),
        "ncc": result.get("ncc"),
    }


def _clean_video_bytes(data: bytes, suffix: str = ".mp4") -> dict[str, Any]:
    from flow2api.services.watermark_engine import (
        VEO_BOTTOM_RIGHT,
        clean_video_file,
        crop_video_file,
    )

    with tempfile.TemporaryDirectory(prefix="wm-api-") as directory:
        src = Path(directory) / f"input{suffix or '.mp4'}"
        dst = Path(directory) / f"output{suffix or '.mp4'}"
        src.write_bytes(data)
        mode = (WATERMARK_VIDEO_MODE or "inpaint").strip().lower()
        if mode == "crop":
            right, bottom = _parse_crop(WATERMARK_VIDEO_CROP)
            crop_video_file(src, dst, right, bottom)
        else:
            clean_video_file(src, dst, VEO_BOTTOM_RIGHT)
        if not dst.is_file() or dst.stat().st_size < 1:
            raise RuntimeError("video watermark clean produced empty output")
        return {
            "buffer": dst.read_bytes(),
            "mime_type": "video/mp4",
            "cleaned": True,
            "ncc": None,
        }


def clean_media_payload(
    *,
    media_base64: str | None = None,
    image_base64: str | None = None,
    video_base64: str | None = None,
    kind: str | None = None,
    return_mode: str = "both",
) -> dict[str, Any]:
    """
    Clean one media blob. Synchronous / CPU-bound — call via asyncio.to_thread.
    """
    if not WATERMARK_CLEAN_ENABLED:
        raise RuntimeError("watermark clean is disabled (FLOW2API_WATERMARK_CLEAN=0)")

    started = time.monotonic()
    raw_b64 = media_base64 or image_base64 or video_base64
    if not raw_b64:
        raise ValueError("Provide media_base64, image_base64, or video_base64")

    data, hint_mime = decode_media_base64(str(raw_b64))
    detected_kind, mime, suffix = sniff_kind_and_mime(data, hint_mime)
    forced = str(kind or "").strip().lower()
    if forced in ("image", "video"):
        detected_kind = forced  # type: ignore[assignment]
        if forced == "image" and not mime.startswith("image/"):
            mime, suffix = "image/jpeg", ".jpg"
        if forced == "video" and not mime.startswith("video/"):
            mime, suffix = "video/mp4", ".mp4"
    elif image_base64 and not video_base64:
        detected_kind = "image"
    elif video_base64 and not image_base64:
        detected_kind = "video"

    if detected_kind == "image" and len(data) > _MAX_IMAGE_BYTES:
        raise ValueError(f"image too large (max {_MAX_IMAGE_BYTES // (1024 * 1024)}MB)")
    if detected_kind == "video" and len(data) > _MAX_VIDEO_BYTES:
        raise ValueError(f"video too large (max {_MAX_VIDEO_BYTES // (1024 * 1024)}MB)")

    if detected_kind == "image":
        cleaned = _clean_image_bytes(data)
    else:
        cleaned = _clean_video_bytes(data, suffix=suffix if suffix.startswith(".") else f".{suffix}")

    out_buf: bytes = cleaned["buffer"]
    out_mime: str = cleaned["mime_type"]
    rid = new_request_id()
    mode = str(return_mode or "both").strip().lower()
    if mode not in ("base64", "url", "both"):
        mode = "both"

    url: str | None = None
    if mode in ("url", "both"):
        out_dir = output_dir(rid)
        out_dir.mkdir(parents=True, exist_ok=True)
        if detected_kind == "image":
            ext = {
                "image/jpeg": ".jpg",
                "image/png": ".png",
                "image/webp": ".webp",
            }.get(out_mime, ".jpg")
            name = f"0{ext}"
            (out_dir / name).write_bytes(out_buf)
            url = public_image_url(rid, 0)
        else:
            name = "video.mp4"
            (out_dir / name).write_bytes(out_buf)
            url = public_video_url(rid, 0)
        local_files = [local_output_path(rid, name)]
    else:
        local_files = []

    payload: dict[str, Any] = {
        "success": True,
        "cleaned": bool(cleaned.get("cleaned")),
        "kind": detected_kind,
        "mime_type": out_mime,
        "request_id": rid,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "message": (
            "Watermark removed."
            if cleaned.get("cleaned")
            else "No watermark detected; original media returned."
        ),
    }
    if cleaned.get("ncc") is not None:
        payload["ncc"] = cleaned["ncc"]
    if mode in ("base64", "both"):
        payload["media_base64"] = base64.b64encode(out_buf).decode("ascii")
        # Aliases matching gen response style
        if detected_kind == "image":
            payload["image_base64"] = payload["media_base64"]
        else:
            payload["video_base64"] = payload["media_base64"]
    if url:
        payload["url"] = url
        payload["Link"] = url
        if detected_kind == "image":
            payload["image_urls"] = [url]
        else:
            payload["video_urls"] = [url]
        if local_files:
            payload["local_files"] = local_files
    return payload
