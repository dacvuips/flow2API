"""Example: clean watermark from local image/video via Flow2API.

  set FLOW2API_TOKEN=f2api_...
  python client/example_watermark_clean.py path/to/image.jpg
  python client/example_watermark_clean.py path/to/video.mp4
"""
from __future__ import annotations

import base64
import mimetypes
import os
import sys
from pathlib import Path

import requests

BASE_URL = os.environ.get("FLOW2API_BASE", "http://127.0.0.1:1994").rstrip("/")
TOKEN = os.environ.get("FLOW2API_TOKEN", "").strip()


def file_to_data_url(path: Path) -> str:
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def main() -> None:
    if not TOKEN:
        raise SystemExit("Set FLOW2API_TOKEN=f2api_...")
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python example_watermark_clean.py <image|video>")
    path = Path(sys.argv[1]).expanduser().resolve()
    if not path.is_file():
        raise SystemExit(f"File not found: {path}")

    mime = mimetypes.guess_type(str(path))[0] or ""
    is_video = mime.startswith("video/") or path.suffix.lower() in {
        ".mp4",
        ".mov",
        ".webm",
        ".m4v",
        ".mkv",
    }
    data_url = file_to_data_url(path)
    body: dict = {"return_mode": "url" if is_video else "both"}
    if is_video:
        body["video_base64"] = data_url
    else:
        body["image_base64"] = data_url

    r = requests.post(
        f"{BASE_URL}/api/watermark/clean",
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        json=body,
        timeout=600 if is_video else 120,
    )
    r.raise_for_status()
    data = r.json()
    print("cleaned:", data.get("cleaned"), "kind:", data.get("kind"))
    print("url:", data.get("url"))
    print("ncc:", data.get("ncc"), "elapsed:", data.get("elapsed_seconds"))

    out = path.with_name(f"{path.stem}_clean{path.suffix if is_video else '.jpg'}")
    if data.get("media_base64"):
        out.write_bytes(base64.b64decode(data["media_base64"]))
        print("saved", out)
    elif data.get("url"):
        print("download from", data["url"])


if __name__ == "__main__":
    main()
