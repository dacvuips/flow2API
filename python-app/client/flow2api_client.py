"""
Flow2API Python client — gọi API local hoặc qua Cloudflare Tunnel.

Ví dụ:
    client = Flow2APIClient("http://localhost:1994", "f2api_...")
    job = client.create_image(prompt="...", aspect_ratio="16:9")
    result = client.wait(job["id"])
"""
from __future__ import annotations

import time
from typing import Any, Optional

import httpx


class Flow2APIClient:
    def __init__(self, base_url: str, token: str, timeout: float = 300.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def create(
        self,
        req_type: str,
        *,
        prompt: str,
        aspect_ratio: str = "16:9",
        image_model: str = "NANO_BANANA_PRO",
        variant_count: int = 1,
        video_quality: str = "fast",
        **extra: Any,
    ) -> dict:
        params: dict[str, Any] = {
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            **extra,
        }
        if req_type == "gen_image":
            params["image_model"] = image_model
        else:
            params["video_quality"] = video_quality
        params["variant_count"] = variant_count
        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(
                f"{self.base_url}/api/requests",
                headers=self._headers(),
                json={"type": req_type, "params": params},
            )
            r.raise_for_status()
            return r.json()

    def create_text(
        self,
        *,
        prompt: str,
        system_instruction: str = "",
        model: str = "gemini-3-flash-preview",
        thinking_level: Optional[str] = None,
        image_base64s: Optional[list[str]] = None,
        audio_base64s: Optional[list[str]] = None,
        schema: Optional[dict] = None,
        json: Optional[bool] = None,
        profile_id: Optional[str] = None,
        **extra: Any,
    ) -> dict:
        """Text via aisandbox flow:generateContent (Gemini).

        thinking_level: optional MINIMAL|LOW|MEDIUM|HIGH.
        Omit to use server default (LOW — Flow UI). Empty system_instruction
        uses Flow screenplay convention unless image_base64s / audio_base64s
        is set (vision / audio analysis).
        schema / json=True → generationConfig.responseMimeType=application/json.
        """
        params: dict[str, Any] = {
            "prompt": prompt,
            "model": model,
            **extra,
        }
        if thinking_level is not None and str(thinking_level).strip():
            params["thinking_level"] = str(thinking_level).strip().upper()
        if system_instruction:
            params["system_instruction"] = system_instruction
        if image_base64s:
            params["image_base64s"] = list(image_base64s)
        if audio_base64s:
            params["audio_base64s"] = list(audio_base64s)
        if schema:
            params["schema"] = schema
        if json:
            params["json"] = True
        if profile_id:
            params["profile_id"] = profile_id
        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(
                f"{self.base_url}/api/requests",
                headers=self._headers(),
                json={"type": "gen_text", "params": params},
            )
            r.raise_for_status()
            return r.json()

    def create_image(self, **kwargs: Any) -> dict:
        return self.create("gen_image", **kwargs)

    def create_text_video(self, **kwargs: Any) -> dict:
        return self.create("gen_text_video", **kwargs)

    def create_audio(
        self,
        *,
        prompt: str = "",
        dialog: str = "",
        voice: str = "achernar",
        model_key: str = "gemini_v4s_tts_flow",
        audio_model: Optional[str] = None,
        modelKey: Optional[str] = None,
        profile_id: Optional[str] = None,
        **extra: Any,
    ) -> dict:
        text = str(dialog or prompt or "").strip()
        mk = str(modelKey or model_key or audio_model or "gemini_v4s_tts_flow").strip()
        params: dict[str, Any] = {
            "dialog": text,
            "prompt": text,
            "voice": voice,
            "modelKey": mk,
            "audio_model": mk,
            **extra,
        }
        if profile_id:
            params["profile_id"] = profile_id
        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(
                f"{self.base_url}/api/requests",
                headers=self._headers(),
                json={"type": "gen_audio", "params": params},
            )
            r.raise_for_status()
            return r.json()

    def upload(
        self,
        *,
        base64: str,
        mime_type: str = "",
        file_name: str = "",
        project_id: Optional[str] = None,
        profile_id: Optional[str] = None,
    ) -> dict:
        """Upload ảnh hoặc video lên Google Flow — trả mediaId."""
        body: dict[str, Any] = {"base64": base64}
        if mime_type:
            body["mime_type"] = mime_type
        if file_name:
            body["file_name"] = file_name
        if project_id:
            body["project_id"] = project_id
        if profile_id:
            body["profile_id"] = profile_id
        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(
                f"{self.base_url}/api/flow/upload",
                headers=self._headers(),
                json=body,
            )
            r.raise_for_status()
            data = r.json()
            media_id = data.get("mediaId") or data.get("media_id")
            if media_id and "mediaId" not in data:
                data["mediaId"] = media_id
            return data

    def get(self, request_id: str) -> dict:
        with httpx.Client(timeout=60.0) as client:
            r = client.get(
                f"{self.base_url}/api/requests/{request_id}",
                headers=self._headers(),
            )
            r.raise_for_status()
            return r.json()

    def wait(
        self,
        request_id: str,
        *,
        poll_interval: float = 2.5,
        max_attempts: int = 120,
    ) -> dict:
        for _ in range(max_attempts):
            task = self.get(request_id)
            status = str(task.get("status", "")).lower()
            if status == "done":
                return task
            if status.startswith("failed"):
                raise RuntimeError(task.get("error") or status)
            time.sleep(poll_interval)
        raise TimeoutError(f"timeout_waiting_result:{request_id}")

    def upsample_image(
        self,
        *,
        media_id: Optional[str] = None,
        request_id: Optional[str] = None,
        index: int = 0,
        project_id: Optional[str] = None,
        profile_id: Optional[str] = None,
        resolution: str = "4k",
        download: bool = False,
        sync: bool = False,
        poll_interval: float = 2.5,
        max_attempts: int = 120,
    ) -> dict | bytes:
        """Upscale ảnh đã generate lên 2K hoặc 4K. resolution: 2k | 4k.

        Mặc định async queue + poll (an toàn với Cloudflare). sync=True chỉ dùng local.
        """
        body: dict[str, Any] = {
            "target_resolution": resolution,
            "index": index,
        }
        if media_id:
            body["media_id"] = media_id
        if request_id:
            body["request_id"] = request_id
        if project_id:
            body["project_id"] = project_id
        if profile_id:
            body["profile_id"] = profile_id
        if sync:
            params: dict[str, Any] = {"sync": "true"}
            if download:
                params["download"] = "true"
            with httpx.Client(timeout=self.timeout) as client:
                r = client.post(
                    f"{self.base_url}/api/requests/upsample-image",
                    headers=self._headers(),
                    params=params,
                    json=body,
                )
                r.raise_for_status()
                if download:
                    return r.content
                return r.json()
        with httpx.Client(timeout=60.0) as client:
            r = client.post(
                f"{self.base_url}/api/requests/upsample-image",
                headers=self._headers(),
                json=body,
            )
            r.raise_for_status()
            job = r.json()
        job_id = str(job.get("id") or "")
        if not job_id:
            raise RuntimeError("missing_upsample_job_id")
        task = self.wait(job_id, poll_interval=poll_interval, max_attempts=max_attempts)
        if download:
            with httpx.Client(timeout=self.timeout) as client:
                dr = client.get(
                    f"{self.base_url}/api/requests/{job_id}",
                    headers=self._headers(),
                    params={"download": "true"},
                )
                dr.raise_for_status()
                return dr.content
        return task

    def upsample_image_4k(
        self,
        *,
        media_id: Optional[str] = None,
        request_id: Optional[str] = None,
        index: int = 0,
        project_id: Optional[str] = None,
        profile_id: Optional[str] = None,
        download: bool = False,
    ) -> dict | bytes:
        return self.upsample_image(
            media_id=media_id,
            request_id=request_id,
            index=index,
            project_id=project_id,
            profile_id=profile_id,
            resolution="4k",
            download=download,
        )

    def upsample_image_2k(
        self,
        *,
        media_id: Optional[str] = None,
        request_id: Optional[str] = None,
        index: int = 0,
        project_id: Optional[str] = None,
        profile_id: Optional[str] = None,
        download: bool = False,
    ) -> dict | bytes:
        return self.upsample_image(
            media_id=media_id,
            request_id=request_id,
            index=index,
            project_id=project_id,
            profile_id=profile_id,
            resolution="2k",
            download=download,
        )

    def upsample_video(
        self,
        *,
        media_id: Optional[str] = None,
        request_id: Optional[str] = None,
        index: int = 0,
        project_id: Optional[str] = None,
        profile_id: Optional[str] = None,
        aspect_ratio: Optional[str] = None,
        workflow_id: Optional[str] = None,
        download: bool = False,
        poll_interval: float = 2.5,
        max_attempts: int = 240,
    ) -> dict | bytes:
        """Upscale video đã generate lên 1080p (async queue + poll)."""
        body: dict[str, Any] = {"index": index}
        if media_id:
            body["media_id"] = media_id
        if request_id:
            body["request_id"] = request_id
        if project_id:
            body["project_id"] = project_id
        if profile_id:
            body["profile_id"] = profile_id
        if aspect_ratio:
            body["aspect_ratio"] = aspect_ratio
        if workflow_id:
            body["workflow_id"] = workflow_id
        with httpx.Client(timeout=60.0) as client:
            r = client.post(
                f"{self.base_url}/api/requests/upsample-video",
                headers=self._headers(),
                json=body,
            )
            r.raise_for_status()
            job = r.json()
        job_id = str(job.get("id") or "")
        if not job_id:
            raise RuntimeError("missing_upsample_job_id")
        task = self.wait(job_id, poll_interval=poll_interval, max_attempts=max_attempts)
        if download:
            with httpx.Client(timeout=self.timeout) as client:
                dr = client.get(
                    f"{self.base_url}/api/requests/{job_id}",
                    headers=self._headers(),
                    params={"download": "true"},
                )
                dr.raise_for_status()
                return dr.content
        return task

    def chatgpt_status(self, profile_id: Optional[str] = None) -> dict:
        """Kiểm tra extension/session ChatGPT sẵn sàng."""
        params: dict[str, Any] = {}
        if profile_id:
            params["profile_id"] = profile_id
        with httpx.Client(timeout=30.0) as client:
            r = client.get(
                f"{self.base_url}/api/v1/chatgpt/status",
                headers=self._headers(),
                params=params or None,
            )
            r.raise_for_status()
            return r.json()

    def chatgpt_chat(
        self,
        prompt: str = "",
        *,
        model: str = "gpt-5-5",
        conversation_id: Optional[str] = None,
        parent_message_id: Optional[str] = None,
        profile_id: Optional[str] = None,
        images: Optional[list[dict[str, Any]]] = None,
        endpoint: Optional[str] = None,
        mode: Optional[str] = None,
        system_hints: Optional[list[str]] = None,
        picture: Optional[bool] = None,
        async_mode: bool = True,
        wait: bool = True,
        poll_interval: float = 2.0,
        max_wait: float = 600.0,
    ) -> dict:
        """
        Gửi chat ChatGPT qua web session trên Chrome extension.

        Mặc định async=True (an toàn với Cloudflare): POST trả job id, rồi poll.
        wait=True: chờ đến done/failed rồi trả result (giống sync từ phía caller).
        wait=False: trả ngay {"id","status","poll_url"} — caller tự poll.

        images: [{"data": "data:image/jpeg;base64,...", "file_name": "a.jpg"}]
        mode="picture_v2" / system_hints=["picture_v2"]: tạo ảnh Conversation image.
        Multi-turn: truyền lại conversation_id + parent_message_id (= message_id trước).

        Response (khi done) gồm text, images[] (ảnh assistant), files[] (file tải được).
        """
        body: dict[str, Any] = {"prompt": prompt, "model": model}
        if conversation_id:
            body["conversation_id"] = conversation_id
        if parent_message_id:
            body["parent_message_id"] = parent_message_id
        if profile_id:
            body["profile_id"] = profile_id
        if endpoint:
            body["endpoint"] = endpoint
        if images:
            body["images"] = images
        if mode:
            body["mode"] = mode
        if system_hints:
            body["system_hints"] = system_hints
        if picture is not None:
            body["picture"] = picture
        params = {"async": "true" if async_mode else "false"}
        with httpx.Client(timeout=self.timeout if not async_mode else 60.0) as client:
            r = client.post(
                f"{self.base_url}/api/v1/chatgpt/chat",
                headers=self._headers(),
                params=params,
                json=body,
            )
            r.raise_for_status()
            data = r.json()
        if not async_mode or not wait:
            return data
        job_id = data.get("id")
        if not job_id:
            return data
        return self.chatgpt_chat_wait(job_id, poll_interval=poll_interval, max_wait=max_wait)

    def chatgpt_chat_job(self, job_id: str) -> dict:
        """Poll 1 lần trạng thái job ChatGPT async."""
        with httpx.Client(timeout=30.0) as client:
            r = client.get(
                f"{self.base_url}/api/v1/chatgpt/chat/{job_id}",
                headers=self._headers(),
            )
            r.raise_for_status()
            return r.json()

    def chatgpt_chat_wait(
        self,
        job_id: str,
        *,
        poll_interval: float = 2.0,
        max_wait: float = 600.0,
    ) -> dict:
        """Poll đến done/failed rồi trả payload (có flatten text/images/files)."""
        import time

        deadline = time.time() + max(1.0, max_wait)
        last: dict = {}
        while time.time() < deadline:
            last = self.chatgpt_chat_job(job_id)
            status = str(last.get("status") or "").lower()
            if status == "done":
                return last.get("result") or last
            if status == "failed":
                err = last.get("error") or "chatgpt_job_failed"
                raise RuntimeError(err)
            time.sleep(max(0.5, poll_interval))
        raise TimeoutError(f"chatgpt_job_timeout:{job_id}")
