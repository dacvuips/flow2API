"""
Google Flow SDK — gọi aisandbox-pa qua extension bridge.

Ported from Flow2API / flowkit patterns.
"""
from __future__ import annotations

import asyncio
import re
import time
import uuid
from typing import Any, Optional

import base64
import logging

from flow2api.config import (
    GOOGLE_API_KEY,
    GOOGLE_FLOW_API,
    POLL_INTERVAL_S,
    RECAPTCHA_RETRY_MAX,
    VIDEOS_DIR,
)
from flow2api.services.flow_client import FlowClient

logger = logging.getLogger(__name__)

IMAGE_MODELS = {
    "NANO_BANANA_PRO": "GEM_PIX_2",
    "NANO_BANANA_2": "GEM_PIX",
}

IMAGE_ASPECT = {
    "16:9": "IMAGE_ASPECT_RATIO_LANDSCAPE",
    "4:3": "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE",
    "1:1": "IMAGE_ASPECT_RATIO_SQUARE",
    "3:4": "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR",
    "9:16": "IMAGE_ASPECT_RATIO_PORTRAIT",
}

VIDEO_ASPECT = {
    "16:9": "VIDEO_ASPECT_RATIO_LANDSCAPE",
    "9:16": "VIDEO_ASPECT_RATIO_PORTRAIT",
}

# Keys per generation mode (t2v / i2v / r2v use different upstream model families).
VIDEO_MODEL_KEYS: dict[str, dict[str, dict[str, dict[str, str]]]] = {
    "t2v": {
        "PAYGATE_TIER_ONE": {
            "16:9": {"lite": "veo_3_1_t2v_lite", "fast": "veo_3_1_t2v_fast", "quality": "veo_3_1_t2v"},
            "9:16": {
                "lite": "veo_3_1_t2v_lite",
                "fast": "veo_3_1_t2v_fast_portrait",
                "quality": "veo_3_1_t2v_portrait",
            },
        },
        "PAYGATE_TIER_TWO": {
            "16:9": {
                "lite": "veo_3_1_t2v_lite",
                "fast": "veo_3_1_t2v_fast_ultra",
                "quality": "veo_3_1_t2v",
                "lite_relaxed": "veo_3_1_t2v_lite_low_priority",
            },
            "9:16": {
                "lite": "veo_3_1_t2v_lite",
                "fast": "veo_3_1_t2v_fast_portrait_ultra",
                "quality": "veo_3_1_t2v_portrait",
                "lite_relaxed": "veo_3_1_t2v_lite_low_priority",
            },
        },
    },
    "i2v": {
        "PAYGATE_TIER_ONE": {
            "16:9": {"lite": "veo_3_1_i2v_lite", "fast": "veo_3_1_i2v_s_fast", "quality": "veo_3_1_i2v_s"},
            "9:16": {
                "lite": "veo_3_1_i2v_lite",
                "fast": "veo_3_1_i2v_s_fast_portrait",
                "quality": "veo_3_1_i2v_s_portrait",
            },
        },
        "PAYGATE_TIER_TWO": {
            "16:9": {
                "lite": "veo_3_1_i2v_lite",
                "fast": "veo_3_1_i2v_s_fast_ultra",
                "quality": "veo_3_1_i2v_s",
                "lite_relaxed": "veo_3_1_i2v_lite_low_priority",
            },
            "9:16": {
                "lite": "veo_3_1_i2v_lite",
                "fast": "veo_3_1_i2v_s_fast_portrait_ultra",
                "quality": "veo_3_1_i2v_s_portrait",
                "lite_relaxed": "veo_3_1_i2v_lite_low_priority",
            },
        },
    },
    "i2v_fl": {
        "PAYGATE_TIER_ONE": {
            "16:9": {
                "lite": "veo_3_1_i2v_lite",
                "fast": "veo_3_1_i2v_s_fast_fl",
                "quality": "veo_3_1_i2v_s_fast_fl",
            },
            "9:16": {
                "lite": "veo_3_1_i2v_lite",
                "fast": "veo_3_1_i2v_s_fast_portrait_fl",
                "quality": "veo_3_1_i2v_s_fast_portrait_fl",
            },
        },
        "PAYGATE_TIER_TWO": {
            "16:9": {
                "lite": "veo_3_1_i2v_lite",
                "fast": "veo_3_1_i2v_s_fast_ultra_fl",
                "quality": "veo_3_1_i2v_s_fast_ultra_fl",
                "lite_relaxed": "veo_3_1_i2v_lite_low_priority",
            },
            "9:16": {
                "lite": "veo_3_1_i2v_lite",
                "fast": "veo_3_1_i2v_s_fast_portrait_ultra_fl",
                "quality": "veo_3_1_i2v_s_fast_portrait_ultra_fl",
                "lite_relaxed": "veo_3_1_i2v_lite_low_priority",
            },
        },
    },
    "r2v": {
        "PAYGATE_TIER_ONE": {
            "16:9": {"lite": "veo_3_1_r2v_fast", "fast": "veo_3_1_r2v_fast", "quality": "veo_3_1_r2v_fast"},
            "9:16": {
                "lite": "veo_3_1_r2v_fast_portrait",
                "fast": "veo_3_1_r2v_fast_portrait",
                "quality": "veo_3_1_r2v_fast_portrait",
            },
        },
        "PAYGATE_TIER_TWO": {
            "16:9": {
                "lite": "veo_3_1_r2v_fast",
                "fast": "veo_3_1_r2v_fast_ultra",
                "quality": "veo_3_1_r2v_fast_ultra",
                "lite_relaxed": "veo_3_1_r2v_lite_low_priority",
            },
            "9:16": {
                "lite": "veo_3_1_r2v_fast_portrait",
                "fast": "veo_3_1_r2v_fast_portrait_ultra",
                "quality": "veo_3_1_r2v_fast_portrait_ultra",
                "lite_relaxed": "veo_3_1_r2v_lite_low_priority",
            },
        },
    },
}

