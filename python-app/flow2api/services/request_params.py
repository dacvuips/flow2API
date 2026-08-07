"""Normalize API request params — accept camelCase aliases from external clients."""
from __future__ import annotations

from typing import Any

# Pin mọi video generation về Veo 3.1 Lite [Lower Priority]
FORCED_VIDEO_QUALITY = "lite_relaxed"


def normalize_video_quality_value(raw: Any) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""
    # Bỏ qua giá trị client gửi lên — luôn dùng lower-priority Lite
    return FORCED_VIDEO_QUALITY


def get_video_quality(params: dict[str, Any], default: str = "") -> str:
    """Luôn trả về lite_relaxed khi request có video quality / default video."""
    p = params or {}
    for key in ("video_quality", "videoQuality", "videoModel"):
        val = p.get(key)
        if val is not None and str(val).strip():
            return FORCED_VIDEO_QUALITY
    if default:
        return FORCED_VIDEO_QUALITY
    return ""


def normalize_request_params(params: dict[str, Any]) -> dict[str, Any]:
    from flow2api.services.activity import sanitize_utf8

    out = sanitize_utf8(dict(params or {}))
    if not isinstance(out, dict):
        out = {}

    has_video_quality = any(
        out.get(k) is not None and str(out.get(k) or "").strip()
        for k in ("video_quality", "videoQuality", "videoModel")
    )
    has_video_hint = any(
        out.get(k) is not None and str(out.get(k) or "").strip()
        for k in ("video_mode", "videoMode", "video_base64s", "videoBase64s")
    )
    if has_video_quality or has_video_hint:
        out["video_quality"] = FORCED_VIDEO_QUALITY

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

    # gen_text: frontend may send thinking_level / thinkingLevel (default HIGH on worker)
    if not out.get("thinking_level"):
        for key in ("thinkingLevel", "thinking_config", "thinkingConfig"):
            raw = out.get(key)
            if raw is None:
                continue
            if isinstance(raw, dict):
                raw = raw.get("thinkingLevel") or raw.get("thinking_level") or raw.get("level")
            if raw is not None and str(raw).strip():
                out["thinking_level"] = str(raw).strip().upper()
                break
    elif out.get("thinking_level") is not None:
        out["thinking_level"] = str(out.get("thinking_level")).strip().upper()

    if not out.get("system_instruction"):
        for key in ("systemInstruction",):
            if out.get(key) is not None:
                out["system_instruction"] = out[key]
                break

    if not out.get("model") and not out.get("image_model"):
        for key in ("text_model", "textModel"):
            if out.get(key) is not None and str(out.get(key) or "").strip():
                out["model"] = str(out[key]).strip()
                break

    return out
