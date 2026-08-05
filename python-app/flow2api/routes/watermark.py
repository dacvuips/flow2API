"""HTTP API for watermark clean (base64 in → base64 / public URL out)."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from flow2api.services.auth_keys import get_api_key_by_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/watermark", tags=["watermark"])


class CleanWatermarkBody(BaseModel):
    """Upload one image or video (base64) and receive cleaned media.

    Prefer ``image_base64`` / ``video_base64`` (same style as gen_image / video).
    Also accepts generic ``media_base64`` + optional ``kind``.
    """

    image_base64: Optional[str] = Field(
        default=None,
        description="Ảnh base64 (pure hoặc data:image/...;base64,...).",
    )
    video_base64: Optional[str] = Field(
        default=None,
        description="Video base64 (pure hoặc data:video/mp4;base64,...).",
    )
    media_base64: Optional[str] = Field(
        default=None,
        description="Media generic — dùng với kind=image|video|auto.",
    )
    kind: Optional[str] = Field(
        default=None,
        description="image | video | auto (mặc định auto theo magic bytes).",
    )
    return_mode: str = Field(
        default="both",
        description="base64 | url | both — media_base64 và/hoặc url công khai /image|/video.",
    )


class CleanBatchBody(BaseModel):
    """Clean multiple images (same Erasio engine). Max 8 images per call."""

    image_base64s: list[str] = Field(default_factory=list)
    return_mode: str = "both"


def _bearer(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing_bearer_token")
    return authorization.split(" ", 1)[1].strip()


def _auth_key_id(token: str = Depends(_bearer)) -> int:
    key_id = get_api_key_by_token(token)
    if not key_id:
        raise HTTPException(401, "invalid_api_key")
    return key_id


@router.post("/clean")
async def clean_watermark(
    body: CleanWatermarkBody,
    api_key_id: int = Depends(_auth_key_id),
) -> dict[str, Any]:
    """
    Xóa watermark Gemini/Flow khỏi ảnh hoặc video do client upload (base64).

    - Ảnh: Erasio reverse-blend (all-tool)
    - Video: OpenMark inpaint/crop (config FLOW2API_WATERMARK_VIDEO_MODE)

    Đồng bộ: trả kết quả trong cùng response (không queue worker).
    """
    del api_key_id  # auth only
    from flow2api.services.watermark_api import clean_media_payload

    try:
        return await asyncio.to_thread(
            clean_media_payload,
            media_base64=body.media_base64,
            image_base64=body.image_base64,
            video_base64=body.video_base64,
            kind=body.kind,
            return_mode=body.return_mode,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        logger.exception("watermark clean failed")
        raise HTTPException(500, f"watermark_clean_failed: {exc}") from exc


@router.post("/clean-batch")
async def clean_watermark_batch(
    body: CleanBatchBody,
    api_key_id: int = Depends(_auth_key_id),
) -> dict[str, Any]:
    """Xóa watermark nhiều ảnh (mỗi item giống /clean). Tối đa 8 ảnh/lần."""
    del api_key_id
    from flow2api.services.watermark_api import clean_media_payload

    items = list(body.image_base64s or [])
    if not items:
        raise HTTPException(400, "image_base64s is required")
    if len(items) > 8:
        raise HTTPException(400, "max 8 images per batch")

    results: list[dict[str, Any]] = []
    for idx, b64 in enumerate(items):
        try:
            one = await asyncio.to_thread(
                clean_media_payload,
                image_base64=b64,
                kind="image",
                return_mode=body.return_mode,
            )
            one["index"] = idx
            results.append(one)
        except Exception as exc:
            results.append(
                {
                    "index": idx,
                    "success": False,
                    "cleaned": False,
                    "message": str(exc),
                }
            )
    ok = sum(1 for r in results if r.get("success"))
    return {
        "success": ok == len(results),
        "count": len(results),
        "cleaned_count": sum(1 for r in results if r.get("cleaned")),
        "results": results,
    }
