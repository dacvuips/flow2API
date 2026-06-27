"""
Google Flow SDK — gọi aisandbox-pa qua extension bridge.

Ported from Flow2API / flowkit patterns.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
import uuid
from typing import Any, Optional

import base64
import logging

import httpx

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
    "NANO_BANANA_2": "NARWHAL",
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
VIDEO_EDIT_PATH = "/v1/video:batchAsyncGenerateVideoEditVideo"
VIDEO_POLL_PATH = "/v1/video:batchCheckAsyncVideoGenerationStatus"
VIDEO_UPSAMPLE_PATH = "/v1/video:batchAsyncGenerateVideoUpsampleVideo"
UPLOAD_IMAGE_PATH = "/v1/flow/uploadImage"
UPLOAD_VIDEO_START_URL = "https://labs.google/fx/api/upload-video?action=start"
MAX_UPLOAD_VIDEO_BYTES = 100 * 1024 * 1024
UPSAMPLE_IMAGE_PATH = "/v1/flow/upsampleImage"
VIDEO_RESOLUTION_1080P = "VIDEO_RESOLUTION_1080P"
VIDEO_UPSAMPLE_MODEL_KEY = "veo_3_1_upsampler_1080p"
VEO_UPSAMPLE_RECREATE_SOURCE_ERROR = "Veo error: please recreate the original video"

OMNI_FLASH_QUALITY = "omni_flash"
OMNI_EDIT_MODEL_KEY = "abra_edit"
OMNI_FRAME_DURATIONS = (4, 6, 8, 10)
OMNI_COMPONENT_DURATION_DEFAULT = 10
OMNI_COMPONENT_WITH_VIDEO_DURATION_S = 8
OMNI_COMPONENT_WITH_VIDEO_END_FRAME = 240  # 8s @ 30fps
OMNI_COMPONENT_MAX_IMAGES_WITH_VIDEO = 5
OMNI_COMPONENT_MAX_IMAGES_ONLY = 7
OMNI_COMPONENT_MAX_VIDEOS = 1


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


def _is_video_edit_url(url: str) -> bool:
    return "batchAsyncGenerateVideoEditVideo" in str(url or "")


def _video_edit_request_body(project_id: str, tier: str, request_item: dict) -> dict:
    """Omni edit / V2V — must not include useV2ModelConfig (API rejects it)."""
    return {
        "mediaGenerationContext": {
            "batchId": str(uuid.uuid4()),
            "audioFailurePreference": "BLOCK_SILENCED_VIDEOS",
        },
        "clientContext": _client_context(project_id, tier),
        "requests": [request_item],
    }


def _sanitize_video_submit_body(url: str, body: dict) -> dict:
    if not _is_video_edit_url(url):
        return body
    out = json.loads(json.dumps(body))
    out.pop("useV2ModelConfig", None)
    return out


def _video_request_body(
    project_id: str, tier: str, request_item: dict, *, use_v2: bool = True
) -> dict:
    """Flow web v2 video submit (i2v / r2v / t2v lower priority)."""
    body: dict[str, Any] = {
        "mediaGenerationContext": {
            "batchId": str(uuid.uuid4()),
            "audioFailurePreference": "BLOCK_SILENCED_VIDEOS",
        },
        "clientContext": _client_context(project_id, tier),
        "requests": [request_item],
    }
    if use_v2:
        body["useV2ModelConfig"] = True
    return body


def _refresh_video_request_body_session(body: dict, client: FlowClient) -> dict:
    """Fresh sessionId + batchId before retry — first submit often gets INVALID_ARGUMENT."""
    if not isinstance(body, dict):
        return body
    out = json.loads(json.dumps(body))
    ctx = out.get("clientContext") or {}
    project_id = str(ctx.get("projectId") or "").strip()
    if not project_id:
        return out
    tier = str(ctx.get("userPaygateTier") or _require_tier(client))
    out["clientContext"] = _client_context(project_id, tier)
    out["mediaGenerationContext"] = {
        "batchId": str(uuid.uuid4()),
        "audioFailurePreference": "BLOCK_SILENCED_VIDEOS",
    }
    return out


def _t2v_request_body(project_id: str, tier: str, request_item: dict) -> dict:
    """Classic text-to-video (lite / fast / quality — non lower-priority)."""
    return {
        "clientContext": _client_context(project_id, tier),
        "requests": [request_item],
    }


def _is_low_priority_model_key(model_key: str) -> bool:
    return "_low_priority" in str(model_key or "")


def _structured_video_prompt(prompt: str) -> dict:
    return {"structuredPrompt": {"parts": [{"text": prompt}]}}


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


def _parse_upload_ids(data: Any) -> tuple[str, str]:
    """Extract media UUID and mediaGenerationId from uploadImage/uploadVideo response."""
    if not isinstance(data, dict):
        return "", ""
    media = data.get("media") if isinstance(data.get("media"), dict) else {}
    media_id = str(media.get("name") or "").strip()
    mg = data.get("mediaGenerationId")
    gen_id = ""
    if isinstance(mg, dict):
        gen_id = str(mg.get("mediaGenerationId") or "").strip()
    return media_id, gen_id


def _guess_upload_mime(raw: str, *, default: str = "image/jpeg") -> str:
    text = str(raw or "")
    if text.startswith("data:"):
        head = text.split(",", 1)[0]
        part = head.split(";", 1)[0]
        if part.startswith("data:") and len(part) > 5:
            return part[5:] or default
    return default


def _guess_upload_file_name(
    mime_type: str,
    file_name: str = "",
    *,
    fallback_stem: str = "upload",
) -> str:
    name = str(file_name or "").strip()
    if name:
        return name
    ext = {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/webp": "webp",
        "video/mp4": "mp4",
        "video/webm": "webm",
        "video/quicktime": "mov",
    }.get(str(mime_type or "").lower(), "bin")
    return f"{fallback_stem}.{ext}"


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


async def _upload_media_bytes(
    client: FlowClient,
    *,
    project_id: str,
    payload_b64: str,
    mime_type: str,
    file_name: str,
    bytes_field: str,
    api_path: str,
    failure_label: str,
    timeout: float = 120,
) -> dict[str, Any]:
    body = {
        "clientContext": {"projectId": project_id, "tool": "PINHOLE"},
        "fileName": file_name,
        bytes_field: _strip_data_url(payload_b64),
        "isHidden": False,
        "isUserUploaded": True,
        "mimeType": mime_type,
    }
    last_err = f"{failure_label}_failed"
    last_resp: dict = {}
    for attempt in range(RECAPTCHA_RETRY_MAX):
        resp = await client.api_request(
            _api_url(api_path), body=body, timeout=timeout, raise_on_error=False
        )
        last_resp = resp if isinstance(resp, dict) else {}
        data = resp.get("data") or {}
        media_id, media_generation_id = _parse_upload_ids(data if isinstance(data, dict) else {})
        if media_id or media_generation_id:
            out: dict[str, Any] = {
                "media_id": media_id or media_generation_id,
                "media_generation_id": media_generation_id or media_id,
                "project_id": project_id,
            }
            if isinstance(data, dict):
                if data.get("width") is not None:
                    out["width"] = data.get("width")
                if data.get("height") is not None:
                    out["height"] = data.get("height")
                if data.get("durationSeconds") is not None:
                    out["duration_seconds"] = data.get("durationSeconds")
            return out
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
        if is_upload_image_internal_error(resp) and attempt < RECAPTCHA_RETRY_MAX - 1:
            delay = _recaptcha_retry_delay(attempt)
            logger.warning(
                "upload internal error retry %s/%s: %s — wait %ss",
                attempt + 1,
                RECAPTCHA_RETRY_MAX,
                last_err,
                delay,
            )
            await asyncio.sleep(delay)
            continue
        break
    raise RuntimeError(f"{failure_label}_failed: {last_err} ({last_resp})")


async def upload_image(
    client: FlowClient,
    *,
    project_id: str,
    image_base64: str,
    mime_type: str = "image/jpeg",
    file_name: str = "upload.jpg",
) -> str:
    mime_type = mime_type or _guess_upload_mime(image_base64)
    file_name = _guess_upload_file_name(mime_type, file_name, fallback_stem="upload")
    result = await _upload_media_bytes(
        client,
        project_id=project_id,
        payload_b64=image_base64,
        mime_type=mime_type,
        file_name=file_name,
        bytes_field="imageBytes",
        api_path=UPLOAD_IMAGE_PATH,
        failure_label="upload_image",
        timeout=120,
    )
    return str(result["media_id"])


def _payload_from_upload_response(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            return data
        return payload
    return {}


def _extract_session_url(payload: Any) -> str:
    data = _payload_from_upload_response(payload)
    queue: list[Any] = [data]
    seen: set[int] = set()
    keys = ("sessionUrl", "session_url", "uploadUrl", "upload_url")
    while queue:
        node = queue.pop(0)
        node_id = id(node)
        if node_id in seen:
            continue
        seen.add(node_id)
        if isinstance(node, dict):
            for key in keys:
                val = str(node.get(key) or "").strip()
                if val.startswith("http"):
                    return val
            for val in node.values():
                if isinstance(val, dict):
                    queue.append(val)
    return ""


async def _finalize_video_upload(
    client: FlowClient,
    *,
    session_url: str,
    raw: bytes,
    mime_type: str,
) -> dict[str, Any]:
    token = str(getattr(client, "flow_key", None) or "").strip()
    if not token:
        raise RuntimeError("upload_video_failed: missing_flow_key")
    headers = {
        "Content-Type": mime_type,
        "Authorization": f"Bearer {token}",
        "X-Goog-Upload-Command": "upload, finalize",
        "X-Goog-Upload-Offset": "0",
    }
    async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as http:
        resp = await http.put(session_url, content=raw, headers=headers)
    if resp.status_code >= 400:
        snippet = (resp.text or "")[:500]
        raise RuntimeError(f"upload_video_failed: HTTP_{resp.status_code} ({snippet})")
    try:
        data = resp.json()
    except Exception:
        text = (resp.text or "").strip()
        if not text:
            raise RuntimeError("upload_video_failed: empty_finalize_response")
        raise RuntimeError(f"upload_video_failed: invalid_finalize_json ({text[:500]})")
    if not isinstance(data, dict):
        raise RuntimeError(f"upload_video_failed: unexpected_finalize_payload ({data!r})")
    return data


def _media_from_upload_payload(payload: dict[str, Any]) -> dict[str, Any]:
    media_id = str(
        payload.get("mediaId")
        or payload.get("name")
        or payload.get("id")
        or ""
    ).strip()
    media = payload.get("media") if isinstance(payload.get("media"), dict) else {}
    if not media_id:
        media_id = str(media.get("name") or media.get("mediaId") or "").strip()
    gen_id = ""
    mg = payload.get("mediaGenerationId")
    if isinstance(mg, dict):
        gen_id = str(mg.get("mediaGenerationId") or "").strip()
    if not media_id:
        media_id = gen_id
    if not gen_id:
        gen_id = media_id
    return {
        "media_id": media_id,
        "media_generation_id": gen_id,
        "width": payload.get("width"),
        "height": payload.get("height"),
        "duration_seconds": payload.get("durationSeconds"),
    }


async def upload_video(
    client: FlowClient,
    *,
    project_id: str,
    video_base64: str,
    mime_type: str = "video/mp4",
    file_name: str = "upload.mp4",
) -> dict[str, Any]:
    import base64

    mime_type = mime_type or _guess_upload_mime(video_base64, default="video/mp4")
    _guess_upload_file_name(mime_type, file_name, fallback_stem="upload")
    raw = base64.b64decode(_strip_data_url(video_base64))
    if not raw:
        raise RuntimeError("upload_video_failed: empty_video")
    if len(raw) > MAX_UPLOAD_VIDEO_BYTES:
        raise RuntimeError("upload_video_failed: file_too_large")

    logger.info(
        "upload_video start project=%s size=%.1fMB mime=%s",
        project_id[:12],
        len(raw) / (1024 * 1024),
        mime_type,
    )
    start_resp = await client.labs_upload_video_start(
        project_id=project_id,
        content_type=mime_type,
        content_length=len(raw),
    )
    start_status = int(start_resp.get("status") or 0)
    if start_status >= 400 or start_resp.get("error"):
        raise RuntimeError(
            f"upload_video_failed: {error_from_response(start_resp)} ({start_resp})"
        )
    session_url = _extract_session_url(start_resp)
    if not session_url:
        raise RuntimeError(f"upload_video_failed: missing_session_url ({start_resp})")

    logger.info("upload_video finalize PUT %.1fMB → %s", len(raw) / (1024 * 1024), session_url[:80])
    finalize_data = await _finalize_video_upload(
        client,
        session_url=session_url,
        raw=raw,
        mime_type=mime_type,
    )
    result = _media_from_upload_payload(finalize_data)
    if not result.get("media_id"):
        raise RuntimeError(f"upload_video_failed: missing_media_id ({finalize_data})")
    result["project_id"] = project_id
    return result


async def upload_media(
    client: FlowClient,
    *,
    project_id: str,
    payload_base64: str,
    mime_type: str = "",
    file_name: str = "",
) -> dict[str, Any]:
    mime = mime_type or _guess_upload_mime(payload_base64)
    if mime.startswith("video/"):
        return await upload_video(
            client,
            project_id=project_id,
            video_base64=payload_base64,
            mime_type=mime,
            file_name=file_name,
        )
    media_id = await upload_image(
        client,
        project_id=project_id,
        image_base64=payload_base64,
        mime_type=mime or "image/jpeg",
        file_name=file_name or "upload.jpg",
    )
    return {"media_id": media_id, "media_generation_id": media_id, "project_id": project_id}


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


def _image_entry_url(entry: dict) -> str | None:
    url = entry.get("fifeUrl") or entry.get("imageUri") or entry.get("url")
    if url:
        return str(url)
    encoded = entry.get("encodedImage")
    if encoded:
        return f"data:image/jpeg;base64,{encoded}"
    return None


def extract_image_urls(data: dict) -> list[str]:
    urls: list[str] = []
    for entry in _iter_generated_images(data):
        url = _image_entry_url(entry)
        if url:
            urls.append(url)
    return urls


def build_image_media_entries(data: dict) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for entry in _iter_generated_images(data):
        url = _image_entry_url(entry)
        mid = entry.get("mediaId") or entry.get("name")
        if url or mid:
            entries.append({"url": url, "media_id": mid, "kind": "image"})
    return entries


def _unwrap_get_media_payload(resp: dict) -> dict:
    payload = resp.get("data") or resp
    if not isinstance(payload, dict):
        return {}
    if "video" not in payload and "image" not in payload:
        inner = payload.get("data")
        if isinstance(inner, dict):
            payload = inner
    return payload if isinstance(payload, dict) else {}


def parse_get_media_image(resp: dict) -> tuple[str | None, bytes | None, str]:
    """Return (remote_url, raw_bytes, mime_type) from GET /v1/media/{id}."""
    payload = _unwrap_get_media_payload(resp)
    image_block = (payload.get("image") or {}) if isinstance(payload, dict) else {}
    generated = (
        (image_block.get("generatedImage") or {})
        if isinstance(image_block, dict)
        else {}
    )
    url = (
        generated.get("fifeUrl")
        or generated.get("imageUri")
        or image_block.get("fifeUrl")
        or (payload.get("fifeUrl") if isinstance(payload, dict) else None)
    )
    encoded = (
        generated.get("encodedImage")
        or image_block.get("encodedImage")
        or ""
    )
    if encoded:
        try:
            raw = base64.b64decode(encoded)
            mime = (
                "image/png"
                if raw[:8] == b"\x89PNG\r\n\x1a\n"
                else "image/jpeg"
            )
            return None, raw, mime
        except Exception:
            pass
    if url:
        return str(url), None, "image/jpeg"
    return None, None, "image/jpeg"


def extract_image_media_ids(data: dict) -> list[str]:
    ids: list[str] = []
    for entry in _iter_generated_images(data):
        mid = entry.get("mediaId") or entry.get("name")
        if mid:
            ids.append(mid)
    return ids


def parse_upsample_image_response(data: Any) -> dict[str, Any]:
    """Extract url / media_id / encoded_image from upsampleImage response."""
    if not isinstance(data, dict):
        return {}

    payload: dict = data
    if isinstance(payload.get("data"), dict):
        inner = payload["data"]
        if inner.get("encodedImage") or inner.get("mediaId") or inner.get("fifeUrl"):
            payload = inner

    encoded = str(payload.get("encodedImage") or "")
    mid = str(payload.get("mediaId") or payload.get("name") or "")
    url = payload.get("fifeUrl") or payload.get("imageUri") or payload.get("url")

    for entry in payload.get("media") or []:
        if not isinstance(entry, dict):
            continue
        gen = (entry.get("image") or {}).get("generatedImage")
        if not isinstance(gen, dict):
            gen = entry.get("image") if isinstance(entry.get("image"), dict) else entry
        if not isinstance(gen, dict):
            continue
        encoded = encoded or str(gen.get("encodedImage") or "")
        mid = mid or str(gen.get("mediaId") or gen.get("name") or "")
        url = url or gen.get("fifeUrl") or gen.get("imageUri") or gen.get("url")

    out: dict[str, Any] = {}
    if url:
        out["url"] = str(url)
    if mid:
        out["media_id"] = mid
    if encoded:
        out["encoded_image"] = encoded
    return out


async def upsample_image(
    client: FlowClient,
    *,
    media_id: str,
    project_id: str,
    target_resolution: str = "UPSAMPLE_IMAGE_RESOLUTION_4K",
) -> dict[str, Any]:
    """Upscale a generated image via /v1/flow/upsampleImage (2K or 4K)."""
    tier = _require_tier(client)
    media_id = str(media_id or "").strip()
    if not media_id:
        raise ValueError("missing_media_id")

    last_err = "upsample_image_failed"
    last_resp: dict = {}
    for attempt in range(RECAPTCHA_RETRY_MAX):
        body = {
            "mediaId": media_id,
            "targetResolution": target_resolution,
            "clientContext": _client_context(project_id, tier),
        }
        resp = await client.api_request(
            _api_url(UPSAMPLE_IMAGE_PATH),
            body=body,
            captcha_action="IMAGE_GENERATION",
            timeout=300,
            raise_on_error=False,
        )
        last_resp = resp if isinstance(resp, dict) else {}
        status = int(resp.get("status") or 0)
        if status < 400:
            data = resp.get("data") or {}
            parsed = parse_upsample_image_response(data)
            if parsed:
                return parsed
            return {"raw": data} if data else {}
        last_err = error_from_response(resp)
        if is_recaptcha_error(last_err) and attempt < RECAPTCHA_RETRY_MAX - 1:
            delay = _recaptcha_retry_delay(attempt)
            logger.warning(
                "upsample reCAPTCHA retry %s/%s: %s — wait %ss",
                attempt + 1,
                RECAPTCHA_RETRY_MAX,
                last_err,
                delay,
            )
            await asyncio.sleep(delay)
            continue
        break
    raise FlowApiError(last_err, step="upsample_image", raw=last_resp)


def normalize_source_video_media_id(media_id: str) -> str:
    """Upsample input must be the generated video UUID, not {id}_upsampled."""
    mid = str(media_id or "").strip()
    if mid.endswith("_upsampled"):
        return mid[: -len("_upsampled")]
    return mid


def extract_workflow_id_from_submit(data: dict | None) -> str:
    """Flow upsample ties to the original video workflow id (workflows[].name)."""
    if not isinstance(data, dict):
        return ""
    for wf in data.get("workflows") or []:
        if not isinstance(wf, dict):
            continue
        name = str(wf.get("name") or wf.get("workflowId") or "").strip()
        if name:
            return name
    for entry in data.get("media") or []:
        if not isinstance(entry, dict):
            continue
        wid = str(entry.get("workflowId") or "").strip()
        if wid:
            return wid
    return ""


def extract_workflow_id_from_result(result: dict | None) -> str:
    if not isinstance(result, dict):
        return ""
    wid = str(result.get("workflow_id") or "").strip()
    if wid:
        return wid
    for entry in result.get("api_attempts") or []:
        if not isinstance(entry, dict):
            continue
        payload = entry.get("data")
        if isinstance(payload, dict):
            found = extract_workflow_id_from_submit(payload)
            if found:
                return found
        response = entry.get("response")
        if isinstance(response, dict):
            found = extract_workflow_id_from_submit(response)
            if found:
                return found
    return ""


async def upsample_video(
    client: FlowClient,
    *,
    media_id: str,
    project_id: str,
    aspect_ratio: str = "16:9",
    workflow_id: str = "",
    max_poll_rounds: int = 240,
) -> dict[str, Any]:
    """Upscale a generated video to 1080p via batchAsyncGenerateVideoUpsampleVideo."""
    media_id = normalize_source_video_media_id(media_id)
    if not media_id:
        raise ValueError("missing_media_id")

    aspect = VIDEO_ASPECT.get(aspect_ratio, VIDEO_ASPECT["16:9"])
    workflow_id = str(workflow_id or "").strip()
    metadata: dict[str, str] = {}
    if workflow_id:
        metadata["workflowId"] = workflow_id
    request_item = {
        "resolution": VIDEO_RESOLUTION_1080P,
        "aspectRatio": aspect,
        "videoModelKey": VIDEO_UPSAMPLE_MODEL_KEY,
        "seed": random.randint(1, 99_999),
        "videoInput": {"mediaId": media_id},
    }
    if metadata:
        request_item["metadata"] = metadata
    last_exc: FlowApiError | None = None
    submit_raw: dict | None = None
    max_attempts = max(4, RECAPTCHA_RETRY_MAX)
    for attempt in range(max_attempts):
        await client.fetch_paygate_tier()
        tier = _require_tier(client)
        body = _video_request_body(project_id, tier, request_item)
        try:
            submit_raw = await _video_submit_request(
                client,
                _api_url(VIDEO_UPSAMPLE_PATH),
                body,
                model_key=VIDEO_UPSAMPLE_MODEL_KEY,
                error_step="video_upsample_submit",
            )
            break
        except FlowApiError as exc:
            last_exc = exc
            msg = str(exc)
            mapped = veo_upsample_recreate_source_public_error(
                msg,
                exc,
                request_type="upsample_video",
            )
            if mapped:
                raise FlowApiError(
                    mapped,
                    step="video_upsample_submit",
                    raw=exc.raw,
                    attempts=exc.attempts,
                ) from exc
            retryable = (
                is_recaptcha_error(msg)
                or is_transient_flow_error(msg)
                or is_invalid_argument_retry_failure(exc, msg)
            )
            if retryable and attempt < max_attempts - 1:
                delay = _recaptcha_retry_delay(attempt)
                logger.warning(
                    "upsample video submit retry %s/%s: %s — wait %ss",
                    attempt + 1,
                    max_attempts,
                    msg,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            raise
    if not submit_raw:
        raise last_exc or FlowApiError(
            VEO_UPSAMPLE_RECREATE_SOURCE_ERROR,
            step="video_upsample_submit",
        )
    operations = extract_video_operations(submit_raw)
    media_ids = collect_video_poll_media_ids(submit_raw, operations)
    poll_project_id = resolve_poll_project_id(submit_raw, operations, project_id)
    if not media_ids:
        raise FlowApiError(
            "upsample_video_no_media_id",
            step="video_upsample_submit",
            raw=submit_raw,
        )

    urls, out_media_ids = await poll_workflow_videos(
        client,
        operations,
        max_poll_rounds,
        project_id=poll_project_id,
    )
    return {
        "video_urls": urls,
        "media_ids": out_media_ids,
        "project_id": poll_project_id,
        "source_media_id": media_id,
        "resolution": VIDEO_RESOLUTION_1080P,
    }


def _video_model_key(mode: str, tier: str, aspect: str, quality: str) -> str:
    table = VIDEO_MODEL_KEYS.get(mode) or VIDEO_MODEL_KEYS["t2v"]
    tier_map = table.get(tier) or table["PAYGATE_TIER_ONE"]
    aspect_map = tier_map.get(aspect) or tier_map["16:9"]
    key = aspect_map.get(quality)
    if not key:
        raise ValueError(f"unsupported video model {quality} for {mode} {aspect} / {tier}")
    return key


def is_omni_flash(quality: str) -> bool:
    q = str(quality or "").strip().lower().replace("-", "_")
    if q in ("omni_flash", "omni"):
        return True
    compact = q.replace("_", "")
    return compact in ("omniflash", "omni")


def omni_frame_model_key(duration_s: int) -> str:
    dur = int(duration_s or 4)
    if dur not in OMNI_FRAME_DURATIONS:
        raise ValueError(f"unsupported_omni_duration:{dur}")
    return f"abra_i2v_{dur}s"


def omni_t2v_model_key(duration_s: int) -> str:
    dur = int(duration_s or 4)
    if dur not in OMNI_FRAME_DURATIONS:
        raise ValueError(f"unsupported_omni_duration:{dur}")
    return f"abra_t2v_{dur}s"


async def gen_omni_frame_video(
    client: FlowClient,
    *,
    project_id: str,
    prompt: str,
    aspect_ratio: str,
    start_media_id: str,
    duration_s: int = 4,
) -> dict:
    """Omni Flash Khung hình — startImage only, abra_i2v_{N}s."""
    tier = _require_tier(client)
    model_key = omni_frame_model_key(duration_s)
    request_item = {
        "aspectRatio": VIDEO_ASPECT.get(aspect_ratio, VIDEO_ASPECT["16:9"]),
        "seed": random.randint(1, 99_999),
        "textInput": _structured_video_prompt(prompt),
        "videoModelKey": model_key,
        "metadata": {},
        "startImage": {"mediaId": start_media_id},
    }
    body = _video_request_body(project_id, tier, request_item)
    return await _video_submit_request(
        client, _api_url(VIDEO_START_PATH), body, model_key=model_key
    )


async def gen_omni_edit_video(
    client: FlowClient,
    *,
    project_id: str,
    prompt: str,
    aspect_ratio: str,
    reference_media_ids: list[str],
    source_video_media_id: str,
    end_frame_index: int = OMNI_COMPONENT_WITH_VIDEO_END_FRAME,
) -> dict:
    """Omni Flash Thành phần V2V — abra_edit + videoInput (optional referenceImages)."""
    video_id = str(source_video_media_id or "").strip()
    if not video_id:
        raise ValueError("omni_edit_requires_source_video")
    tier = _require_tier(client)
    aspect = VIDEO_ASPECT.get(aspect_ratio, VIDEO_ASPECT["16:9"])
    request_item: dict[str, Any] = {
        "aspectRatio": aspect,
        "seed": random.randint(1, 99_999),
        "textInput": _structured_video_prompt(prompt),
        "videoModelKey": OMNI_EDIT_MODEL_KEY,
        "metadata": {},
        "videoInput": {
            "mediaId": video_id,
            "startFrameIndex": 0,
            "endFrameIndex": int(end_frame_index or OMNI_COMPONENT_WITH_VIDEO_END_FRAME),
        },
    }
    if reference_media_ids:
        request_item["referenceImages"] = [
            {"mediaId": mid, "imageUsageType": "IMAGE_USAGE_TYPE_ASSET"}
            for mid in reference_media_ids
        ]
    body = _video_edit_request_body(project_id, tier, request_item)
    return await _video_submit_request(
        client, _api_url(VIDEO_EDIT_PATH), body, model_key=OMNI_EDIT_MODEL_KEY
    )


async def gen_omni_reference_video(
    client: FlowClient,
    *,
    project_id: str,
    prompt: str,
    aspect_ratio: str,
    reference_media_ids: list[str],
    duration_s: int = OMNI_COMPONENT_DURATION_DEFAULT,
) -> dict:
    """Omni Flash Thành phần (ảnh only) — abra_t2v_{N}s + referenceImages."""
    if not reference_media_ids:
        raise ValueError("omni_reference_requires_images")
    tier = _require_tier(client)
    model_key = omni_t2v_model_key(duration_s)
    request_item = {
        "aspectRatio": VIDEO_ASPECT.get(aspect_ratio, VIDEO_ASPECT["16:9"]),
        "seed": random.randint(1, 99_999),
        "textInput": _structured_video_prompt(prompt),
        "videoModelKey": model_key,
        "metadata": {},
        "referenceImages": [
            {"mediaId": mid, "imageUsageType": "IMAGE_USAGE_TYPE_ASSET"}
            for mid in reference_media_ids
        ],
    }
    body = _video_request_body(project_id, tier, request_item)
    return await _video_submit_request(
        client, _api_url(VIDEO_REF_PATH), body, model_key=model_key
    )


async def gen_omni_text_video(
    client: FlowClient,
    *,
    project_id: str,
    prompt: str,
    aspect_ratio: str,
    duration_s: int = OMNI_COMPONENT_DURATION_DEFAULT,
) -> dict:
    """Omni Flash text-only — abra_t2v_{N}s on batchAsyncGenerateVideoText."""
    tier = _require_tier(client)
    model_key = omni_t2v_model_key(duration_s)
    request_item = {
        "aspectRatio": VIDEO_ASPECT.get(aspect_ratio, VIDEO_ASPECT["16:9"]),
        "seed": random.randint(1, 99_999),
        "textInput": _structured_video_prompt(prompt),
        "videoModelKey": model_key,
        "metadata": {},
    }
    body = _video_request_body(project_id, tier, request_item)
    return await _video_submit_request(
        client, _api_url(VIDEO_T2V_PATH), body, model_key=model_key
    )


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
    aspect = VIDEO_ASPECT.get(aspect_ratio, VIDEO_ASPECT["16:9"])
    seed = int(time.time() * 1000) % 1_000_000
    if _is_low_priority_model_key(model_key):
        request_item = {
            "aspectRatio": aspect,
            "seed": seed,
            "textInput": _structured_video_prompt(prompt),
            "videoModelKey": model_key,
            "metadata": {},
        }
        body = _video_request_body(project_id, tier, request_item)
    else:
        request_item = {
            "aspectRatio": aspect,
            "seed": seed,
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
        if isinstance(raw, dict) and get_media_http_status(raw) == 404:
            return True
    text = str(exc).lower()
    if "requested entity was not found" in text:
        return True
    return "HTTP_404" in str(exc)


def get_media_http_status(resp: dict) -> int:
    """Resolve HTTP status from extension callback or compact api_trace entry."""
    status = int(resp.get("status") or resp.get("http_status") or 0)
    if status:
        return status
    data = resp.get("data")
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            code = err.get("code")
            if code is not None:
                try:
                    return int(code)
                except (TypeError, ValueError):
                    pass
            if str(err.get("status") or "").upper() == "NOT_FOUND":
                return 404
    return 0


def is_get_media_404_message(msg: str) -> bool:
    text = str(msg or "").lower()
    return "requested entity was not found" in text or "http_404" in text


def _get_media_error_status(resp: dict) -> str:
    data = resp.get("data")
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("status") or "").upper()
    if isinstance(resp.get("error"), dict):
        return str(resp["error"].get("status") or "").upper()
    return ""


def is_get_media_not_ready_error(exc: Exception) -> bool:
    """GET /v1/media before render completes — 404 or 400 INVALID_ARGUMENT."""
    if isinstance(exc, GetMedia404Error):
        return True
    if not isinstance(exc, FlowApiError) or exc.step != "get_media":
        return False
    raw = exc.raw
    if isinstance(raw, dict):
        status = get_media_http_status(raw)
        if status == 404:
            return True
        if status == 400 and _get_media_error_status(raw) == "INVALID_ARGUMENT":
            return True
    msg = str(exc).lower()
    return "invalid argument" in msg or "http_404" in msg


def is_get_media_404_failure(
    exc: Exception,
    msg: str = "",
    api_trace: list[dict] | None = None,
) -> bool:
    if is_get_media_404_error(exc):
        return True
    if is_get_media_404_message(msg):
        return True
    for entry in api_trace or []:
        if entry.get("label") != "get_media":
            continue
        if int(entry.get("http_status") or 0) == 404:
            return True
        data = entry.get("data")
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict) and int(err.get("code") or 0) == 404:
                return True
            if str(err.get("status") or "").upper() == "NOT_FOUND":
                return True
    return False


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


def is_upload_image_internal_error(resp: dict) -> bool:
    """HTTP 500 INTERNAL from /v1/flow/uploadImage — transient Google backend fault."""
    status = int(resp.get("status") or 0)
    data = resp.get("data")
    if not isinstance(data, dict):
        return False
    err = data.get("error")
    if not isinstance(err, dict):
        return False
    code = int(err.get("code") or 0)
    if status != 500 and code != 500:
        return False
    msg = str(err.get("message") or "").strip().lower()
    err_status = str(err.get("status") or "").strip().upper()
    return err_status == "INTERNAL" or msg == "internal error encountered."


def is_upload_image_internal_failure(
    exc: Exception,
    msg: str = "",
    api_trace: list[dict] | None = None,
) -> bool:
    for entry in api_trace or []:
        if entry.get("label") != "upload_image":
            continue
        pseudo = {"status": entry.get("http_status"), "data": entry.get("data")}
        if is_upload_image_internal_error(pseudo):
            return True
    text = str(msg or exc or "").lower()
    return "upload_image_failed" in text and "internal error encountered" in text


def _payload_has_trpc_401(payload: Any) -> bool:
    if payload is None:
        return False
    if isinstance(payload, dict):
        text = json.dumps(payload, ensure_ascii=False)
    else:
        text = str(payload)
    upper = text.upper()
    return "TRPC_401" in upper


def is_trpc_401_failure(
    exc: Exception | None = None,
    msg: str = "",
    api_trace: list[dict] | None = None,
) -> bool:
    if _payload_has_trpc_401(msg):
        return True
    if exc is not None:
        if _payload_has_trpc_401(str(exc)):
            return True
        if isinstance(exc, FlowApiError):
            if _payload_has_trpc_401(exc.raw):
                return True
            for attempt in exc.attempts or []:
                if _payload_has_trpc_401(attempt):
                    return True
                response = attempt.get("response") if isinstance(attempt, dict) else None
                if isinstance(response, dict):
                    if _payload_has_trpc_401(response):
                        return True
                    if _payload_has_trpc_401(response.get("data")):
                        return True
    for entry in api_trace or []:
        if _payload_has_trpc_401(entry.get("data")):
            return True
        if _payload_has_trpc_401(entry):
            return True
    return False


def is_extension_timeout_error(msg: str = "", exc: Exception | None = None) -> bool:
    """Extension bridge did not respond in time — safe to retry after a short wait."""
    text = str(msg or "").strip().lower()
    if text == "extension_timeout" or text.startswith("extension_timeout"):
        return True
    if exc is None:
        return False
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True
    return "extension_timeout" in str(exc).lower()


def is_extension_disconnect_error(msg: str = "", exc: Exception | None = None) -> bool:
    """Extension WS dropped mid-request — often a startup reconnect race; safe to retry."""
    text = str(msg or "").strip().lower()
    if text == "extension_disconnected" or text.startswith("extension_disconnected"):
        return True
    if exc is None:
        return False
    if isinstance(exc, ConnectionError):
        return True
    return "extension disconnected" in str(exc).lower()


_RECAPTCHA_ERR_RE = re.compile(r"re\s*[-_]?\s*captcha|captcha", re.IGNORECASE)


def is_recaptcha_error(msg: str) -> bool:
    """reCAPTCHA / RECAPTCHA / captcha — không phân biệt hoa thường."""
    text = str(msg or "")
    if not text:
        return False
    return bool(_RECAPTCHA_ERR_RE.search(text))


def recaptcha_retry_delay(attempt: int = 0) -> float:
    """Chờ ngẫu nhiên 3–5s trước mỗi lần retry reCAPTCHA (không retry ngay)."""
    del attempt
    return random.uniform(3.0, 5.0)


def _recaptcha_retry_delay(attempt: int) -> float:
    return recaptcha_retry_delay(attempt)


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
    error_step: str = "video_submit",
) -> dict:
    """Submit video job; retry reCAPTCHA / transient errors without changing upload media."""
    last_err = "video_submit_failed"
    attempts: list[dict] = []
    last_resp: dict = {}
    max_attempts = max(4, RECAPTCHA_RETRY_MAX)
    for attempt in range(max_attempts):
        submit_body = _sanitize_video_submit_body(url, body)
        resp = await client.api_request(
            url, body=submit_body, captcha_action=captcha_action, timeout=300, raise_on_error=False
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
            body = _sanitize_video_submit_body(
                url, _refresh_video_request_body_session(body, client)
            )
            continue
        if (
            is_invalid_argument_retry_failure(None, last_err, attempts)
            or _payload_has_invalid_argument_retry(last_resp)
        ) and attempt < max_attempts - 1:
            if "useV2ModelConfig" in str(last_err or ""):
                body = _sanitize_video_submit_body(url, body)
                logger.warning(
                    "video submit stripped useV2ModelConfig for edit endpoint (attempt %s)",
                    attempt + 1,
                )
            delay = _recaptcha_retry_delay(attempt)
            logger.warning(
                "video submit INVALID_ARGUMENT retry %s/%s: %s — wait %ss",
                attempt + 1,
                max_attempts,
                last_err,
                delay,
            )
            await asyncio.sleep(delay)
            body = _sanitize_video_submit_body(
                url, _refresh_video_request_body_session(body, client)
            )
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
        step=error_step,
        raw=last_resp,
        attempts=attempts,
    )


def _is_upsample_media_entry(entry: dict) -> bool:
    if not isinstance(entry, dict):
        return False
    name = str(entry.get("name") or entry.get("mediaId") or "")
    if name.endswith("_upsampled"):
        return True
    video = entry.get("video") or {}
    generated = (video.get("generatedVideo") or {}) if isinstance(video, dict) else {}
    return bool((generated.get("upsampleMetadata") or {}).get("videoUpsampleResolution"))


def _upsample_poll_media_names(data: dict) -> list[str]:
    """Upsample submit returns media[].name like {sourceId}_upsampled — poll that, not primaryMediaId."""
    names: list[str] = []
    for entry in data.get("media") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or entry.get("mediaId") or "").strip()
        if name and _is_upsample_media_entry(entry):
            names.append(name)
    if names:
        return list(dict.fromkeys(names))
    for entry in data.get("operations") or []:
        if not isinstance(entry, dict):
            continue
        op = entry.get("operation") or entry
        name = str(op.get("name") or "").strip()
        if name.endswith("_upsampled"):
            names.append(name)
    return list(dict.fromkeys(names))


def _enrich_video_operations(data: dict, ops: list[dict]) -> list[dict]:
    """Attach poll ids / project ids — upsample uses operation.name = {uuid}_upsampled."""
    media_list = [m for m in (data.get("media") or []) if isinstance(m, dict)]
    media_by_name = {
        str(m.get("name") or m.get("mediaId")): m
        for m in media_list
        if m.get("name") or m.get("mediaId")
    }
    upsample_names = _upsample_poll_media_names(data)
    default_project = str(data.get("projectId") or "").strip()
    for wf in data.get("workflows") or []:
        if isinstance(wf, dict) and wf.get("projectId"):
            default_project = str(wf["projectId"])
            break

    enriched: list[dict] = []
    for idx, op in enumerate(ops):
        if not isinstance(op, dict):
            continue
        copy = dict(op)
        op_inner = copy.get("operation") or copy
        poll_id = str(copy.get("_primary_media_id") or "").strip()
        if not poll_id:
            video_meta = (op_inner.get("metadata") or {}).get("video") or {}
            poll_id = str(
                video_meta.get("mediaId") or video_meta.get("name") or op_inner.get("name") or ""
            ).strip()
        if upsample_names:
            poll_id = upsample_names[idx] if idx < len(upsample_names) else upsample_names[0]
        elif not poll_id and media_list:
            entry = media_list[idx] if idx < len(media_list) else media_list[0]
            poll_id = str(entry.get("name") or entry.get("mediaId") or "").strip()

        media_entry = media_by_name.get(poll_id) or {}
        project_id = (
            copy.get("_project_id")
            or media_entry.get("projectId")
            or default_project
        )
        if poll_id:
            copy["_primary_media_id"] = poll_id
        if project_id:
            copy["_project_id"] = str(project_id)
        if upsample_names or media_list or (data.get("workflows") or []):
            copy["_workflow_mode"] = True
        enriched.append(copy)
    return enriched


def extract_video_operations(data: dict) -> list[dict]:
    """Parse submit response: classic operations[] or Low Priority workflows/media."""
    if not isinstance(data, dict):
        return []

    ops = data.get("operations")
    if isinstance(ops, list) and ops:
        raw_ops = [o for o in ops if isinstance(o, dict)]
        if raw_ops:
            return _enrich_video_operations(data, raw_ops)

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


def _extract_poll_status(entry: dict) -> str:
    """Read generation status from Flow poll payloads (operations or media entries)."""
    if not isinstance(entry, dict):
        return ""
    for key in ("status",):
        val = entry.get(key)
        if val:
            return str(val)
    op = entry.get("operation")
    if isinstance(op, dict) and op.get("status"):
        return str(op["status"])
    meta = entry.get("mediaMetadata") or {}
    if isinstance(meta, dict):
        media_status = meta.get("mediaStatus") or {}
        if isinstance(media_status, dict):
            for key in ("mediaGenerationStatus", "status"):
                val = media_status.get(key)
                if val:
                    return str(val)
    return ""


def _media_local_path_exists(media_id: str) -> bool:
    return (VIDEOS_DIR / f"{media_id}.mp4").is_file()


def _media_url_is_resolved(url: str, media_id: str) -> bool:
    text = str(url or "").strip()
    if text and not text.startswith("/media/"):
        return True
    return _media_local_path_exists(media_id)


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
        status = _extract_poll_status(entry)
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


def _poll_entry_failure_message(entry: dict) -> str:
    if not isinstance(entry, dict):
        return ""
    meta = entry.get("mediaMetadata") or {}
    if isinstance(meta, dict):
        media_status = meta.get("mediaStatus") or {}
        if isinstance(media_status, dict):
            err = media_status.get("error")
            if isinstance(err, dict):
                msg = err.get("message") or err.get("status")
                if msg:
                    return str(msg)
    op = entry.get("operation") or entry
    if isinstance(op, dict):
        err = entry.get("error") or op.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("status")
            if msg:
                return str(msg)
        if err:
            return str(err)
    return ""


def extract_media_poll_failure_error(data: dict) -> str:
    """First API error message from a failed video media poll response."""
    if not isinstance(data, dict):
        return ""
    for entry in data.get("media") or []:
        if not isinstance(entry, dict):
            continue
        if not _poll_entry_failed(_extract_poll_status(entry)):
            continue
        msg = _poll_entry_failure_message(entry)
        if msg:
            return msg
    for entry in data.get("operations") or []:
        if not isinstance(entry, dict):
            continue
        op = entry.get("operation") or entry
        status = entry.get("status") or op.get("status") or ""
        if not _poll_entry_failed(status):
            continue
        msg = _poll_entry_failure_message(entry)
        if msg:
            return msg
    return ""


def _payload_has_prominent_people_filter(payload: Any) -> bool:
    if payload is None:
        return False
    if isinstance(payload, dict):
        text = json.dumps(payload, ensure_ascii=False)
    else:
        text = str(payload)
    upper = text.upper()
    return (
        "PUBLIC_ERROR_PROMINENT_PEOPLE_FILTER_FAILED" in upper
        or "PROMINENT_PERSON" in upper
    )


def is_prominent_people_filter_failure(
    exc: Exception | None = None,
    msg: str = "",
    api_trace: list[dict] | None = None,
) -> bool:
    if _payload_has_prominent_people_filter(msg):
        return True
    if exc is not None and _payload_has_prominent_people_filter(str(exc)):
        return True
    if isinstance(exc, FlowApiError):
        for attempt in exc.attempts or []:
            if _payload_has_prominent_people_filter(attempt):
                return True
            response = attempt.get("response") if isinstance(attempt, dict) else None
            if isinstance(response, dict):
                data = response.get("data")
                if _payload_has_prominent_people_filter(data):
                    return True
    for entry in api_trace or []:
        label = str(entry.get("label") or "")
        if label and "poll" not in label.lower() and label != "video_poll_media":
            continue
        if _payload_has_prominent_people_filter(entry.get("data")):
            return True
        if _payload_has_prominent_people_filter(entry):
            return True
    return False


def _flow_response_is_not_found(raw: Any) -> bool:
    if not isinstance(raw, dict):
        return False
    if get_media_http_status(raw) == 404:
        return True
    return _get_media_error_status(raw) == "NOT_FOUND"


def veo_upsample_recreate_source_public_error(
    msg: str = "",
    exc: Exception | None = None,
    *,
    request_type: str = "",
) -> str | None:
    """Map Veo 1080p upsample NOT_FOUND to a short English message for API callers."""
    if str(msg or "").strip() == VEO_UPSAMPLE_RECREATE_SOURCE_ERROR:
        return VEO_UPSAMPLE_RECREATE_SOURCE_ERROR

    upsample_ctx = str(request_type or "").strip().lower() == "upsample_video"
    if isinstance(exc, FlowApiError) and exc.step == "video_upsample_submit":
        upsample_ctx = True
    if not upsample_ctx:
        return None

    message = str(msg or "").strip() or (str(exc) if exc else "")
    not_found = is_get_media_404_message(message)
    if not not_found and isinstance(exc, FlowApiError):
        not_found = _flow_response_is_not_found(exc.raw)
    if not_found:
        return VEO_UPSAMPLE_RECREATE_SOURCE_ERROR
    return None


def sanitize_public_error(
    msg: str,
    exc: Exception | None = None,
    *,
    request_type: str = "",
) -> str:
    """Map raw Google Flow codes to short user-facing labels."""
    mapped = veo_upsample_recreate_source_public_error(
        msg,
        exc,
        request_type=request_type,
    )
    if mapped:
        return mapped
    if _payload_has_prominent_people_filter(msg):
        return "content_filter"
    return str(msg or "").strip()


def _rpc_error_invalid_argument(payload: dict) -> bool:
    err = payload.get("error")
    if not isinstance(err, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            err = data.get("error")
    if not isinstance(err, dict):
        return False
    if str(err.get("status") or "").upper() == "INVALID_ARGUMENT":
        return True
    for detail in err.get("details") or []:
        if isinstance(detail, dict) and str(detail.get("reason") or "").upper() == "PUBLIC_ERROR_MINOR":
            return True
    return False


def _payload_has_invalid_argument_retry(payload: Any) -> bool:
    if payload is None:
        return False
    if isinstance(payload, dict):
        if _rpc_error_invalid_argument(payload):
            return True
        data = payload.get("data")
        if isinstance(data, dict) and data is not payload and _rpc_error_invalid_argument(data):
            return True
        text = json.dumps(payload, ensure_ascii=False)
    else:
        text = str(payload)
    return "PUBLIC_ERROR_MINOR" in text.upper()


def is_invalid_argument_retry_failure(
    exc: Exception | None = None,
    msg: str = "",
    api_trace: list[dict] | None = None,
) -> bool:
    if _payload_has_invalid_argument_retry(msg):
        return True
    if exc is not None:
        if _payload_has_invalid_argument_retry(str(exc)):
            return True
        if isinstance(exc, FlowApiError):
            if _payload_has_invalid_argument_retry(exc.raw):
                return True
            for attempt in exc.attempts or []:
                if _payload_has_invalid_argument_retry(attempt):
                    return True
                response = attempt.get("response") if isinstance(attempt, dict) else None
                if isinstance(response, dict):
                    if _payload_has_invalid_argument_retry(response):
                        return True
                    if _payload_has_invalid_argument_retry(response.get("data")):
                        return True
    for entry in api_trace or []:
        if _payload_has_invalid_argument_retry(entry.get("data")):
            return True
        if _payload_has_invalid_argument_retry(entry):
            return True
    return False


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


async def try_fetch_media_video_url_with_retry(
    client: FlowClient,
    media_id: str,
    *,
    max_attempts: int = 8,
    retry_404: bool = True,
) -> str | None:
    """GET /v1/media/{id} with backoff — poll SUCCESSFUL often precedes media API readiness."""
    last_404: GetMedia404Error | None = None
    for attempt in range(max(1, max_attempts)):
        try:
            return await try_fetch_media_video_url(client, media_id)
        except GetMedia404Error as exc:
            last_404 = exc
            if not retry_404 or attempt >= max_attempts - 1:
                raise
            await asyncio.sleep(min(12.0, POLL_INTERVAL_S * (attempt + 2)))
        except (asyncio.TimeoutError, TimeoutError) as exc:
            if attempt >= max_attempts - 1:
                raise FlowApiError("extension_timeout_get_media", step="get_media") from exc
            await asyncio.sleep(POLL_INTERVAL_S)
    if last_404:
        raise last_404
    return None


async def _resolve_all_media_urls(
    client: FlowClient,
    completed: dict[str, str],
    pending: list[str],
) -> bool:
    """Upgrade /media/{id} placeholders to real URLs or saved local MP4."""
    ok = True
    for mid in pending:
        url = completed.get(mid, "")
        if _media_url_is_resolved(url, mid):
            if str(url).startswith("/media/") or not url:
                completed[mid] = f"/media/{mid}"
            continue
        try:
            fetched = await try_fetch_media_video_url_with_retry(
                client,
                mid,
                max_attempts=1,
                retry_404=False,
            )
        except FlowApiError as exc:
            if is_get_media_not_ready_error(exc):
                ok = False
                continue
            raise
        except GetMedia404Error:
            ok = False
            continue
        if fetched:
            completed[mid] = fetched
        else:
            ok = False
    return ok


async def try_fetch_media_video_url(client: FlowClient, media_id: str) -> str | None:
    """GET /v1/media/{id} — Lower Priority / workflow models return MP4 inline."""
    resp = await client.get_media(media_id)
    status = get_media_http_status(resp)
    if status == 500:
        return None
    if status == 404:
        raise GetMedia404Error(_get_media_http_error(resp, status), raw=resp)
    if status == 400 and _get_media_error_status(resp) == "INVALID_ARGUMENT":
        # Video still rendering — poll will retry; do not abort the task.
        return None
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
    requeue_on_get_media_404: bool = False,
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

        if interleave_get_media:
            for mid in list(remaining):
                if _media_url_is_resolved(completed.get(mid, ""), mid):
                    continue
                poll_status = ""
                for entry in poll.get("media") or []:
                    if isinstance(entry, dict) and str(
                        entry.get("name") or entry.get("mediaId") or ""
                    ) == mid:
                        poll_status = _extract_poll_status(entry)
                        break
                if (
                    not requeue_on_get_media_404
                    and not _poll_entry_done(poll_status, False)
                    and mid not in completed
                ):
                    continue
                try:
                    url = await try_fetch_media_video_url_with_retry(
                        client, mid, max_attempts=1, retry_404=False
                    )
                except GetMedia404Error:
                    if (
                        requeue_on_get_media_404
                        and not _poll_entry_done(poll_status, False)
                        and mid not in completed
                    ):
                        continue
                    raise
                except FlowApiError as exc:
                    if (
                        requeue_on_get_media_404
                        and is_get_media_not_ready_error(exc)
                        and not _poll_entry_done(poll_status, False)
                        and mid not in completed
                    ):
                        continue
                    raise
                if url:
                    completed[mid] = url

        if on_round:
            try:
                on_round(round_idx + 1, poll, dict(completed))
            except Exception:
                pass

        if any_failed and not completed:
            raise FlowApiError(
                extract_media_poll_failure_error(poll) or "video_generation_failed",
                step="video_poll_media",
                attempts=poll_snapshots,
            )
        if all_done or (completed and len(completed) >= len(pending)):
            if await _resolve_all_media_urls(client, completed, pending):
                return list(completed.values()), list(completed.keys())

        if round_idx % 10 == 9:
            logger.info("media poll round %s/%s done=%s", round_idx + 1, max_rounds, len(completed))
        await asyncio.sleep(POLL_INTERVAL_S)

    if completed and await _resolve_all_media_urls(client, completed, pending):
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
        op_inner = op.get("operation") or op
        video_meta = (op_inner.get("metadata") or {}).get("video") or {}
        mid = video_meta.get("mediaId") or video_meta.get("name") or op_inner.get("name")
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
        upsample_ids = _upsample_poll_media_names(submit_raw)
        if upsample_ids:
            ids.extend(upsample_ids)
        project_id = submit_raw.get("projectId")
        for entry in submit_raw.get("media") or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or entry.get("mediaId") or "").strip()
            if not name:
                continue
            if name in ids:
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
    project_id: str | None = None,
    should_abort=None,
    on_round=None,
) -> tuple[list[str], list[str]]:
    """Poll Lower Priority / workflow models (batch status + get_media, requeue on 404)."""
    media_ids = list(
        dict.fromkeys(
            str(op["_primary_media_id"])
            for op in operations
            if op.get("_primary_media_id")
        )
    )
    if not project_id:
        for op in operations:
            pid = op.get("_project_id")
            if pid:
                project_id = str(pid)
                break

    if project_id and media_ids:
        return await poll_video_by_media(
            client,
            project_id,
            media_ids,
            max_rounds,
            should_abort=should_abort,
            on_round=on_round,
            requeue_on_get_media_404=True,
        )

    pending = {mid for mid in media_ids if mid}
    completed: dict[str, str] = {}

    for round_idx in range(max_rounds):
        if should_abort:
            should_abort()
        for mid in list(pending):
            if _media_url_is_resolved(completed.get(mid, ""), mid):
                continue
            url = await try_fetch_media_video_url_with_retry(
                client, mid, max_attempts=1, retry_404=False
            )
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
    if isinstance(err, FlowApiError) and err.args and str(err.args[0]).strip():
        return str(err.args[0]).strip()
    if isinstance(err, (asyncio.TimeoutError, TimeoutError)):
        return "extension_timeout"
    if isinstance(err, ConnectionError):
        return "extension_disconnected"
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
    if text.strip():
        return text
    name = type(err).__name__ if err is not None else "unknown_error"
    if name in ("TimeoutError", "CancelledError"):
        return "extension_timeout"
    return name or "unknown_error"