VIDEO_T2V_PATH = "/v1/video:batchAsyncGenerateVideoText"
VIDEO_START_PATH = "/v1/video:batchAsyncGenerateVideoStartImage"
VIDEO_START_END_PATH = "/v1/video:batchAsyncGenerateVideoStartAndEndImage"
VIDEO_REF_PATH = "/v1/video:batchAsyncGenerateVideoReferenceImages"
VIDEO_POLL_PATH = "/v1/video:batchCheckAsyncVideoGenerationStatus"
UPLOAD_IMAGE_PATH = "/v1/flow/uploadImage"


def _api_url(path: str) -> str:
    url = f"{GOOGLE_FLOW_API}{path}"
    if GOOGLE_API_KEY:
        sep = "&" if "?" in path else "?"
        return f"{url}{sep}key={GOOGLE_API_KEY}"
    return url


def _image_batch_url(project_id: str) -> str:
    return _api_url(f"/v1/projects/{project_id}/flowMedia:batchGenerateImages")


def _require_tier(client: FlowClient) -> str:
    return client.paygate_tier or "PAYGATE_TIER_ONE"


def _client_context(project_id: str, tier: str, session_ms: Optional[int] = None) -> dict:
    ts = session_ms if session_ms is not None else int(time.time() * 1000)
    return {
        "projectId": project_id,
        "recaptchaContext": {
            "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
            "token": "",
        },
        "sessionId": f";{ts}",
        "tool": "PINHOLE",
        "userPaygateTier": tier,
    }


def _video_request_body(project_id: str, tier: str, request_item: dict) -> dict:
    """Image-to-video / reference-video (Flow web client shape)."""
    return {
        "mediaGenerationContext": {"batchId": str(uuid.uuid4())},
        "clientContext": _client_context(project_id, tier),
        "requests": [request_item],
        "useV2ModelConfig": True,
    }


def _t2v_request_body(project_id: str, tier: str, request_item: dict) -> dict:
    """Text-to-video — no useV2ModelConfig / mediaGenerationContext (labs.google shape)."""
    return {
        "clientContext": _client_context(project_id, tier),
        "requests": [request_item],
    }


