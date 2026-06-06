"""Ví dụ tạo ảnh qua Flow2API."""
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
    result = client.wait(job["id"])
    print("done:", result.get("result"))
