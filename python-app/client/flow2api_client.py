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