async def ensure_project(client: FlowClient, title: str = "Flow2API") -> str:
    data = await client.trpc_request(
        "project.createProject",
        {"json": {"projectTitle": title, "toolName": "PINHOLE"}},
    )
    try:
        return data["result"]["data"]["json"]["result"]["projectId"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError(f"create_project_failed: {data}") from exc


def _strip_data_url(b64: str) -> str:
    if b64.startswith("data:"):
        return b64.split(",", 1)[-1]
    return b64


async def upload_images(
    client: FlowClient,
    *,
    project_id: str,
    image_base64s: list[str],
) -> list[str]:
    """Upload user images via /v1/flow/uploadImage — returns media UUIDs for video APIs."""
    ids: list[str] = []
    for idx, b64 in enumerate(image_base64s):
        if not b64:
            continue
        mime = "image/png" if str(b64).startswith("data:image/png") else "image/jpeg"
        ext = "png" if mime == "image/png" else "jpg"
        ids.append(
            await upload_image(
                client,
                project_id=project_id,
                image_base64=b64,
                mime_type=mime,
                file_name=f"upload_{idx}.{ext}",
            )
        )
    return ids


async def upload_image(
    client: FlowClient,
    *,
    project_id: str,
    image_base64: str,
    mime_type: str = "image/jpeg",
    file_name: str = "upload.jpg",
) -> str:
    body = {
        "clientContext": {"projectId": project_id, "tool": "PINHOLE"},
        "fileName": file_name,
        "imageBytes": _strip_data_url(image_base64),
        "isHidden": False,
        "isUserUploaded": True,
        "mimeType": mime_type,
    }
    last_err = "upload_image_failed"
    last_resp: dict = {}
    for attempt in range(RECAPTCHA_RETRY_MAX):
        resp = await client.api_request(
            _api_url(UPLOAD_IMAGE_PATH), body=body, timeout=120, raise_on_error=False
        )
        last_resp = resp if isinstance(resp, dict) else {}
        data = resp.get("data") or {}
        media = data.get("media") if isinstance(data, dict) else {}
        name = (media or {}).get("name") if isinstance(media, dict) else None
        if name:
            return name
        last_err = error_from_response(resp)
        if is_recaptcha_error(last_err) and attempt < RECAPTCHA_RETRY_MAX - 1:
            delay = _recaptcha_retry_delay(attempt)
            logger.warning(
                "upload reCAPTCHA retry %s/%s: %s — wait %ss",
                attempt + 1,
                RECAPTCHA_RETRY_MAX,
                last_err,
                delay,
            )
            await asyncio.sleep(delay)
            continue
        break
    raise RuntimeError(f"upload_image_failed: {last_err} ({last_resp})")


async def gen_image(
    client: FlowClient,
    *,
    project_id: str,
    prompt: str,
    aspect_ratio: str,
    image_model: str,
    variant_count: int = 1,
    image_base64s: Optional[list[str]] = None,
    image_input_types: Optional[list[str]] = None,
) -> dict:
    tier = _require_tier(client)
    model_name = IMAGE_MODELS.get(image_model, IMAGE_MODELS["NANO_BANANA_PRO"])
    aspect = IMAGE_ASPECT.get(aspect_ratio, IMAGE_ASPECT["16:9"])

    uploaded_ids: list[str] = []
    if image_base64s:
        types = image_input_types or ["reference"] * len(image_base64s)
        for b64, _kind in zip(image_base64s, types):
            uploaded_ids.append(
                await upload_image(client, project_id=project_id, image_base64=b64)
            )

    last_err = "image_generation_failed"
    last_resp: dict = {}
    for attempt in range(RECAPTCHA_RETRY_MAX):
        ts_try = int(time.time() * 1000) + attempt
        ctx_try = _client_context(project_id, tier, ts_try)
        requests_try: list[dict[str, Any]] = []
        for i in range(max(1, min(variant_count, 4))):
            item: dict[str, Any] = {
                "clientContext": {**ctx_try, "sessionId": f";{ts_try + i}"},
                "seed": (ts_try + i) % 1_000_000,
                "structuredPrompt": {"parts": [{"text": prompt}]},
                "imageAspectRatio": aspect,
                "imageModelName": model_name,
            }
            if uploaded_ids:
                types = image_input_types or ["reference"] * len(uploaded_ids)
                item["imageInputs"] = [
                    {
                        "name": mid,
                        "imageInputType": "IMAGE_INPUT_TYPE_REFERENCE"
                        if kind == "reference"
                        else "IMAGE_INPUT_TYPE_BASE_IMAGE",
                    }
                    for mid, kind in zip(uploaded_ids, types)
                ]
            requests_try.append(item)
        body: dict[str, Any] = {"clientContext": ctx_try, "requests": requests_try}
        if uploaded_ids:
            body["mediaGenerationContext"] = {"batchId": str(uuid.uuid4())}
            body["useNewMedia"] = True
        resp = await client.api_request(
            _image_batch_url(project_id),
            body=body,
            captcha_action="IMAGE_GENERATION",
            timeout=300,
            raise_on_error=False,
        )
        last_resp = resp if isinstance(resp, dict) else {}
        status = int(resp.get("status") or 0)
        if status < 400:
            return resp.get("data") or {}
        last_err = error_from_response(resp)
        if is_recaptcha_error(last_err) and attempt < RECAPTCHA_RETRY_MAX - 1:
            delay = _recaptcha_retry_delay(attempt)
            logger.warning(
                "image gen reCAPTCHA retry %s/%s: %s — wait %ss",
                attempt + 1,
                RECAPTCHA_RETRY_MAX,
                last_err,
                delay,
            )
            await asyncio.sleep(delay)
            continue
        break
    raise FlowApiError(last_err, step="gen_image", raw=last_resp)


def _iter_generated_images(data: dict) -> list[dict]:
    out: list[dict] = []
    for entry in data.get("media") or []:
        if not isinstance(entry, dict):
            continue
        gen = (entry.get("image") or {}).get("generatedImage")
        if isinstance(gen, dict):
            out.append(gen)
            continue
        if entry.get("fifeUrl") or entry.get("url"):
            out.append(entry)
    return out


def extract_image_urls(data: dict) -> list[str]:
    urls: list[str] = []
    for entry in _iter_generated_images(data):
        url = entry.get("fifeUrl") or entry.get("url")
        if url:
            urls.append(url)
    return urls


def extract_image_media_ids(data: dict) -> list[str]:
    ids: list[str] = []
    for entry in _iter_generated_images(data):
        mid = entry.get("mediaId") or entry.get("name")
        if mid:
            ids.append(mid)
    return ids


def _video_model_key(mode: str, tier: str, aspect: str, quality: str) -> str:
    table = VIDEO_MODEL_KEYS.get(mode) or VIDEO_MODEL_KEYS["t2v"]
    tier_map = table.get(tier) or table["PAYGATE_TIER_ONE"]
    aspect_map = tier_map.get(aspect) or tier_map["16:9"]
    key = aspect_map.get(quality)
    if not key:
        raise ValueError(f"unsupported video model {quality} for {mode} {aspect} / {tier}")
    return key


async def gen_text_video(
    client: FlowClient,
    *,
    project_id: str,
    prompt: str,
    aspect_ratio: str,
    video_quality: str,
) -> dict:
    tier = _require_tier(client)
    model_key = _video_model_key("t2v", tier, aspect_ratio, video_quality)
    request_item = {
        "aspectRatio": VIDEO_ASPECT.get(aspect_ratio, VIDEO_ASPECT["16:9"]),
        "seed": int(time.time() * 1000) % 1_000_000,
        "textInput": {"prompt": prompt},
        "videoModelKey": model_key,
    }
    body = _t2v_request_body(project_id, tier, request_item)
    return await _video_submit_request(
        client, _api_url(VIDEO_T2V_PATH), body, model_key=model_key
    )


async def gen_video_start_image(
    client: FlowClient,
    *,
    project_id: str,
    prompt: str,
    aspect_ratio: str,
    video_quality: str,
    start_media_id: str,
) -> dict:
    tier = _require_tier(client)
    model_key = _video_model_key("i2v", tier, aspect_ratio, video_quality)
    request_item = {
        "aspectRatio": VIDEO_ASPECT.get(aspect_ratio, VIDEO_ASPECT["16:9"]),
        "seed": int(time.time()) % 10000,
        "textInput": {"structuredPrompt": {"parts": [{"text": prompt}]}},
        "videoModelKey": model_key,
        "startImage": {"mediaId": start_media_id},
        "metadata": {},
    }
    body = _video_request_body(project_id, tier, request_item)
    return await _video_submit_request(
        client, _api_url(VIDEO_START_PATH), body, model_key=model_key
    )


async def gen_video_start_end_image(
    client: FlowClient,
    *,
    project_id: str,
    prompt: str,
    aspect_ratio: str,
    video_quality: str,
    start_media_id: str,
    end_media_id: str,
) -> dict:
    tier = _require_tier(client)
    model_key = _video_model_key("i2v_fl", tier, aspect_ratio, video_quality)
    request_item = {
        "aspectRatio": VIDEO_ASPECT.get(aspect_ratio, VIDEO_ASPECT["16:9"]),
        "seed": int(time.time()) % 10000,
        "textInput": {"structuredPrompt": {"parts": [{"text": prompt}]}},
        "videoModelKey": model_key,
        "startImage": {"mediaId": start_media_id},
        "endImage": {"mediaId": end_media_id},
        "metadata": {},
    }
    body = _video_request_body(project_id, tier, request_item)
    return await _video_submit_request(
        client, _api_url(VIDEO_START_END_PATH), body, model_key=model_key
    )


async def gen_multi_image_video(
    client: FlowClient,
    *,
    project_id: str,
    prompt: str,
    aspect_ratio: str,
    video_quality: str,
    reference_media_ids: list[str],
) -> dict:
    tier = _require_tier(client)
    model_key = _video_model_key("r2v", tier, aspect_ratio, video_quality)
    request_item = {
        "aspectRatio": VIDEO_ASPECT.get(aspect_ratio, VIDEO_ASPECT["16:9"]),
        "seed": int(time.time()) % 10000,
        "textInput": {"structuredPrompt": {"parts": [{"text": prompt}]}},
        "videoModelKey": model_key,
        "referenceImages": [
            {"mediaId": mid, "imageUsageType": "IMAGE_USAGE_TYPE_ASSET"}
            for mid in reference_media_ids
        ],
        "metadata": {},
    }
    body = _video_request_body(project_id, tier, request_item)
    return await _video_submit_request(
        client, _api_url(VIDEO_REF_PATH), body, model_key=model_key
    )


_TRANSIENT_MARKERS = (
    "internal error",
    "high traffic",
    "resource_exhausted",
    "temporarily unavailable",
    "unavailable",
)


class FlowApiError(RuntimeError):
    """Flow API failure — carries raw extension/api responses for dashboard debug."""

    def __init__(
        self,
        message: str,
        *,
        step: str = "",
        raw: Any = None,
        attempts: list[dict] | None = None,
    ) -> None:
        super().__init__(message)
        self.step = step
        self.raw = raw
        self.attempts = attempts or []


class GetMedia404Error(FlowApiError):
    """GET /v1/media/{id} returned 404 — worker may requeue the job."""

    def __init__(self, message: str, *, raw: Any = None) -> None:
        super().__init__(message, step="get_media", raw=raw)


def is_get_media_404_error(exc: Exception) -> bool:
    if isinstance(exc, GetMedia404Error):
        return True
    if isinstance(exc, FlowApiError) and exc.step == "get_media":
        raw = exc.raw
        if isinstance(raw, dict) and int(raw.get("status") or 0) == 404:
            return True
    return "HTTP_404" in str(exc)


def compact_api_response(resp: Any, label: str = "") -> dict:
    """Shrink extension callback for logs / task result (keep structure, drop huge blobs)."""
    if not isinstance(resp, dict):
        return {"label": label or "response", "body": str(resp)[:4000]}
    data = resp.get("data")
    compact_data: Any
    if isinstance(data, str):
        compact_data = data[:4000]
    elif isinstance(data, dict):
        compact_data = {
            k: (v if not isinstance(v, str) or len(v) < 400 else f"[string {len(v)} chars]")
            for k, v in data.items()
        }
        if isinstance(data.get("error"), dict):
            compact_data["error"] = data["error"]
    else:
        compact_data = data
    return {
        "label": label or "response",
        "http_status": resp.get("status"),
        "extension_error": resp.get("error"),
        "data": compact_data,
        "top_keys": list(resp.keys()),
    }


def is_transient_flow_error(msg: str) -> bool:
    low = str(msg or "").lower()
    return any(m in low for m in _TRANSIENT_MARKERS)


_RECAPTCHA_ERR_RE = re.compile(r"re\s*[-_]?\s*captcha|captcha", re.IGNORECASE)


def is_recaptcha_error(msg: str) -> bool:
    """reCAPTCHA / RECAPTCHA / captcha — không phân biệt hoa thường."""
    text = str(msg or "")
    if not text:
        return False
    return bool(_RECAPTCHA_ERR_RE.search(text))


def _recaptcha_retry_delay(attempt: int) -> float:
    return float(min(30, (attempt + 1) * 2))


def error_from_response(resp: dict) -> str:
    data = resp.get("data")
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("status") or err)
        if err:
            return str(err)
    if resp.get("error"):
        return str(resp["error"])
    status = int(resp.get("status") or 0)
    return f"HTTP_{status}" if status else "unknown_error"


