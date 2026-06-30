"""Normalize API request params — accept camelCase aliases from external clients."""
from __future__ import annotations

import re
from typing import Any


def normalize_video_quality_value(raw: Any) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s).lower().replace("-", "_")
    compact = snake.replace("_", "")
    if compact in ("omniflash", "omni"):
        return "omni_flash"
    return snake


def get_video_quality(params: dict[str, Any], default: str = "") -> str:
    p = params or {}
    for key in ("video_quality", "videoQuality", "videoModel"):
        val = p.get(key)
        if val is not None and str(val).strip():
            return normalize_video_quality_value(val)
    return default


def normalize_request_params(params: dict[str, Any]) -> dict[str, Any]:
    out = dict(params or {})

    quality = get_video_quality(out, default="")
    if quality:
        out["video_quality"] = quality

    if out.get("video_duration_s") is None:
        for key in ("videoDurationS", "omniDurationS", "omni_duration_s"):
            if out.get(key) is not None:
                out["video_duration_s"] = out[key]
                break

    if not out.get("video_mode"):
        for key in ("videoMode",):
            if out.get(key):
                out["video_mode"] = out[key]
                break

    if not out.get("aspect_ratio") and out.get("aspectRatio"):
        out["aspect_ratio"] = out["aspectRatio"]

    if not out.get("image_model"):
        for key in ("imageModel",):
            if out.get(key):
                out["image_model"] = out[key]
                break

    image_base64s = out.get("image_base64s") or out.get("imageBase64s")
    if image_base64s:
        out["image_base64s"] = image_base64s

    video_base64s = out.get("video_base64s") or out.get("videoBase64s")
    if video_base64s:
        out["video_base64s"] = video_base64s

    if not out.get("profile_id"):
        for key in ("profileId",):
            if out.get(key):
                out["profile_id"] = str(out[key]).strip()
                break

    return out
