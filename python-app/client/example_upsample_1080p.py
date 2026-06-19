"""Ví dụ tạo video rồi upscale 1080p qua Flow2API."""
import os

from flow2api_client import Flow2APIClient

BASE = os.environ.get("FLOW2API_BASE", "http://127.0.0.1:1994")
TOKEN = os.environ.get("FLOW2API_TOKEN", "")


def main() -> None:
    if not TOKEN:
        raise SystemExit("Đặt FLOW2API_TOKEN trước khi chạy")

    client = Flow2APIClient(BASE, TOKEN)
    job = client.create_text_video(
        prompt="A cinematic realistic video, smooth camera movement",
        aspect_ratio="16:9",
        video_quality="fast",
    )
    print("queued:", job)
    task = client.wait(job["id"], max_attempts=240)
    print("done:", task.get("id"), task.get("result", {}).get("media_ids"))

    upscaled = client.upsample_video(request_id=job["id"])
    print("1080p json:", upscaled)

    media_ids = (task.get("result") or {}).get("media_ids") or []
    if media_ids:
        video_bytes = client.upsample_video(media_id=media_ids[0], download=True)
        out = "output_1080p.mp4"
        with open(out, "wb") as f:
            f.write(video_bytes)  # type: ignore[arg-type]
        print("saved", out)


if __name__ == "__main__":
    main()