def _looks_like_media_id(value: str) -> bool:
    return bool(
        re.match(
            r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
            value,
        )
    )


def _collect_media_entries(obj: Any, out: list[dict], seen: set[str]) -> None:
    if isinstance(obj, dict):
        name = obj.get("name") or obj.get("mediaId")
        if name and isinstance(name, str) and name not in seen:
            if (
                obj.get("projectId")
                or obj.get("video")
                or obj.get("status")
                or _looks_like_media_id(name)
            ):
                seen.add(name)
                out.append(obj)
        for v in obj.values():
            _collect_media_entries(v, out, seen)
    elif isinstance(obj, list):
        for v in obj:
            _collect_media_entries(v, out, seen)


def normalize_submit_payload(resp: Any) -> dict:
    """Extract Google Flow JSON body from extension api_request callback."""
    if not isinstance(resp, dict):
        return {}
    candidates: list[dict] = []
    data = resp.get("data")
    if isinstance(data, dict):
        candidates.append(data)
        inner = data.get("data")
        if isinstance(inner, dict):
            candidates.append(inner)
    if resp.get("operations") is not None or resp.get("workflows") or resp.get("media"):
        candidates.append(resp)
    for c in candidates:
        if c.get("operations") or c.get("workflows") or c.get("media"):
            return c
    base = candidates[0] if candidates else {}
    media_found: list[dict] = []
    _collect_media_entries(resp, media_found, set())
    if media_found:
        merged = dict(base) if isinstance(base, dict) else {}
        existing = {m.get("name") or m.get("mediaId") for m in merged.get("media") or [] if isinstance(m, dict)}
        extra = [m for m in media_found if (m.get("name") or m.get("mediaId")) not in existing]
        if extra:
            merged["media"] = list(merged.get("media") or []) + extra
            return merged
    return base if isinstance(base, dict) else {}


