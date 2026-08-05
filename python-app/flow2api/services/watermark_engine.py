"""Media watermark cleanup engines for Flow2API.

- Images: Erasio reverse-blend (port of all-tool remove-flow-watermark).
- Videos: OpenMark fixed-region Telea inpaint or edge crop + FFmpeg.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image


@dataclass(frozen=True)
class Region:
    """Normalized rectangle: left, top, width, height in 0..1 space."""

    left: float
    top: float
    width: float
    height: float

    def pixels(self, frame_width: int, frame_height: int) -> tuple[int, int, int, int]:
        x = round(self.left * frame_width)
        y = round(self.top * frame_height)
        width = round(self.width * frame_width)
        height = round(self.height * frame_height)
        if width < 1 or height < 1:
            raise ValueError("Region is too small for this media size.")
        if x < 0 or y < 0 or x + width > frame_width or y + height > frame_height:
            raise ValueError("Region is outside the media bounds.")
        return x, y, width, height


# Calibrated from Flow/Veo 1280x720 (~35x19 logo) and Gemini bottom-right mark.
VEO_BOTTOM_RIGHT = Region(0.968, 0.963, 0.027, 0.027)
GEMINI_BOTTOM_RIGHT = Region(0.879, 0.771, 0.094, 0.139)

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".flv"}


def inpaint(frame: np.ndarray, region: Region, radius: int = 5) -> np.ndarray:
    height, width = frame.shape[:2]
    x, y, box_width, box_height = region.pixels(width, height)
    mask = np.zeros((height, width), dtype=np.uint8)
    mask[y : y + box_height, x : x + box_width] = 255
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    mask = cv2.dilate(mask, kernel)
    return cv2.inpaint(frame, mask, radius, cv2.INPAINT_TELEA)


def clean_image_file(source: Path, output: Path, region: Region | None = None) -> None:
    """Clean Gemini/Flow image sparkle mark (Erasio reverse-blend from all-tool).

    ``region`` is ignored — detection uses alpha maps 48/96 + NCC.
    Kept for API compatibility with older callers.
    """
    del region  # auto-detect
    from flow2api.services.watermark_image_erasio import clean_image_file_erasio

    clean_image_file_erasio(Path(source), Path(output), flow_known_watermark=True)


def strip_image_metadata(source: Path, output: Path) -> None:
    with Image.open(source) as image:
        if image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
        image.save(output)


def _bundled_ffmpeg_candidates() -> list[Path]:
    """Local paths: FLOW2API root / this package / imageio-ffmpeg wheel."""
    roots: list[Path] = []
    try:
        from flow2api.config import APP_ROOT, ROOT

        roots.extend([APP_ROOT, ROOT, ROOT.parent])
    except Exception:
        pass
    here = Path(__file__).resolve()
    roots.extend([here.parents[2], here.parents[3] if len(here.parents) > 3 else here.parents[2]])
    names = (
        Path("tools") / "ffmpeg" / "bin" / "ffmpeg.exe",
        Path("tools") / "ffmpeg" / "ffmpeg.exe",
        Path("tools") / "ffmpeg" / "bin" / "ffmpeg",
        Path("tools") / "ffmpeg" / "ffmpeg",
    )
    out: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        for rel in names:
            candidate = (root / rel).resolve()
            key = str(candidate).lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(candidate)
    return out


def ffmpeg_executable() -> str:
    """Resolve FFmpeg: env → PATH → tools/ffmpeg → imageio-ffmpeg (pip install)."""
    override = os.environ.get("OPENMARK_FFMPEG", "").strip() or os.environ.get(
        "FLOW2API_FFMPEG", ""
    ).strip()
    if override:
        path = Path(override)
        if path.is_file():
            return str(path)
        raise RuntimeError(f"FLOW2API_FFMPEG / OPENMARK_FFMPEG is not a file: {override}")

    found = shutil.which("ffmpeg")
    if found:
        return found

    for candidate in _bundled_ffmpeg_candidates():
        if candidate.is_file():
            return str(candidate)

    try:
        import imageio_ffmpeg

        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled and Path(bundled).is_file():
            return bundled
    except Exception:
        pass

    raise RuntimeError(
        "ffmpeg was not found. Run install.bat (installs imageio-ffmpeg), "
        "or set FLOW2API_FFMPEG to ffmpeg.exe."
    )


def _run_ffmpeg(args: list[str]) -> None:
    completed = subprocess.run(
        [ffmpeg_executable(), "-hide_banner", "-loglevel", "error", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        shell=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "FFmpeg failed.")


def clean_video_file(source: Path, output: Path, region: Region) -> int:
    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        raise ValueError(f"OpenCV could not decode video: {source}")
    frame_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = capture.get(cv2.CAP_PROP_FPS)
    if frame_width < 1 or frame_height < 1 or fps <= 0:
        capture.release()
        raise ValueError("Video has invalid dimensions or frame rate.")
    region.pixels(frame_width, frame_height)
    with tempfile.TemporaryDirectory(prefix="openmark-") as directory:
        silent_video = Path(directory) / "silent.mp4"
        writer = cv2.VideoWriter(
            str(silent_video),
            cv2.VideoWriter_fourcc(*"mp4v"),  # type: ignore[attr-defined]
            fps,
            (frame_width, frame_height),
        )
        if not writer.isOpened():
            capture.release()
            raise RuntimeError("OpenCV could not initialize video encoder.")
        frames = 0
        try:
            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                writer.write(inpaint(frame, region))
                frames += 1
        finally:
            capture.release()
            writer.release()
        if frames == 0:
            raise ValueError("Video contains no decodable frames.")
        _run_ffmpeg(
            [
                "-y",
                "-i",
                str(silent_video),
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-map",
                "1:a?",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "copy",
                "-map_metadata",
                "-1",
                "-movflags",
                "+faststart",
                str(output),
            ]
        )
    return frames


def crop_video_file(
    source: Path, output: Path, crop_right: float, crop_bottom: float
) -> int:
    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        raise ValueError(f"OpenCV could not decode video: {source}")
    frame_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    frame_count = round(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    capture.release()
    cut_right = round(crop_right * frame_width)
    cut_bottom = round(crop_bottom * frame_height)
    kept_width = frame_width - cut_right
    kept_height = frame_height - cut_bottom
    if frame_width < 2 or frame_height < 2 or kept_width < 2 or kept_height < 2:
        raise ValueError("Crop removes too much of the video frame.")
    filtergraph = (
        f"crop={kept_width}:{kept_height}:0:0,"
        f"scale={frame_width}:{frame_height}:flags=lanczos"
    )
    _run_ffmpeg(
        [
            "-y",
            "-i",
            str(source),
            "-filter:v",
            filtergraph,
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-crf",
            "18",
            "-preset",
            "medium",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "copy",
            "-map_metadata",
            "-1",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )
    return frame_count
