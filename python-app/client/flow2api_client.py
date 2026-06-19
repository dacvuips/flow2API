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
            params["variant_count"] = variant_count
        else:
            params["video_quality"] = video_quality
        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(
                f"{self.base_url}/api/requests",
                headers=self._headers(),
                json={"type": req_type, "params": params},
            )
            r.raise_for_status()
            return r.json()

    def create_image(self, **kwargs: Any) -> dict:
        return self.create("gen_image", **kwargs)

    def create_text_video(self, **kwargs: Any) -> dict:
        return self.create("gen_text_video", **kwargs)

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
    ) -> dict | bytes:
        """Upscale ảnh đã generate lên 2K hoặc 4K. resolution: 2k | 4k."""
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
        params = {"download": "true"} if download else {}
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
