"""Ví dụ tạo ảnh rồi upscale 4K qua Flow2API."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from client.flow2api_client import Flow2APIClient

BASE = os.environ.get("FLOW2API_BASE", "http://127.0.0.1:1994")
TOKEN = os.environ.get("FLOW2API_TOKEN", "f2api_YOUR_TOKEN_HERE")

if __name__ == "__main__":
    client = Flow2APIClient(BASE, TOKEN)
    job = client.create_image(
        prompt="A cinematic fashion photo, realistic lighting",
        aspect_ratio="16:9",
        image_model="NANO_BANANA_PRO",
        variant_count=1,
    )
    print("queued:", job)
    task = client.wait(job["id"])
    result = task.get("result") or {}
    media_ids = result.get("media_ids") or []
    print("media_ids:", media_ids)

    # Cách 1: truyền request_id
    upscaled_2k = client.upsample_image_2k(request_id=job["id"])
    upscaled_4k = client.upsample_image_4k(request_id=job["id"])
    print("2k json:", upscaled_2k)
    print("4k json:", upscaled_4k)

    # Cách 2: tải thẳng file bytes
    if media_ids:
        image_bytes = client.upsample_image_4k(media_id=media_ids[0], download=True)
        out = "output_4k.jpg"
        with open(out, "wb") as f:
            f.write(image_bytes)
        print("saved:", out, len(image_bytes), "bytes")
