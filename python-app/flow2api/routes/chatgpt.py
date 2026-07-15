"""Chat GPT tab — OpenAI official API + chatgpt.com web conversation via extension."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from flow2api.services import system_ops
from flow2api.services.api_auth import auth_key_id
from flow2api.services.chatgpt_broker import get_chatgpt_broker
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
) -> dict[str, Any]:
    prompt = (prompt or "").strip()
    norm_images = _normalize_images(images)
    if not prompt and not norm_images:
        raise HTTPException(400, "empty_prompt")

    endpoint = (endpoint or "").strip() or DEFAULT_WEB_ENDPOINT
    model = (model or "").strip() or DEFAULT_WEB_MODEL
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

    session = _try_pick_ws_session(profile_id)
    if session:
        result = await session.chatgpt_send(params, timeout=180.0)
        return _format_web_result(
            result,
            endpoint=endpoint,
            model=model,
            via="ws",
            profile_id=session.profile_id,
            profile_label=session.display_name(),
        )

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
            "Agent phải chạy ở http://127.0.0.1:1994.",
        )

    result = await broker.submit(params, timeout=240.0)
    return _format_web_result(result, endpoint=endpoint, model=model, via="http")


async def _run_public_chat_job(job_id: str, kwargs: dict[str, Any]) -> None:
    broker = get_chatgpt_broker()
    broker.mark_public_running(job_id)
    try:
        result = await run_web_chat(**kwargs)
        broker.finish_public_job(job_id, result=result)
    except HTTPException as exc:
        detail = exc.detail
        if not isinstance(detail, str):
            detail = str(detail)
        broker.finish_public_job(job_id, error=detail)
    except Exception as exc:
        logger.exception("chatgpt public job %s failed", job_id[:8])
        broker.finish_public_job(job_id, error=str(exc) or "chatgpt_job_failed")


def enqueue_web_chat(
    *,
    prompt: str = "",
    model: str | None = None,
    endpoint: str | None = None,
    profile_id: str | None = None,
    conversation_id: str | None = None,
    parent_message_id: str | None = None,
    images: list[Any] | None = None,
) -> dict[str, Any]:
    """Enqueue chat job and return immediately (Cloudflare-safe)."""
    prompt = (prompt or "").strip()
    norm_images = _normalize_images(images)
    if not prompt and not norm_images:
        raise HTTPException(400, "empty_prompt")

    kwargs = {
        "prompt": prompt,
        "model": model,
        "endpoint": endpoint,
        "profile_id": profile_id,
        "conversation_id": conversation_id,
        "parent_message_id": parent_message_id,
        "images": images,
    }
    broker = get_chatgpt_broker()
    job = broker.create_public_job(
        {
            "prompt": prompt,
            "model": (model or "").strip() or DEFAULT_WEB_MODEL,
            "endpoint": (endpoint or "").strip() or DEFAULT_WEB_ENDPOINT,
            "profile_id": profile_id,
            "conversation_id": conversation_id,
            "parent_message_id": parent_message_id,
            "images": norm_images,
        }
    )
    task = asyncio.create_task(_run_public_chat_job(job.job_id, kwargs))
    broker.track_public_task(job.job_id, task)
    logger.info("chatgpt public job queued %s", job.job_id[:8])
    return {
        "ok": True,
        "id": job.job_id,
        "status": "queued",
        "poll_url": f"/api/v1/chatgpt/chat/{job.job_id}",
    }


def public_job_payload(job_id: str) -> dict[str, Any]:
    broker = get_chatgpt_broker()
    job = broker.get_public_job(job_id)
    if not job:
        raise HTTPException(404, "chatgpt_job_not_found")
    payload = job.to_dict(include_result=True)
    payload["ok"] = job.status != "failed"
    if job.status == "done" and isinstance(job.result, dict):
        # Flatten common fields for convenient poll clients
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
            if key in job.result and key not in payload:
                payload[key] = job.result[key]
    return payload


async def web_status_payload(profile_id: str | None = None) -> dict[str, Any]:
    pool = get_extension_pool()
    pool.hydrate_db_profiles()
    broker = get_chatgpt_broker()
    broker_stats = broker.stats()
    http_workers = broker.online_workers()
    profiles = [
        {
            "profile_id": s.profile_id,
            "display_name": s.display_name(),
            "online": s.connected,
            "email": s.email,
        }
        for s in pool.list_sessions()
    ]
    online = [p for p in profiles if p["online"]]
    session = _try_pick_ws_session(profile_id)

    base = {
        "ok": True,
        "mode": "web",
        "profiles_online": len(online),
        "profiles_total": len(profiles),
        "profiles_offline_gen": pool.offline_gen_count(),
        "profiles": profiles,
        "http_workers_online": len(http_workers),
        "http_workers": http_workers,
        "broker": broker_stats,
        "default_endpoint": DEFAULT_WEB_ENDPOINT,
        "default_model": DEFAULT_WEB_MODEL,
        "transport": "ws" if session else ("http" if http_workers else "none"),
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
            "hint": "Extension đang poll HTTP (không cần Bridge WS). Gửi chat sẽ qua hàng đợi.",
        }

    return {
        **base,
        "ok": False,
        "extension_connected": False,
        "loggedIn": False,
        "error": (
            "Chưa có extension sẵn sàng (WS offline và chưa có HTTP poll). "
            "Reload extension Flow2API trên Chrome profile đã login chatgpt.com, "
            "đảm bảo Python agent chạy ở http://127.0.0.1:1994."
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


@router.post("/web/chat")
async def chatgpt_web_chat(body: WebChatBody, _: int = Depends(auth_key_id)):
    return await run_web_chat(
        prompt=body.prompt,
        model=body.model,
        endpoint=body.endpoint,
        profile_id=body.profile_id,
        conversation_id=body.conversation_id,
        parent_message_id=body.parent_message_id,
        images=body.images,
    )


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
    Ảnh: truyền `images[].data` dạng data URL hoặc base64.
    """
    kwargs = dict(
        prompt=body.prompt,
        model=body.model,
        endpoint=body.endpoint,
        profile_id=body.profile_id,
        conversation_id=body.conversation_id,
        parent_message_id=body.parent_message_id,
        images=list(body.images or []),
    )
    if async_mode:
        return enqueue_web_chat(**kwargs)
    return await run_web_chat(**kwargs)


@v1_router.get("/chat/{job_id}")
async def chatgpt_v1_chat_job(job_id: str, _: int = Depends(auth_key_id)):
    """Poll trạng thái job ChatGPT async (`queued` | `running` | `done` | `failed`)."""
    return public_job_payload(job_id)