async def _video_submit_request(
    client: FlowClient,
    url: str,
    body: dict,
    *,
    model_key: str,
    captcha_action: str = "VIDEO_GENERATION",
) -> dict:
    """Submit video job; retry reCAPTCHA / transient errors without changing upload media."""
    last_err = "video_submit_failed"
    attempts: list[dict] = []
    last_resp: dict = {}
    max_attempts = max(4, RECAPTCHA_RETRY_MAX)
    for attempt in range(max_attempts):
        resp = await client.api_request(
            url, body=body, captcha_action=captcha_action, timeout=300, raise_on_error=False
        )
        last_resp = resp if isinstance(resp, dict) else {}
        attempts.append(compact_api_response(resp, f"submit_attempt_{attempt + 1}"))
        payload = normalize_submit_payload(resp)
        if extract_video_operations(payload):
            return payload
        last_err = error_from_response(resp)
        status = int(resp.get("status") or 0)
        if status < 400:
            break
        if is_recaptcha_error(last_err) and attempt < max_attempts - 1:
            delay = _recaptcha_retry_delay(attempt)
            logger.warning(
                "video submit reCAPTCHA retry %s/%s: %s — wait %ss",
                attempt + 1,
                max_attempts,
                last_err,
                delay,
            )
            await asyncio.sleep(delay)
            continue
        if is_transient_flow_error(last_err) and attempt < 3:
            delay = min(300, (2**attempt) * 10)
            logger.warning(
                "video submit transient (attempt %s): %s — retry in %ss",
                attempt + 1,
                last_err,
                delay,
            )
            await asyncio.sleep(delay)
            continue
        break
    logger.warning(
        "video submit failed: model=%s err=%s snippet=%s",
        model_key,
        last_err,
        str(normalize_submit_payload(last_resp))[:500],
    )
    raise FlowApiError(
        last_err,
        step="video_submit",
        raw=last_resp,
        attempts=attempts,
    )


def extract_video_operations(data: dict) -> list[dict]:
    """Parse submit response: classic operations[] or Low Priority workflows/media."""
    if not isinstance(data, dict):
        return []

    ops = data.get("operations")
    if isinstance(ops, list) and ops:
        return [o for o in ops if isinstance(o, dict)]

    synthesized: list[dict] = []
    media_list = [m for m in (data.get("media") or []) if isinstance(m, dict)]
    media_by_id = {
        str(m.get("name") or m.get("mediaId")): m
        for m in media_list
        if m.get("name") or m.get("mediaId")
    }
    workflows = data.get("workflows") or []
    media_poll_names = [
        str(m.get("name") or m.get("mediaId"))
        for m in media_list
        if (m.get("name") or m.get("mediaId"))
    ]
    for wf_idx, wf in enumerate(workflows):
        if not isinstance(wf, dict):
            continue
        wf_name = wf.get("name", "") or wf.get("workflowId", "")
        primary = (wf.get("metadata") or {}).get("primaryMediaId", "")
        if not primary and not media_poll_names:
            continue
        # Flow web polls media[].name — often differs from workflow primaryMediaId.
        poll_id = str(primary) if primary else ""
        if media_poll_names and poll_id not in media_poll_names:
            poll_id = media_poll_names[wf_idx] if wf_idx < len(media_poll_names) else media_poll_names[0]
        elif not poll_id and media_poll_names:
            poll_id = media_poll_names[wf_idx] if wf_idx < len(media_poll_names) else media_poll_names[0]
        media_entry = media_by_id.get(poll_id) or media_by_id.get(str(primary)) or {}
        if not media_entry and media_list:
            media_entry = media_list[0]
        synthesized.append(
            {
                "operation": {
                    "name": wf_name or poll_id,
                    "metadata": {"video": {"mediaId": poll_id}},
                },
                "status": "MEDIA_GENERATION_STATUS_PENDING",
                "_workflow_mode": True,
                "_primary_media_id": str(poll_id),
                "_workflow_primary_media_id": str(primary) if primary else None,
                "_project_id": media_entry.get("projectId") or data.get("projectId"),
            }
        )

    if synthesized:
        logger.info("workflow-schema video submit: %d item(s)", len(synthesized))
        return synthesized

    for entry in media_list:
        mid = entry.get("name") or entry.get("mediaId")
        if mid:
            synthesized.append(
                {
                    "operation": {"name": mid, "metadata": {"video": {"mediaId": mid}}},
                    "status": "MEDIA_GENERATION_STATUS_PENDING",
                    "_workflow_mode": True,
                    "_primary_media_id": str(mid),
                    "_project_id": entry.get("projectId") or data.get("projectId"),
                }
            )
    if synthesized:
        logger.info("media-schema video submit: %d item(s)", len(synthesized))
    return synthesized


