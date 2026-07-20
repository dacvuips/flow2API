"""Chat GPT tab — OpenAI official API + chatgpt.com web conversation via extension."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Any, AsyncIterator, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from flow2api.services import system_ops
from flow2api.services.api_auth import auth_key_id
from flow2api.services.chatgpt_broker import get_chatgpt_broker
from flow2api.services.chatgpt_media import (
    persist_chatgpt_request_images,
    persist_chatgpt_result_media,
    resolve_chatgpt_media_path,
    sanitize_chatgpt_result_for_poll,
)
from flow2api.services.chatgpt_playwright import playwright_status, run_playwright_chat
from flow2api.services.chatgpt_pool import (
    PLAYWRIGHT_PROFILE_ID,
    ensure_scheduler_started,
    is_playwright_slot_id,
    list_chatgpt_profiles,
    nudge_scheduler,
    queue_summary,
    register_job_runner,
)
from flow2api.services.chatgpt_pool_settings import (
    add_playwright_slot,
    get_chatgpt_pool_settings,
    list_playwright_slots,
    remove_playwright_slot,
    save_chatgpt_pool_settings,
    set_chatgpt_dispatch_enabled,
    update_playwright_slot,
)
from flow2api.services.extension_pool import ExtensionSession, get_extension_pool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chatgpt", tags=["chatgpt"])

DEFAULT_WEB_ENDPOINT = "/backend-api/f/conversation"
DEFAULT_WEB_MODEL = "gpt-5-5"


class OpenAISettingsBody(BaseModel):
    api_key: str | None = None
    model: str | None = None
    base_url: str | None = None


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatBody(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1)
    model: str | None = None
    stream: bool = True
    temperature: float | None = Field(default=None, ge=0, le=2)


class WebChatBody(BaseModel):
    prompt: str = ""
    endpoint: str | None = None
    model: str | None = None
    profile_id: str | None = None
    conversation_id: str | None = None
    parent_message_id: str | None = None
    images: list[dict[str, Any]] | None = None
    mode: str | None = None
    system_hints: list[str] | None = None
    picture: bool | None = None
    tab_id: str | None = None  # client browser-tab id — poll gắn theo tab


class PublicChatImage(BaseModel):
    """Image for public ChatGPT API — data URL or raw base64."""

    data: str = Field(min_length=1)
    file_name: str | None = None
    fileName: str | None = None
    mime_type: str | None = None
    mimeType: str | None = None


class PublicChatBody(BaseModel):
    """Public external ChatGPT API body (sync)."""

    prompt: str = ""
    model: str | None = None
    conversation_id: str | None = None
    parent_message_id: str | None = None
    profile_id: str | None = None
    endpoint: str | None = None
    images: list[PublicChatImage] | None = None
    mode: str | None = None
    system_hints: list[str] | None = None
    picture: bool | None = None


def _normalize_system_hints(
    *,
    mode: str | None = None,
    system_hints: list[str] | None = None,
    picture: bool | None = None,
) -> list[str]:
    hints: list[str] = []
    mode_l = str(mode or "").strip().lower()
    if mode_l in ("picture_v2", "picture") or picture is True:
        hints.append("picture_v2")
    for h in system_hints or []:
        s = str(h or "").strip()
        if s and s not in hints:
            hints.append(s)
    return hints


def _normalize_images(images: list[Any] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for img in images or []:
        if isinstance(img, PublicChatImage):
            data = (img.data or "").strip()
            if not data:
                continue
            out.append(
                {
                    "data": data,
                    "fileName": (img.file_name or img.fileName or "upload.jpg").strip() or "upload.jpg",
                    "mimeType": (img.mime_type or img.mimeType or "").strip() or None,
                }
            )
            continue
        if not isinstance(img, dict):
            continue
        data = str(img.get("data") or img.get("base64") or "").strip()
        if not data:
            continue
        out.append(
            {
                "data": data,
                "fileName": str(img.get("fileName") or img.get("file_name") or img.get("name") or "upload.jpg"),
                "mimeType": str(img.get("mimeType") or img.get("mime_type") or "") or None,
            }
        )
    return out


def _try_pick_ws_session(profile_id: str | None = None) -> ExtensionSession | None:
    pool = get_extension_pool()
    pool.hydrate_db_profiles()
    pid = (profile_id or "").strip()
    if pid:
        session = pool.get(pid)
        if session and session.connected:
            return session
        return None
    for session in pool.list_sessions():
        if session.connected:
            return session
    return None


_chatgpt_delay_lock = asyncio.Lock()
_chatgpt_last_start_at = 0.0


async def _await_chatgpt_call_delay() -> float:
    """Sleep a random duration in [min, max] between consecutive ChatGPT calls."""
    global _chatgpt_last_start_at
    cfg = system_ops.chatgpt_config()
    dmin = float(cfg.get("call_delay_min_s") if cfg.get("call_delay_min_s") is not None else cfg.get("call_delay_s") or 0)
    dmax = float(cfg.get("call_delay_max_s") if cfg.get("call_delay_max_s") is not None else cfg.get("call_delay_s") or 0)
    dmin = max(0.0, min(600.0, dmin))
    dmax = max(0.0, min(600.0, dmax))
    if dmax < dmin:
        dmin, dmax = dmax, dmin
    delay = random.uniform(dmin, dmax) if dmax > 0 else 0.0
    if delay <= 0:
        async with _chatgpt_delay_lock:
            _chatgpt_last_start_at = time.time()
        return 0.0

    async with _chatgpt_delay_lock:
        now = time.time()
        wait = delay - (now - _chatgpt_last_start_at)
        if wait > 0:
            logger.info(
                "chatgpt call delay random=%.1fs (range %.1f–%.1f) waiting=%.1fs",
                delay,
                dmin,
                dmax,
                wait,
            )
            await asyncio.sleep(wait)
        _chatgpt_last_start_at = time.time()
        return max(0.0, wait)


def _format_asset_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "file_id": item.get("file_id") or item.get("fileId"),
        "file_name": item.get("file_name") or item.get("fileName"),
        "mime_type": item.get("mime_type") or item.get("mimeType"),
        "kind": item.get("kind") or "file",
        "width": item.get("width") or 0,
        "height": item.get("height") or 0,
        "size_bytes": item.get("size_bytes") or item.get("sizeBytes") or item.get("file_size") or 0,
        "asset_pointer": item.get("asset_pointer") or item.get("assetPointer"),
        "sandbox_path": item.get("sandbox_path") or item.get("sandboxPath"),
        "source": item.get("source"),
        "download_url": item.get("download_url") or item.get("downloadUrl"),
        "data": item.get("data"),
        "error": item.get("error"),
    }


def _format_uploaded_image(img: dict[str, Any]) -> dict[str, Any]:
    return {
        "file_id": img.get("fileId") or img.get("file_id"),
        "file_name": img.get("fileName") or img.get("file_name"),
        "mime_type": img.get("mimeType") or img.get("mime_type"),
        "file_size": img.get("fileSize") or img.get("file_size"),
        "width": img.get("width"),
        "height": img.get("height"),
        "library_file_id": img.get("libraryFileId") or img.get("library_file_id"),
        "download_url": img.get("downloadUrl") or img.get("download_url"),
    }


def _format_web_result(
    result: dict[str, Any],
    *,
    endpoint: str,
    model: str,
    via: str,
    profile_id: str | None = None,
    profile_label: str | None = None,
) -> dict[str, Any]:
    if not result.get("ok"):
        detail = result.get("error") or "chatgpt_send_failed"
        raise HTTPException(502, str(detail))

    uploaded = result.get("uploadedImages") or result.get("uploaded_images")
    if not isinstance(uploaded, list):
        uploaded = []

    images = result.get("images") if isinstance(result.get("images"), list) else []
    files = result.get("files") if isinstance(result.get("files"), list) else []

    # Old extension returned upload metadata under "images" (fileId/fileName, no kind/url).
    if not uploaded and images:
        sample = images[0] if isinstance(images[0], dict) else {}
        if (
            (sample.get("fileId") or sample.get("file_id"))
            and not sample.get("kind")
            and not sample.get("download_url")
            and not sample.get("data")
        ):
            uploaded = images
            images = []

    return {
        "ok": True,
        "mode": "web",
        "via": via,
        "text": result.get("text") or "",
        "endpoint": result.get("endpoint") or endpoint,
        "model": result.get("model") or model,
        "conversation_id": result.get("conversationId") or result.get("conversation_id"),
        "message_id": result.get("messageId") or result.get("message_id"),
        "profile_id": profile_id,
        "profile_label": profile_label,
        "uploaded_images": [
            _format_uploaded_image(img)
            for img in uploaded
            if isinstance(img, dict)
        ],
        "images": [
            _format_asset_item(img)
            for img in images
            if isinstance(img, dict)
        ],
        "files": [
            _format_asset_item(f)
            for f in files
            if isinstance(f, dict)
        ],
        "requirements_error": result.get("requirementsError") or result.get("requirements_error"),
    }


async def run_web_chat(
    *,
    prompt: str = "",
    model: str | None = None,
    endpoint: str | None = None,
    profile_id: str | None = None,
    conversation_id: str | None = None,
    parent_message_id: str | None = None,
    images: list[Any] | None = None,
    mode: str | None = None,
    system_hints: list[str] | None = None,
    picture: bool | None = None,
    slot_id: str | None = None,
    _force_playwright: bool = False,
    _force_extension: bool = False,
) -> dict[str, Any]:
    prompt = (prompt or "").strip()
    norm_images = _normalize_images(images)
    if not prompt and not norm_images:
        raise HTTPException(400, "empty_prompt")

    endpoint = (endpoint or "").strip() or DEFAULT_WEB_ENDPOINT
    model = (model or "").strip() or DEFAULT_WEB_MODEL
    hints = _normalize_system_hints(mode=mode, system_hints=system_hints, picture=picture)
    params: dict[str, Any] = {
        "prompt": prompt,
        "endpoint": endpoint,
        "model": model,
    }
    if norm_images:
        params["images"] = norm_images
    if conversation_id:
        params["conversationId"] = conversation_id
    if parent_message_id:
        params["parentMessageId"] = parent_message_id
    if hints:
        params["systemHints"] = hints
        params["mode"] = "picture_v2" if "picture_v2" in hints else (mode or "")

    cgpt_cfg = system_ops.chatgpt_config()
    transport = str(cgpt_cfg.get("transport") or "playwright").strip().lower()
    pid = (profile_id or "").strip() or None
    sid = (slot_id or "").strip() or None
    if not sid and is_playwright_slot_id(pid):
        sid = pid if pid != PLAYWRIGHT_PROFILE_ID else None

    # Scheduler / explicit pin overrides global transport
    use_playwright = bool(_force_playwright) or (
        not _force_extension
        and (is_playwright_slot_id(pid) or transport == "playwright")
    )
    if is_playwright_slot_id(pid):
        use_playwright = True
    if _force_extension and pid and not is_playwright_slot_id(pid):
        use_playwright = False

    waited = await _await_chatgpt_call_delay()
    if waited > 0:
        logger.info("chatgpt delay waited %.1fs", waited)

    send_timeout = 300.0 if "picture_v2" in hints else 180.0
    broker_timeout = 360.0 if "picture_v2" in hints else 240.0

    if use_playwright:
        result = await run_playwright_chat(
            prompt=prompt,
            images=norm_images,
            picture_mode="picture_v2" in hints,
            timeout_s=send_timeout,
            slot_id=sid or pid,
        )
        used_slot = result.get("slot_id") or sid or pid or PLAYWRIGHT_PROFILE_ID
        out = _format_web_result(
            result,
            endpoint=endpoint,
            model=model,
            via="playwright",
            profile_id=str(used_slot),
            profile_label=str(used_slot),
        )
        out["call_delay_s"] = cgpt_cfg.get("call_delay_s")
        out["page_url"] = result.get("page_url")
        out["slot_id"] = used_slot
        return out

    session = _try_pick_ws_session(pid)
    if session:
        result = await session.chatgpt_send(params, timeout=send_timeout)
        out = _format_web_result(
            result,
            endpoint=endpoint,
            model=model,
            via="ws",
            profile_id=session.profile_id,
            profile_label=session.display_name(),
        )
        out["call_delay_s"] = cgpt_cfg.get("call_delay_s")
        return out

    broker = get_chatgpt_broker()
    if not broker.online_workers():
        for _ in range(16):
            await asyncio.sleep(0.5)
            if broker.online_workers():
                break
    if not broker.online_workers():
        raise HTTPException(
            503,
            "extension_not_ready — Không có Bridge WS và chưa thấy extension poll HTTP. "
            "Reload extension trên Chrome (profile đã login chatgpt.com), đợi 2–3 giây rồi gọi lại. "
            "Hoặc Settings → Chat GPT → transport = playwright. "
            "Agent phải chạy ở http://127.0.0.1:1994.",
        )

    if pid:
        params["preferredWorkerId"] = pid
    result = await broker.submit(params, timeout=broker_timeout)
    out = _format_web_result(
        result,
        endpoint=endpoint,
        model=model,
        via="http",
        profile_id=pid,
    )
    out["call_delay_s"] = cgpt_cfg.get("call_delay_s")
    return out


async def _run_public_chat_job(job_id: str, kwargs: dict[str, Any]) -> None:
    broker = get_chatgpt_broker()
    if broker.get_public_job(job_id) and broker.get_public_job(job_id).status == "queued":
        broker.mark_public_running(job_id)
    try:
        clean = {
            k: v
            for k, v in kwargs.items()
            if k in (
                "prompt",
                "model",
                "endpoint",
                "profile_id",
                "conversation_id",
                "parent_message_id",
                "images",
                "mode",
                "system_hints",
                "picture",
                "slot_id",
                "_force_playwright",
                "_force_extension",
            )
        }
        result = await run_web_chat(**clean)
        result = persist_chatgpt_result_media(job_id, result)
        broker.finish_public_job(job_id, result=result)
    except HTTPException as exc:
        detail = exc.detail
        if not isinstance(detail, str):
            detail = str(detail)
        broker.finish_public_job(job_id, error=detail)
    except asyncio.CancelledError:
        broker.finish_public_job(job_id, error="cancelled")
        raise
    except Exception as exc:
        logger.exception("chatgpt public job %s failed", job_id[:8])
        broker.finish_public_job(job_id, error=str(exc) or "chatgpt_job_failed")


# Wire scheduler → job runner (avoid circular import at module import time)
register_job_runner(_run_public_chat_job)

def enqueue_web_chat(
    *,
    prompt: str = "",
    model: str | None = None,
    endpoint: str | None = None,
    profile_id: str | None = None,
    conversation_id: str | None = None,
    parent_message_id: str | None = None,
    images: list[Any] | None = None,
    mode: str | None = None,
    system_hints: list[str] | None = None,
    picture: bool | None = None,
    tab_id: str | None = None,
) -> dict[str, Any]:
    """Enqueue chat job and return immediately (Cloudflare-safe). Scheduler starts work."""
    prompt = (prompt or "").strip()
    norm_images = _normalize_images(images)
    if not prompt and not norm_images:
        raise HTTPException(400, "empty_prompt")

    hints = _normalize_system_hints(mode=mode, system_hints=system_hints, picture=picture)
    pid = (profile_id or "").strip() or None
    tid = str(tab_id or "").strip()[:64] or None
    kwargs = {
        "prompt": prompt,
        "model": model,
        "endpoint": endpoint,
        "profile_id": pid,
        "conversation_id": conversation_id,
        "parent_message_id": parent_message_id,
        "images": images,
        "mode": mode,
        "system_hints": hints or system_hints,
        "picture": picture,
    }
    broker = get_chatgpt_broker()
    job = broker.create_public_job(
        {
            "prompt": prompt,
            "model": (model or "").strip() or DEFAULT_WEB_MODEL,
            "endpoint": (endpoint or "").strip() or DEFAULT_WEB_ENDPOINT,
            "profile_id": pid,
            "profile_assigned_by_user": bool(pid),
            "conversation_id": conversation_id,
            "parent_message_id": parent_message_id,
            "images": norm_images,
            "mode": "picture_v2" if "picture_v2" in hints else mode,
            "system_hints": hints,
            "tab_id": tid,
        },
        kwargs=kwargs,
    )
    # Persist request images for dashboard list/detail (prompt + input thumbs)
    if norm_images:
        previews = persist_chatgpt_request_images(job.job_id, norm_images)
        urls = [
            str(p.get("url") or p.get("download_url") or "")
            for p in previews
            if isinstance(p, dict) and (p.get("url") or p.get("download_url"))
        ]
        broker.update_public_params(
            job.job_id,
            {
                "prompt": prompt,
                "prompt_preview": prompt[:200],
                "image_count": len(norm_images),
                "input_preview_urls": urls[:12],
            },
        )
    ensure_scheduler_started()
    nudge_scheduler()
    logger.info(
        "chatgpt public job queued %s profile=%s tab=%s",
        job.job_id[:8],
        pid or "auto",
        (tid or "-")[:12],
    )
    return {
        "ok": True,
        "id": job.job_id,
        "status": "queued",
        "poll_url": f"/api/v1/chatgpt/chat/{job.job_id}",
        "profile_id": pid,
        "tab_id": tid,
        "queue": queue_summary(),
    }


def public_job_payload(job_id: str) -> dict[str, Any]:
    broker = get_chatgpt_broker()
    job = broker.get_public_job(job_id)
    if not job:
        raise HTTPException(404, "chatgpt_job_not_found")
    payload = job.to_dict(include_result=True)
    if isinstance(payload.get("result"), dict):
        payload["result"] = sanitize_chatgpt_result_for_poll(payload["result"])
    payload["ok"] = job.status != "failed"
    if job.status == "done" and isinstance(job.result, dict):
        # Flatten common fields for convenient poll clients
        safe_result = sanitize_chatgpt_result_for_poll(job.result) or {}
        for key in (
            "text",
            "images",
            "files",
            "conversation_id",
            "message_id",
            "model",
            "endpoint",
            "via",
            "uploaded_images",
        ):
            if key in safe_result and key not in payload:
                payload[key] = safe_result[key]

    # Request payload for dashboard detail (full prompt + input images)
    params = job.params_summary or {}
    kwargs = broker.get_public_kwargs(job_id) or {}
    prompt = str(kwargs.get("prompt") or params.get("prompt") or params.get("prompt_preview") or "")
    request_images: list[dict[str, Any]] = []
    for url in params.get("input_preview_urls") or []:
        u = str(url or "").strip()
        if u:
            request_images.append({"url": u, "download_url": u, "kind": "request"})
    if not request_images:
        for img in kwargs.get("images") or []:
            if isinstance(img, dict):
                src = str(img.get("data") or img.get("url") or img.get("download_url") or "").strip()
                if not src:
                    continue
                entry: dict[str, Any] = {"kind": "request"}
                if src.startswith("data:"):
                    entry["data"] = src
                else:
                    entry["url"] = src
                    entry["download_url"] = src
                if img.get("fileName") or img.get("file_name"):
                    entry["file_name"] = img.get("fileName") or img.get("file_name")
                request_images.append(entry)
            elif isinstance(img, str) and img.strip():
                src = img.strip()
                entry = {"kind": "request"}
                if src.startswith("data:"):
                    entry["data"] = src
                else:
                    entry["url"] = src
                    entry["download_url"] = src
                request_images.append(entry)
    payload["request"] = {
        "prompt": prompt,
        "images": request_images[:12],
        "image_count": int(params.get("image_count") or len(request_images) or 0),
    }
    # Ensure list-style params also expose full prompt for UI
    if isinstance(payload.get("params"), dict):
        payload["params"] = {
            **payload["params"],
            "prompt": prompt or payload["params"].get("prompt") or "",
            "prompt_preview": (prompt or payload["params"].get("prompt_preview") or "")[:200],
        }
    return payload


async def web_status_payload(profile_id: str | None = None) -> dict[str, Any]:
    pool = get_extension_pool()
    pool.hydrate_db_profiles()
    broker = get_chatgpt_broker()
    broker_stats = broker.stats()
    http_workers = broker.online_workers()
    profiles = await list_chatgpt_profiles()
    online = [p for p in profiles if p.get("online")]
    accepting = [p for p in profiles if p.get("accepts_new_jobs")]
    session = _try_pick_ws_session(profile_id)
    cgpt_cfg = system_ops.chatgpt_config()
    transport_setting = str(cgpt_cfg.get("transport") or "playwright").strip().lower()
    pw_st = await playwright_status()
    pool_settings = get_chatgpt_pool_settings()
    ensure_scheduler_started()

    base = {
        "ok": True,
        "mode": "web",
        "profiles_online": len(online),
        "profiles_total": len(profiles),
        "profiles_accepting": len(accepting),
        "profiles_offline_gen": pool.offline_gen_count(),
        "profiles": profiles,
        "http_workers_online": len(http_workers),
        "http_workers": http_workers,
        "broker": broker_stats,
        "queue": queue_summary(),
        "pool": pool_settings.to_dict(),
        "default_endpoint": DEFAULT_WEB_ENDPOINT,
        "default_model": DEFAULT_WEB_MODEL,
        "transport_setting": transport_setting,
        "chatgpt": cgpt_cfg,
        "playwright": pw_st,
        "transport": "ws" if session else ("http" if http_workers else "none"),
    }

    if transport_setting == "playwright":
        return {
            **base,
            "transport": "playwright",
            "extension_connected": bool(pw_st.get("playwright_installed")),
            "loggedIn": None,
            "ok": bool(pw_st.get("playwright_installed")),
            "error": None
            if pw_st.get("playwright_installed")
            else (pw_st.get("hint") or "playwright_not_installed"),
            "hint": pw_st.get("hint")
            or "Playwright sẽ mở chatgpt.com (/ hoặc /images), gõ prompt, bấm nút gửi, bắt Network conversation.",
            "profile_label": cgpt_cfg.get("chrome_profile") or "Default",
        }

    if session:
        data = await session.chatgpt_session_status(timeout=20.0)
        return {
            **base,
            "extension_connected": True,
            "loggedIn": bool(data.get("loggedIn")),
            "profile_id": session.profile_id,
            "profile_label": session.display_name(),
            "email": data.get("email"),
            "name": data.get("name"),
            "deviceId": data.get("deviceId"),
            "cookieCount": data.get("cookieCount"),
            "present": data.get("present"),
            "error": data.get("error"),
        }

    if http_workers:
        return {
            **base,
            "extension_connected": True,
            "loggedIn": None,
            "error": None,
            "hint": "Extension đang poll HTTP (không cần Bridge WS). Gửi chat sẽ qua hàng đợi pool.",
        }

    # Still OK if playwright slot is accepting
    if any(p.get("kind") == "playwright" and p.get("accepts_new_jobs") for p in profiles):
        return {
            **base,
            "transport": "playwright",
            "extension_connected": True,
            "loggedIn": None,
            "ok": True,
            "error": None,
            "hint": "Playwright slot sẵn sàng nhận job.",
        }

    return {
        **base,
        "ok": False,
        "extension_connected": False,
        "loggedIn": False,
        "error": (
            "Chưa có profile ChatGPT sẵn sàng (WS/HTTP/Playwright). "
            "Bật Nhận job trên profile, Mở Chrome CDP, hoặc reload extension."
        ),
    }


@router.get("/status")
async def chatgpt_status(_: int = Depends(auth_key_id)):
    return {"ok": True, **system_ops.public_openai_config()}


@router.get("/web/status")
async def chatgpt_web_status(
    profile_id: str | None = None,
    _: int = Depends(auth_key_id),
):
    return await web_status_payload(profile_id)


class ProfileDispatchBody(BaseModel):
    enabled: bool


class PoolSettingsBody(BaseModel):
    max_concurrent: int | None = None
    profile_default_max_concurrent: int | None = None


class AssignProfileBody(BaseModel):
    profile_id: str | None = None


@router.get("/web/jobs")
async def chatgpt_web_jobs(
    status: str | None = Query(None, description="queued|running|done|failed|cancelled|active|all"),
    limit: int = Query(50, ge=1, le=200),
    _: int = Depends(auth_key_id),
):
    broker = get_chatgpt_broker()
    ensure_scheduler_started()
    return {
        "ok": True,
        "items": broker.list_public_jobs(status=status, limit=limit),
        "queue": queue_summary(),
        "pool": get_chatgpt_pool_settings().to_dict(),
    }


@router.put("/web/chat/{job_id}/profile")
async def chatgpt_assign_job_profile(
    job_id: str,
    body: AssignProfileBody,
    _: int = Depends(auth_key_id),
):
    broker = get_chatgpt_broker()
    try:
        job = broker.set_public_profile(job_id, body.profile_id)
    except KeyError:
        raise HTTPException(404, "chatgpt_job_not_found") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    nudge_scheduler()
    return {"ok": True, **job.to_dict(include_result=False)}


@router.post("/web/chat/{job_id}/cancel")
async def chatgpt_cancel_job(job_id: str, _: int = Depends(auth_key_id)):
    broker = get_chatgpt_broker()
    try:
        job = broker.cancel_public_job(job_id)
    except KeyError:
        raise HTTPException(404, "chatgpt_job_not_found") from None
    nudge_scheduler()
    return {"ok": True, **job.to_dict(include_result=False)}


@router.put("/profiles/{profile_id}/dispatch")
async def chatgpt_profile_dispatch(
    profile_id: str,
    body: ProfileDispatchBody,
    _: int = Depends(auth_key_id),
):
    try:
        settings = set_chatgpt_dispatch_enabled(profile_id, bool(body.enabled))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    nudge_scheduler()
    profiles = await list_chatgpt_profiles()
    return {
        "ok": True,
        "profile_id": profile_id,
        "enabled": bool(body.enabled),
        "pool": settings.to_dict(),
        "profiles": profiles,
    }


@router.get("/pool/settings")
async def chatgpt_pool_settings_get(_: int = Depends(auth_key_id)):
    return {"ok": True, "pool": get_chatgpt_pool_settings().to_dict()}


@router.put("/pool/settings")
async def chatgpt_pool_settings_put(body: PoolSettingsBody, _: int = Depends(auth_key_id)):
    patch: dict[str, Any] = {}
    if body.max_concurrent is not None:
        patch["max_concurrent"] = body.max_concurrent
    if body.profile_default_max_concurrent is not None:
        patch["profile_default_max_concurrent"] = body.profile_default_max_concurrent
    settings = save_chatgpt_pool_settings(**patch) if patch else get_chatgpt_pool_settings()
    nudge_scheduler()
    return {"ok": True, "pool": settings.to_dict()}


@router.post("/pool/nudge")
async def chatgpt_pool_nudge(_: int = Depends(auth_key_id)):
    ensure_scheduler_started()
    nudge_scheduler()
    return {"ok": True, "queue": queue_summary()}


@router.post("/kpi/reset")
async def chatgpt_kpi_reset(_: int = Depends(auth_key_id)):
    from flow2api.services.chatgpt_counters import reset_counters

    counters = reset_counters()
    return {"ok": True, "summary": counters.to_dict(), "queue": queue_summary()}


class SlotCreateBody(BaseModel):
    label: str | None = None
    port: int | None = None


class SlotUpdateBody(BaseModel):
    label: str | None = None
    port: int | None = None


@router.get("/slots")
async def chatgpt_slots_list(_: int = Depends(auth_key_id)):
    from flow2api.services.chatgpt_playwright import playwright_slot_status

    st = await playwright_slot_status()
    return {
        "ok": True,
        "slots": st.get("slots") or [s.to_dict() for s in list_playwright_slots()],
        "pool": get_chatgpt_pool_settings().to_dict(),
        "profiles": await list_chatgpt_profiles(),
    }


@router.post("/slots")
async def chatgpt_slots_create(body: SlotCreateBody | None = None, _: int = Depends(auth_key_id)):
    body = body or SlotCreateBody()
    slot = add_playwright_slot(label=body.label, port=body.port)
    nudge_scheduler()
    return {
        "ok": True,
        "slot": slot.to_dict(),
        "pool": get_chatgpt_pool_settings().to_dict(),
        "profiles": await list_chatgpt_profiles(),
    }


@router.put("/slots/{slot_id}")
async def chatgpt_slots_update(
    slot_id: str,
    body: SlotUpdateBody,
    _: int = Depends(auth_key_id),
):
    try:
        slot = update_playwright_slot(slot_id, label=body.label, port=body.port)
    except KeyError:
        raise HTTPException(404, "slot_not_found") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    nudge_scheduler()
    return {"ok": True, "slot": slot.to_dict(), "profiles": await list_chatgpt_profiles()}


@router.delete("/slots/{slot_id}")
async def chatgpt_slots_delete(slot_id: str, _: int = Depends(auth_key_id)):
    from flow2api.services.chatgpt_playwright import reset_playwright_browser

    try:
        settings = remove_playwright_slot(slot_id)
    except KeyError:
        raise HTTPException(404, "slot_not_found") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await reset_playwright_browser(slot_id)
    nudge_scheduler()
    return {
        "ok": True,
        "pool": settings.to_dict(),
        "profiles": await list_chatgpt_profiles(),
    }


@router.post("/slots/{slot_id}/launch")
async def chatgpt_slots_launch(slot_id: str, _: int = Depends(auth_key_id)):
    from flow2api.services.chatgpt_playwright import reset_playwright_browser

    result = system_ops.launch_playwright_slot(slot_id)
    await reset_playwright_browser(slot_id)
    if not result.get("ok"):
        raise HTTPException(400, result.get("message") or result.get("error") or "launch_failed")
    nudge_scheduler()
    return {
        **result,
        "profiles": await list_chatgpt_profiles(),
    }


@router.post("/slots/launch-all")
async def chatgpt_slots_launch_all(_: int = Depends(auth_key_id)):
    from flow2api.services.chatgpt_playwright import reset_playwright_browser

    result = system_ops.launch_all_playwright_slots()
    await reset_playwright_browser()
    nudge_scheduler()
    return {
        **result,
        "profiles": await list_chatgpt_profiles(),
    }


@router.post("/web/chat")
async def chatgpt_web_chat(
    body: WebChatBody,
    async_mode: bool = Query(
        True,
        alias="async",
        description="Mặc định true: trả job id ngay rồi poll GET /web/chat/{id} (tránh Cloudflare 524). "
        "false = chờ xong trong 1 request (chỉ dùng local).",
    ),
    _: int = Depends(auth_key_id),
):
    kwargs = dict(
        prompt=body.prompt,
        model=body.model,
        endpoint=body.endpoint,
        profile_id=body.profile_id,
        conversation_id=body.conversation_id,
        parent_message_id=body.parent_message_id,
        images=body.images,
        mode=body.mode,
        system_hints=body.system_hints,
        picture=body.picture,
        tab_id=body.tab_id,
    )
    if async_mode:
        out = enqueue_web_chat(**kwargs)
        out["poll_url"] = f"/api/chatgpt/web/chat/{out['id']}"
        return out
    sync_kwargs = {k: v for k, v in kwargs.items() if k != "tab_id"}
    return await run_web_chat(**sync_kwargs)


@router.get("/web/chat/{job_id}")
async def chatgpt_web_chat_job(job_id: str, _: int = Depends(auth_key_id)):
    """Poll job ChatGPT async từ dashboard (`queued` | `running` | `done` | `failed`)."""
    return public_job_payload(job_id)


@router.get("/web/chat/{job_id}/media/{kind}/{index}")
async def chatgpt_web_chat_media(job_id: str, kind: str, index: int = 0):
    """Serve persisted ChatGPT result image/file (auth via middleware / access_token query)."""
    path = resolve_chatgpt_media_path(job_id, kind, index)
    if not path or not path.is_file():
        raise HTTPException(404, "chatgpt_media_not_found")
    ext = path.suffix.lower()
    mime = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".pdf": "application/pdf",
    }.get(ext, "application/octet-stream")
    return FileResponse(path, media_type=mime)


@router.post("/settings")
async def save_chatgpt_settings(body: OpenAISettingsBody, _: int = Depends(auth_key_id)):
    current = system_ops.openai_config()
    patch: dict[str, Any] = {}
    data = body.model_dump(exclude_none=True)

    if "api_key" in data:
        key = str(data["api_key"] or "").strip()
        # Keep existing key when UI sends masked placeholder / empty
        if key and "…" not in key and not key.endswith("…"):
            patch["api_key"] = key
        elif not key:
            patch["api_key"] = ""

    if "model" in data:
        model = str(data["model"] or "").strip()
        if model:
            patch["model"] = model

    if "base_url" in data:
        base = str(data["base_url"] or "").strip().rstrip("/")
        if base:
            patch["base_url"] = base

    if not patch:
        return {"ok": True, **system_ops.public_openai_config()}

    system_ops.save_config({"openai": {**current, **patch}})
    return {"ok": True, **system_ops.public_openai_config()}


@router.post("/chat")
async def chatgpt_chat(body: ChatBody, _: int = Depends(auth_key_id)):
    oai = system_ops.openai_config()
    if not oai["api_key"]:
        raise HTTPException(400, "missing_openai_api_key")

    model = (body.model or oai["model"]).strip() or oai["model"]
    url = f'{oai["base_url"]}/chat/completions'
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": m.role, "content": m.content} for m in body.messages],
        "stream": bool(body.stream),
    }
    if body.temperature is not None:
        payload["temperature"] = body.temperature

    headers = {
        "Authorization": f"Bearer {oai['api_key']}",
        "Content-Type": "application/json",
    }

    if body.stream:
        return StreamingResponse(
            _stream_chat(url, headers, payload),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        logger.warning("openai chat error: %s", exc)
        raise HTTPException(502, f"openai_request_failed: {exc}") from exc

    data = _safe_json(resp)
    if resp.status_code >= 400:
        detail = _openai_error_detail(data) or resp.text[:400] or f"http_{resp.status_code}"
        raise HTTPException(resp.status_code if resp.status_code < 500 else 502, detail)

    text = ""
    try:
        text = str(data["choices"][0]["message"]["content"] or "")
    except Exception:
        text = ""

    return {
        "ok": True,
        "model": data.get("model") or model,
        "text": text,
        "usage": data.get("usage"),
        "id": data.get("id"),
    }


async def _stream_chat(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> AsyncIterator[str]:
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as resp:
                if resp.status_code >= 400:
                    raw = await resp.aread()
                    try:
                        err = json.loads(raw.decode("utf-8", errors="replace"))
                    except Exception:
                        err = {"error": {"message": raw.decode("utf-8", errors="replace")[:400]}}
                    detail = _openai_error_detail(err) or f"http_{resp.status_code}"
                    yield f"data: {json.dumps({'error': detail}, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
                    return

                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    if line.startswith("data:"):
                        data = line[5:].strip()
                        if data == "[DONE]":
                            yield "data: [DONE]\n\n"
                            return
                        yield f"data: {data}\n\n"
                    else:
                        yield f": {line}\n\n"
                yield "data: [DONE]\n\n"
    except httpx.HTTPError as exc:
        logger.warning("openai stream error: %s", exc)
        yield f"data: {json.dumps({'error': str(exc)}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"


def _safe_json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {"raw": data}
    except Exception:
        return {"raw": resp.text[:500]}


def _openai_error_detail(data: dict[str, Any] | None) -> str | None:
    if not isinstance(data, dict):
        return None
    err = data.get("error")
    if isinstance(err, dict):
        msg = err.get("message") or err.get("code")
        return str(msg) if msg else None
    if isinstance(err, str):
        return err
    return None


# ── Public external API (giống /api/requests — Bearer token) ────────────
v1_router = APIRouter(prefix="/api/v1/chatgpt", tags=["chatgpt-v1"])


@v1_router.get("/status")
async def chatgpt_v1_status(
    profile_id: str | None = None,
    _: int = Depends(auth_key_id),
):
    """Kiểm tra extension / session sẵn sàng cho ChatGPT web."""
    return await web_status_payload(profile_id)


@v1_router.post("/chat")
async def chatgpt_v1_chat(
    body: PublicChatBody,
    async_mode: bool = Query(
        True,
        alias="async",
        description="Mặc định true: trả job id ngay rồi poll GET /chat/{id} (tránh Cloudflare 524). "
        "false = chờ xong trong 1 request (chỉ dùng local / không qua Cloudflare).",
    ),
    _: int = Depends(auth_key_id),
):
    """
    Gửi tin nhắn ChatGPT qua session Chrome extension.

    Mặc định **async** (khuyên dùng qua `flow2.viettheo.site`):
    - POST trả `{id, status:"queued", poll_url}`
    - Poll `GET /api/v1/chatgpt/chat/{id}` đến `done` / `failed`

    Sync: `?async=false` — chờ kết quả trong cùng request (dễ 524 qua Cloudflare).

    Multi-turn: gửi lại `conversation_id` + `parent_message_id` (= `message_id` lần trước).
    Ảnh upload: truyền `images[].data` dạng data URL hoặc base64.
    Tạo ảnh (Conversation image): `mode: "picture_v2"` hoặc `system_hints: ["picture_v2"]`.
    """
    kwargs = dict(
        prompt=body.prompt,
        model=body.model,
        endpoint=body.endpoint,
        profile_id=body.profile_id,
        conversation_id=body.conversation_id,
        parent_message_id=body.parent_message_id,
        images=list(body.images or []),
        mode=body.mode,
        system_hints=body.system_hints,
        picture=body.picture,
    )
    if async_mode:
        return enqueue_web_chat(**kwargs)
    return await run_web_chat(**kwargs)


@v1_router.get("/chat/{job_id}")
async def chatgpt_v1_chat_job(job_id: str, _: int = Depends(auth_key_id)):
    """Poll trạng thái job ChatGPT async (`queued` | `running` | `done` | `failed`)."""
    return public_job_payload(job_id)