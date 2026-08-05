"""Post-generation watermark gateway for Flow2API.

After media is cached under storage/outputs/{request_id}/, files are cleaned
in-place so /image/{id} and /video/{id} already serve watermark-free bytes.
Client-facing URLs and result shape are unchanged.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from flow2api.config import (
    WATERMARK_CLEAN_ENABLED,
    WATERMARK_FAIL_SOFT,
    WATERMARK_STRIP_IMAGE_METADATA,
    WATERMARK_VIDEO_CROP,
    WATERMARK_VIDEO_MODE,
)

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".flv"}


def _engine():
    """Lazy import so the API boots even if OpenCV is not installed yet."""
    from flow2api.services import watermark_engine as eng

    return eng


def _parse_crop(value: str) -> tuple[float, float]:
    right_s, bottom_s = (item.strip() for item in str(value or "").split(",", 1))
    right, bottom = float(right_s), float(bottom_s)
    if not all(0 <= item < 1 for item in (right, bottom)) or right + bottom == 0:
        raise ValueError("Crop values must be in 0..1 and at least one must be positive.")
    return right, bottom


def _clean_image_inplace(path: Path) -> bool:
    """Image: Erasio reverse-blend (all-tool). Returns True if mark was removed."""
    from flow2api.services.watermark_image_erasio import clean_image_file_erasio

    tmp = path.with_name(f".{path.name}.wm_clean{path.suffix}")
    try:
        cleaned = clean_image_file_erasio(path, tmp, flow_known_watermark=True)
        if not cleaned:
            logger.info("erasio skip (no mark) %s", path.name)
            return False
        if WATERMARK_STRIP_IMAGE_METADATA:
            from flow2api.services.watermark_engine import strip_image_metadata

            meta_tmp = path.with_name(f".{path.name}.wm_meta{path.suffix}")
            try:
                strip_image_metadata(tmp, meta_tmp)
                meta_tmp.replace(tmp)
            finally:
                meta_tmp.unlink(missing_ok=True)
        tmp.replace(path)
        return True
    finally:
        tmp.unlink(missing_ok=True)


def _clean_video_inplace(path: Path) -> bool:
    eng = _engine()
    tmp = path.with_name(f".{path.name}.wm_clean{path.suffix}")
    try:
        mode = (WATERMARK_VIDEO_MODE or "inpaint").strip().lower()
        if mode == "crop":
            right, bottom = _parse_crop(WATERMARK_VIDEO_CROP)
            eng.crop_video_file(path, tmp, right, bottom)
        else:
            eng.clean_video_file(path, tmp, eng.VEO_BOTTOM_RIGHT)
        tmp.replace(path)
        return True
    finally:
        tmp.unlink(missing_ok=True)


def _clean_path_sync(path: Path) -> bool:
    """Clean one media file. Returns True only when media was modified."""
    suffix = path.suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return _clean_image_inplace(path)
    if suffix in VIDEO_EXTENSIONS:
        return _clean_video_inplace(path)
    logger.debug("watermark gateway skip unsupported type: %s", path.name)
    return False


def collect_output_media_paths(out_dir: Path, *, is_video: bool) -> list[Path]:
    if not out_dir.is_dir():
        return []
    if is_video:
        return sorted(
            p for p in out_dir.glob("*.mp4") if p.is_file() and not p.name.startswith(".")
        )
    return sorted(
        p
        for p in out_dir.iterdir()
        if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in IMAGE_EXTENSIONS
    )


async def clean_media_files(paths: list[Path], *, request_id: str = "") -> dict[str, Any]:
    """Clean listed files in a worker thread. Soft-fails when configured."""
    if not WATERMARK_CLEAN_ENABLED or not paths:
        return {"enabled": WATERMARK_CLEAN_ENABLED, "cleaned": 0, "failed": 0}

    started = time.monotonic()
    cleaned = 0
    failed = 0
    errors: list[str] = []
    rid = (request_id or "")[:12]

    for path in paths:
        if not path.is_file():
            continue
        try:
            ok = await asyncio.to_thread(_clean_path_sync, path)
            if ok:
                cleaned += 1
                logger.info("watermark cleaned %s (%s)", path.name, rid or "n/a")
            else:
                logger.info("watermark untouched %s (%s)", path.name, rid or "n/a")
        except Exception as exc:
            failed += 1
            msg = f"{path.name}: {exc}"
            errors.append(msg)
            logger.warning("watermark clean failed %s %s", rid or path.name, exc)
            if not WATERMARK_FAIL_SOFT:
                raise

    elapsed = round(time.monotonic() - started, 3)
    return {
        "enabled": True,
        "cleaned": cleaned,
        "failed": failed,
        "elapsed_seconds": elapsed,
        "errors": errors,
    }


async def clean_request_outputs(
    request_id: str,
    out_dir: Path,
    *,
    is_video: bool,
) -> dict[str, Any]:
    """Gateway entry: clean all stored images/videos for a finished request."""
    paths = collect_output_media_paths(out_dir, is_video=is_video)
    return await clean_media_files(paths, request_id=request_id)