def _unwrap_poll_data(resp: dict) -> dict:
    data = resp.get("data") or {}
    if not isinstance(data, dict):
        return {}
    inner = data.get("data")
    if isinstance(inner, dict) and (inner.get("media") or inner.get("operations")):
        return inner
    return data


async def check_async_operations(client: FlowClient, operations: list[dict]) -> dict:
    resp = await client.api_request(
        _api_url(VIDEO_POLL_PATH),
        body={"operations": operations},
        timeout=120,
        raise_on_error=False,
    )
    status = int(resp.get("status") or 0)
    data = _unwrap_poll_data(resp)
    if status >= 400:
        err = error_from_response(resp)
        if isinstance(data, dict):
            data = {**data, "_transient_error": err, "_http_status": status}
        else:
            data = {"_transient_error": err, "_http_status": status}
    return data if isinstance(data, dict) else {}


async def check_async_media(
    client: FlowClient, project_id: str, media_ids: list[str]
) -> dict:
    """Poll video status via Flow web client shape: media[{name, projectId}]."""
    body = {
        "media": [{"name": mid, "projectId": project_id} for mid in media_ids if mid],
    }
    resp = await client.api_request(
        _api_url(VIDEO_POLL_PATH),
        body=body,
        timeout=120,
        raise_on_error=False,
    )
    status = int(resp.get("status") or 0)
    data = _unwrap_poll_data(resp)
    if status >= 400:
        err = error_from_response(resp)
        if isinstance(data, dict):
            data = {**data, "_transient_error": err, "_http_status": status}
        else:
            data = {"_transient_error": err, "_http_status": status}
    return data if isinstance(data, dict) else {}


def _video_url_from_block(video_block: dict) -> str | None:
    if not isinstance(video_block, dict):
        return None
    generated = video_block.get("generatedVideo") or {}
    return (
        generated.get("fifeUrl")
        or video_block.get("fifeUrl")
        or video_block.get("servingUri")
        or generated.get("servingUri")
    )


def _poll_entry_done(status: str, has_url: bool) -> bool:
    if has_url:
        return True
    st = str(status or "").upper()
    return st in (
        "MEDIA_GENERATION_STATUS_SUCCESSFUL",
        "MEDIA_GENERATION_STATUS_COMPLETE",
        "SUCCESSFUL",
        "COMPLETE",
        "DONE",
    ) or "SUCCESSFUL" in st


def _save_encoded_video(media_id: str, encoded: str) -> str | None:
    if not encoded:
        return None
    try:
        binary = base64.b64decode(encoded)
    except Exception:
        return None
    if len(binary) < 12 or binary[4:8] != b"ftyp":
        return None
    out_path = VIDEOS_DIR / f"{media_id}.mp4"
    out_path.write_bytes(binary)
    logger.info("saved poll video %s (%d bytes)", media_id[:8], len(binary))
    return f"/media/{media_id}"


def _poll_entry_failed(status: str) -> bool:
    st = str(status or "").upper()
    return "FAILED" in st or st == "MEDIA_GENERATION_STATUS_FAILED"


def parse_media_poll_result(data: dict) -> tuple[dict[str, str], bool, bool]:
    """Return completed media_id→url map, all_done, any_failed."""
    completed: dict[str, str] = {}
    any_failed = False
    pending_count = 0

    for entry in data.get("operations") or []:
        if not isinstance(entry, dict):
            continue
        op = entry.get("operation") or entry
        status = entry.get("status") or op.get("status") or ""
        video_meta = (op.get("metadata") or {}).get("video") or {}
        mid = video_meta.get("mediaId") or video_meta.get("name") or op.get("name")
        url = video_meta.get("fifeUrl") or video_meta.get("url")
        if not url:
            resp = entry.get("response") or op.get("response") or {}
            for v in (resp.get("videos") or []) if isinstance(resp, dict) else []:
                if isinstance(v, dict):
                    url = v.get("fifeUrl") or v.get("url")
                    mid = mid or v.get("mediaId") or v.get("name")
                    if url:
                        break
        if _poll_entry_failed(status):
            any_failed = True
        elif mid and _poll_entry_done(status, bool(url)):
            completed[str(mid)] = url or f"/media/{mid}"
        else:
            pending_count += 1

    for entry in data.get("media") or []:
        if not isinstance(entry, dict):
            continue
        mid = str(entry.get("name") or entry.get("mediaId") or "")
        if not mid:
            continue
        status = entry.get("status") or ""
        video_block = entry.get("video") or {}
        url = _video_url_from_block(video_block)
        encoded = ""
        if isinstance(video_block, dict):
            encoded = str(video_block.get("encodedVideo") or "")
        has_video = bool(url) or (len(encoded) > 200 and encoded.startswith("AAAA"))
        if _poll_entry_failed(status):
            any_failed = True
        elif _poll_entry_done(status, has_video):
            if not url and encoded:
                url = _save_encoded_video(mid, encoded)
            completed[mid] = url or f"/media/{mid}"
        else:
            pending_count += 1

    all_done = pending_count == 0 and bool(completed) and not any_failed
    if pending_count == 0 and completed:
        all_done = True
    return completed, all_done, any_failed


def _get_media_http_error(resp: dict, status: int) -> str:
    data = resp.get("data")
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        msg = data["error"].get("message") or data["error"].get("status")
        if msg:
            return str(msg)
    err = resp.get("error")
    if err:
        return str(err)
    return f"HTTP_{status}"


async def try_fetch_media_video_url(client: FlowClient, media_id: str) -> str | None:
    """GET /v1/media/{id} — Lower Priority / workflow models return MP4 inline."""
    resp = await client.get_media(media_id)
    status = int(resp.get("status") or 0)
    if status == 500:
        return None
    if status == 404:
        raise GetMedia404Error(_get_media_http_error(resp, status), raw=resp)
    if status >= 400:
        raise FlowApiError(
            _get_media_http_error(resp, status),
            step="get_media",
            raw=resp,
        )
    payload = resp.get("data") or resp
    if isinstance(payload, dict) and "video" not in payload and isinstance(payload.get("data"), dict):
        payload = payload["data"]
    video_block = (payload.get("video") or {}) if isinstance(payload, dict) else {}
    generated = (video_block.get("generatedVideo") or {}) if isinstance(video_block, dict) else {}
    url = (
        generated.get("fifeUrl")
        or video_block.get("fifeUrl")
        or video_block.get("servingUri")
        or (payload.get("fifeUrl") if isinstance(payload, dict) else None)
    )
    if url:
        return url
    encoded = video_block.get("encodedVideo", "") if isinstance(video_block, dict) else ""
    return _save_encoded_video(media_id, encoded) if encoded else None


async def poll_video_by_media(
    client: FlowClient,
    project_id: str,
    media_ids: list[str],
    max_rounds: int,
    *,
    should_abort=None,
    on_round=None,
    interleave_get_media: bool = True,
) -> tuple[list[str], list[str]]:
    """Poll batchCheckAsyncVideoGenerationStatus with media[{name, projectId}]."""
    pending = [mid for mid in dict.fromkeys(media_ids) if mid]
    if not pending:
        raise RuntimeError("missing_media_ids_for_poll")

    completed: dict[str, str] = {}
    transient_streak = 0
    poll_snapshots: list[dict] = []

    for round_idx in range(max_rounds):
        if should_abort:
            should_abort()
        remaining = [mid for mid in pending if mid not in completed]
        if not remaining:
            break

        poll = await check_async_media(client, project_id, remaining)
        poll_snapshots.append(
            {
                "round": round_idx + 1,
                "request": {"media": [{"name": m, "projectId": project_id} for m in remaining]},
                "response": compact_api_response(
                    {"status": poll.get("_http_status"), "data": poll},
                    f"poll_round_{round_idx + 1}",
                ),
            }
        )
        if len(poll_snapshots) > 8:
            poll_snapshots = poll_snapshots[-8:]

        transient = poll.get("_transient_error")
        if transient and not poll.get("media") and not poll.get("operations"):
            if is_transient_flow_error(str(transient)):
                transient_streak += 1
                await asyncio.sleep(min(30, POLL_INTERVAL_S * min(transient_streak, 6)))
                continue
            raise FlowApiError(
                str(transient),
                step="video_poll_media",
                attempts=poll_snapshots,
            )

        transient_streak = 0
        done_map, all_done, any_failed = parse_media_poll_result(poll)
        completed.update(done_map)

        if interleave_get_media and round_idx % 3 == 2:
            for mid in remaining:
                if mid in completed:
                    continue
                url = await try_fetch_media_video_url(client, mid)
                if url:
                    completed[mid] = url

        if on_round:
            try:
                on_round(round_idx + 1, poll, dict(completed))
            except Exception:
                pass

        if any_failed and not completed:
            raise FlowApiError(
                "video_generation_failed",
                step="video_poll_media",
                attempts=poll_snapshots,
            )
        if all_done or (completed and len(completed) >= len(pending)):
            return list(completed.values()), list(completed.keys())

        if round_idx % 10 == 9:
            logger.info("media poll round %s/%s done=%s", round_idx + 1, max_rounds, len(completed))
        await asyncio.sleep(POLL_INTERVAL_S)

    if completed:
        return list(completed.values()), list(completed.keys())
    raise FlowApiError(
        "timeout_waiting_video",
        step="video_poll_media",
        attempts=poll_snapshots,
    )


def collect_video_media_ids(operations: list[dict]) -> list[str]:
    ids: list[str] = []
    for op in operations:
        if not isinstance(op, dict):
            continue
        mid = op.get("_primary_media_id")
        if mid:
            ids.append(str(mid))
            continue
        video_meta = ((op.get("operation") or op).get("metadata") or {}).get("video") or {}
        mid = video_meta.get("mediaId") or video_meta.get("name")
        if mid:
            ids.append(str(mid))
    return list(dict.fromkeys(ids))


def collect_video_poll_media_ids(
    submit_raw: dict,
    operations: list[dict] | None = None,
) -> list[str]:
    """Media IDs for batchCheckAsync — prefer submit.media[].name (Flow UI shape)."""
    ids: list[str] = []
    if isinstance(submit_raw, dict):
        project_id = submit_raw.get("projectId")
        for entry in submit_raw.get("media") or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or entry.get("mediaId") or "").strip()
            if not name:
                continue
            if entry.get("projectId") or project_id:
                ids.append(name)
        if not ids:
            for entry in submit_raw.get("media") or []:
                if isinstance(entry, dict):
                    name = str(entry.get("name") or entry.get("mediaId") or "").strip()
                    if name:
                        ids.append(name)
    for mid in collect_video_media_ids(operations or []):
        if mid not in ids:
            ids.append(mid)
    return list(dict.fromkeys(ids))


def resolve_poll_project_id(
    submit_raw: dict,
    operations: list[dict],
    fallback: str,
) -> str:
    if isinstance(submit_raw, dict):
        for entry in submit_raw.get("media") or []:
            if isinstance(entry, dict) and entry.get("projectId"):
                return str(entry["projectId"])
        if submit_raw.get("projectId"):
            return str(submit_raw["projectId"])
    for op in operations:
        if op.get("_project_id"):
            return str(op["_project_id"])
    return fallback


def _urls_from_operations(operations: list[dict]) -> tuple[list[str], list[str]]:
    urls: list[str] = []
    media_ids: list[str] = []
    for entry in operations:
        if not isinstance(entry, dict):
            continue
        op = entry.get("operation") or entry
        video_meta = (op.get("metadata") or {}).get("video") or {}
        url = video_meta.get("fifeUrl") or video_meta.get("url")
        mid = video_meta.get("mediaId") or video_meta.get("name")
        if url:
            urls.append(url)
        if mid:
            media_ids.append(mid)
            if not url:
                urls.append(f"/media/{mid}")
        resp = entry.get("response") or op.get("response") or {}
        for v in (resp.get("videos") or []) if isinstance(resp, dict) else []:
            if isinstance(v, dict):
                u = v.get("fifeUrl") or v.get("url")
                m = v.get("mediaId") or v.get("name")
                if u:
                    urls.append(u)
                if m:
                    media_ids.append(m)
    return list(dict.fromkeys(urls)), list(dict.fromkeys(media_ids))


async def poll_workflow_videos(
    client: FlowClient,
    operations: list[dict],
    max_rounds: int,
    *,
    should_abort=None,
    on_round=None,
) -> tuple[list[str], list[str]]:
    """Poll Lower Priority / workflow models via GET /v1/media/{id}."""
    pending = {op["_primary_media_id"] for op in operations if op.get("_primary_media_id")}
    completed: dict[str, str] = {}

    for round_idx in range(max_rounds):
        if should_abort:
            should_abort()
        for mid in list(pending):
            if mid in completed:
                continue
            url = await try_fetch_media_video_url(client, mid)
            if url:
                completed[mid] = url

        if on_round:
            try:
                on_round(round_idx + 1, {"mode": "get_media"}, dict(completed))
            except Exception:
                pass

        if len(completed) >= len(pending) and pending:
            return list(completed.values()), list(completed.keys())
        await asyncio.sleep(POLL_INTERVAL_S)

    if completed:
        return list(completed.values()), list(completed.keys())
    raise RuntimeError("timeout_waiting_video")


def summarize_video_poll(data: dict) -> list[dict]:
    out: list[dict] = []
    for entry in data.get("operations") or []:
        if not isinstance(entry, dict):
            continue
        op = entry.get("operation") or entry
        name = op.get("name")
        status = (entry.get("status") or op.get("status") or "").upper()
        done = bool(
            entry.get("done")
            or op.get("done")
            or status == "MEDIA_GENERATION_STATUS_SUCCESSFUL"
        )
        media_entries: list[dict] = []
        video_meta = (op.get("metadata") or {}).get("video") or {}
        if video_meta.get("fifeUrl") or video_meta.get("mediaId"):
            media_entries.append(
                {
                    "url": video_meta.get("fifeUrl"),
                    "media_id": video_meta.get("mediaId"),
                    "mediaType": "video",
                }
            )
        resp = entry.get("response") or op.get("response") or {}
        videos = (resp.get("videos") or []) if isinstance(resp, dict) else []
        for v in videos:
            if isinstance(v, dict):
                url = v.get("fifeUrl") or v.get("url")
                mid = v.get("mediaId") or v.get("name")
                if url or mid:
                    media_entries.append({"url": url, "media_id": mid, "mediaType": "video"})
        err = entry.get("error") or op.get("error")
        out.append({"name": name, "done": done, "status": status, "media_entries": media_entries, "error": err})
    return out


def format_api_error(err: Any) -> str:
    """Shorten HTML / huge API errors for dashboard display."""
    if isinstance(err, dict):
        inner = err.get("error")
        if isinstance(inner, dict) and inner.get("message"):
            return str(inner["message"])
        if err.get("message"):
            return str(err["message"])
    text = str(err)
    if "'message':" in text or '"message":' in text:
        m = re.search(r"['\"]message['\"]\s*:\s*['\"]([^'\"]+)['\"]", text)
        if m:
            return m.group(1).strip()
    if "<code>" in text:
        m = re.search(r"<code>([^<]+)</code>", text)
        if m:
            return m.group(1).strip()
    if len(text) > 400:
        return text[:397] + "..."
    return text
