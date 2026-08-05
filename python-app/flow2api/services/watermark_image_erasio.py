"""Erasio-style Gemini/Flow image sparkle watermark remover.

Port of all-tool `src/libs/remove-flow-watermark/remove-image.ts` + alpha maps:
  1. Match alpha templates 48 / 96 (NCC)
  2. Auto margin 32 / 64; weak auto → scan bottom-right + refine
  3. reverseBlend: original = (blended − α·255) / (1 − α)

Video cleaning stays in watermark_engine (OpenMark path).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

OVERLAY_VALUE = 255.0
ALPHA_FLOOR = 0.002
ALPHA_CEILING = 0.99
SMALL_MARK_SIZE = 48
LARGE_MARK_SIZE = 96
NCC_THRESHOLD = 0.25
NCC_GOOD = 0.5
SCAN_STRIDE = 8
REFINE_RADIUS = 8
GAIN_DRIFT = 0.62

_ASSETS = Path(__file__).resolve().parent.parent / "assets" / "watermark"


@dataclass(frozen=True)
class WatermarkAlphaMap:
    values: np.ndarray  # float32 flat length side*side
    side: int


@dataclass(frozen=True)
class MarkRegion:
    x: int
    y: int
    w: int
    h: int


@dataclass
class DetectedMark:
    side: int
    alpha_map: WatermarkAlphaMap
    x: int
    y: int
    ncc: float
    auto_x: int
    auto_y: int
    method: str  # auto | scan


@lru_cache(maxsize=4)
def _load_alpha_map(side: int) -> WatermarkAlphaMap:
    path = _ASSETS / f"alpha_{side}.png"
    if not path.is_file():
        raise FileNotFoundError(f"Alpha map missing: {path}")
    with Image.open(path) as img:
        img = img.convert("RGBA")
        if img.size != (side, side):
            img = img.resize((side, side), Image.Resampling.BILINEAR)
        arr = np.asarray(img, dtype=np.float32)
    # Erasio gray maps: α = max(R,G,B) / 255
    values = arr[:, :, :3].max(axis=2).reshape(-1) / 255.0
    return WatermarkAlphaMap(values=values.astype(np.float32), side=side)


def get_image_alpha_maps() -> tuple[WatermarkAlphaMap, WatermarkAlphaMap]:
    return _load_alpha_map(SMALL_MARK_SIZE), _load_alpha_map(LARGE_MARK_SIZE)


def compute_watermark_ncc(
    rgba: np.ndarray,
    width: int,
    height: int,
    region: MarkRegion,
    alpha_map: WatermarkAlphaMap,
) -> float:
    """Pearson NCC between alpha template and image luminance (0..1)."""
    x1 = max(0, region.x)
    y1 = max(0, region.y)
    x2 = min(width, region.x + region.w)
    y2 = min(height, region.y + region.h)
    if x1 >= x2 or y1 >= y2:
        return 0.0

    side = alpha_map.side
    alphas: list[float] = []
    grays: list[float] = []
    for row in range(y1, y2):
        map_row = row - region.y
        for col in range(x1, x2):
            map_col = col - region.x
            if map_row < 0 or map_row >= side or map_col < 0 or map_col >= side:
                continue
            alpha = float(alpha_map.values[map_row * side + map_col])
            r, g, b = rgba[row, col, 0], rgba[row, col, 1], rgba[row, col, 2]
            gray = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
            alphas.append(alpha)
            grays.append(gray)

    n = len(alphas)
    if n == 0:
        return 0.0
    a = np.asarray(alphas, dtype=np.float64)
    g = np.asarray(grays, dtype=np.float64)
    mean_a = float(a.mean())
    mean_g = float(g.mean())
    cov = float((a * g).mean() - mean_a * mean_g)
    std_a = float(np.sqrt(max(0.0, (a * a).mean() - mean_a * mean_a)))
    std_g = float(np.sqrt(max(0.0, (g * g).mean() - mean_g * mean_g)))
    if std_a < 0.001 or std_g < 0.001:
        return 0.0
    return cov / (std_a * std_g)


def _compute_watermark_ncc_fast(
    rgba: np.ndarray,
    region: MarkRegion,
    alpha_map: WatermarkAlphaMap,
) -> float:
    """Vectorized NCC for full in-bounds square region (side x side)."""
    side = alpha_map.side
    h, w = rgba.shape[:2]
    x, y = region.x, region.y
    if x < 0 or y < 0 or x + side > w or y + side > h:
        return compute_watermark_ncc(rgba, w, h, region, alpha_map)

    patch = rgba[y : y + side, x : x + side, :3].astype(np.float64)
    gray = (0.299 * patch[:, :, 0] + 0.587 * patch[:, :, 1] + 0.114 * patch[:, :, 2]) / 255.0
    a = alpha_map.values.astype(np.float64).reshape(side, side)
    mean_a = float(a.mean())
    mean_g = float(gray.mean())
    cov = float((a * gray).mean() - mean_a * mean_g)
    std_a = float(np.sqrt(max(0.0, (a * a).mean() - mean_a * mean_a)))
    std_g = float(np.sqrt(max(0.0, (gray * gray).mean() - mean_g * mean_g)))
    if std_a < 0.001 or std_g < 0.001:
        return 0.0
    return cov / (std_a * std_g)


def scan_near_auto(
    rgba: np.ndarray,
    alpha_map: WatermarkAlphaMap,
    side: int,
    auto_x: int,
    auto_y: int,
    *,
    radius: int = 40,
    stride: int = 4,
    refine: int = REFINE_RADIUS,
) -> tuple[int, int, float]:
    """Scan only around Gemini/Flow auto margin (avoids mid-frame false positives)."""
    height, width = rgba.shape[:2]
    x_end = width - side
    y_end = height - side
    if x_end < 0 or y_end < 0:
        return -1, -1, float("-inf")

    x_lo = max(0, auto_x - radius)
    y_lo = max(0, auto_y - radius)
    x_hi = min(x_end, auto_x + radius)
    y_hi = min(y_end, auto_y + radius)
    if x_lo > x_hi or y_lo > y_hi:
        return -1, -1, float("-inf")

    best_x, best_y, best_ncc = auto_x, auto_y, float("-inf")
    for y in range(y_lo, y_hi + 1, stride):
        for x in range(x_lo, x_hi + 1, stride):
            ncc = _compute_watermark_ncc_fast(rgba, MarkRegion(x, y, side, side), alpha_map)
            if ncc > best_ncc:
                best_x, best_y, best_ncc = x, y, ncc

    if refine > 0 and best_ncc > float("-inf"):
        rx_lo = max(0, best_x - refine)
        ry_lo = max(0, best_y - refine)
        rx_hi = min(x_end, best_x + refine)
        ry_hi = min(y_end, best_y + refine)
        for y in range(ry_lo, ry_hi + 1):
            for x in range(rx_lo, rx_hi + 1):
                ncc = _compute_watermark_ncc_fast(
                    rgba, MarkRegion(x, y, side, side), alpha_map
                )
                if ncc > best_ncc:
                    best_x, best_y, best_ncc = x, y, ncc

    return best_x, best_y, best_ncc


def scan_bottom_right(
    rgba: np.ndarray,
    alpha_map: WatermarkAlphaMap,
    side: int,
    stride: int = SCAN_STRIDE,
    refine: int = REFINE_RADIUS,
) -> tuple[int, int, float]:
    """Original Erasio full bottom-right quadrant scan (unknown/public images)."""
    height, width = rgba.shape[:2]
    x_start = max(0, width // 2)
    y_start = max(0, height // 2)
    x_end = width - side
    y_end = height - side
    if x_end < x_start or y_end < y_start:
        return -1, -1, float("-inf")

    best_x, best_y, best_ncc = x_start, y_start, float("-inf")
    for y in range(y_start, y_end + 1, stride):
        for x in range(x_start, x_end + 1, stride):
            ncc = _compute_watermark_ncc_fast(rgba, MarkRegion(x, y, side, side), alpha_map)
            if ncc > best_ncc:
                best_x, best_y, best_ncc = x, y, ncc

    if refine > 0:
        rx_lo = max(0, best_x - refine)
        ry_lo = max(0, best_y - refine)
        rx_hi = min(x_end, best_x + refine)
        ry_hi = min(y_end, best_y + refine)
        for y in range(ry_lo, ry_hi + 1):
            for x in range(rx_lo, rx_hi + 1):
                ncc = _compute_watermark_ncc_fast(
                    rgba, MarkRegion(x, y, side, side), alpha_map
                )
                if ncc > best_ncc:
                    best_x, best_y, best_ncc = x, y, ncc

    return best_x, best_y, best_ncc


def reverse_blend(
    rgba: np.ndarray,
    region: MarkRegion,
    alpha_map: WatermarkAlphaMap,
    gain: float = 1.0,
    *,
    floor: float = ALPHA_FLOOR,
    ceiling: float = ALPHA_CEILING,
) -> None:
    """In-place: original = (blended − α·255) / (1 − α)."""
    height, width = rgba.shape[:2]
    start_x = max(0, region.x)
    start_y = max(0, region.y)
    end_x = min(width, region.x + region.w)
    end_y = min(height, region.y + region.h)
    if start_x >= end_x or start_y >= end_y:
        return

    side = alpha_map.side
    map_y0 = start_y - region.y
    map_x0 = start_x - region.x
    map_y1 = end_y - region.y
    map_x1 = end_x - region.x
    if map_y0 < 0 or map_x0 < 0 or map_y1 > side or map_x1 > side:
        for row in range(start_y, end_y):
            map_row = row - region.y
            for col in range(start_x, end_x):
                map_col = col - region.x
                if map_row < 0 or map_row >= side or map_col < 0 or map_col >= side:
                    continue
                alpha = float(alpha_map.values[map_row * side + map_col]) * gain
                if alpha < floor:
                    continue
                if alpha > ceiling:
                    alpha = ceiling
                divisor = 1.0 - alpha
                if divisor <= 0:
                    continue
                for ch in range(3):
                    corrected = (float(rgba[row, col, ch]) - alpha * OVERLAY_VALUE) / divisor
                    rgba[row, col, ch] = int(max(0, min(255, round(corrected))))
        return

    alpha = alpha_map.values.reshape(side, side)[map_y0:map_y1, map_x0:map_x1].astype(np.float64)
    alpha = alpha * gain
    alpha = np.clip(alpha, 0.0, ceiling)
    mask = alpha >= floor
    if not np.any(mask):
        return
    alpha = np.where(mask, alpha, 0.0)
    divisor = 1.0 - alpha
    safe = np.where(mask, divisor, 1.0)
    patch = rgba[start_y:end_y, start_x:end_x, :3].astype(np.float64)
    corrected = (patch - alpha[:, :, None] * OVERLAY_VALUE) / safe[:, :, None]
    corrected = np.clip(np.rint(corrected), 0, 255).astype(np.uint8)
    for ch in range(3):
        channel = rgba[start_y:end_y, start_x:end_x, ch]
        channel[mask] = corrected[:, :, ch][mask]
        rgba[start_y:end_y, start_x:end_x, ch] = channel


def detect_mark_erasio(
    rgba: np.ndarray,
    alpha48: WatermarkAlphaMap,
    alpha96: WatermarkAlphaMap,
    ncc_threshold: float,
    *,
    flow_known_watermark: bool = False,
) -> DetectedMark | None:
    """
    Detect sparkle mark.

    Flow/Gemini known stamps almost always sit at auto margin (32/64). Full
    quadrant scan often false-matches mid-frame texture and skips the real
    corner mark — prefer auto + near-auto refine when flow_known_watermark.
    """
    height, width = rgba.shape[:2]
    configs = (
        (LARGE_MARK_SIZE, 64, alpha96),
        (SMALL_MARK_SIZE, 32, alpha48),
    )
    best: DetectedMark | None = None
    best_auto: DetectedMark | None = None

    for side, margin, alpha_map in configs:
        if side >= width or side >= height:
            continue
        auto_x = width - margin - side
        auto_y = height - margin - side
        if auto_x < 0 or auto_y < 0:
            continue

        auto_region = MarkRegion(auto_x, auto_y, side, side)
        auto_ncc = _compute_watermark_ncc_fast(rgba, auto_region, alpha_map)
        auto_entry = DetectedMark(
            side=side,
            alpha_map=alpha_map,
            x=auto_x,
            y=auto_y,
            ncc=auto_ncc,
            auto_x=auto_x,
            auto_y=auto_y,
            method="auto",
        )
        if best_auto is None or auto_ncc > best_auto.ncc:
            best_auto = auto_entry

        entry = auto_entry

        if flow_known_watermark:
            # Only nudge around auto corner — never full-frame scan.
            if auto_ncc < NCC_GOOD:
                sx, sy, sncc = scan_near_auto(
                    rgba, alpha_map, side, auto_x, auto_y
                )
                # Accept near-scan only if better and not a weak false peak.
                if sncc > auto_ncc + 0.03 and sncc >= ncc_threshold:
                    entry = DetectedMark(
                        side=side,
                        alpha_map=alpha_map,
                        x=sx,
                        y=sy,
                        ncc=sncc,
                        auto_x=auto_x,
                        auto_y=auto_y,
                        method="scan",
                    )
        else:
            if auto_ncc < NCC_GOOD:
                sx, sy, sncc = scan_bottom_right(rgba, alpha_map, side)
                if sncc > auto_ncc:
                    entry = DetectedMark(
                        side=side,
                        alpha_map=alpha_map,
                        x=sx,
                        y=sy,
                        ncc=sncc,
                        auto_x=auto_x,
                        auto_y=auto_y,
                        method="scan",
                    )

        if best is None or entry.ncc > best.ncc:
            best = entry

    # Flow pipeline: stamp is always present at auto margin even when NCC is
    # weak after JPEG compression — force best auto placement.
    if flow_known_watermark:
        if best is not None and best.ncc >= ncc_threshold:
            return best
        if best_auto is not None:
            return best_auto
        return None

    if best is None or best.ncc < ncc_threshold:
        return None
    return best


def remove_image_watermark(
    data: bytes,
    *,
    flow_known_watermark: bool = True,
    ncc_threshold: float | None = None,
    gain: float | None = None,
    floor: float = ALPHA_FLOOR,
    ceiling: float = ALPHA_CEILING,
) -> dict[str, Any]:
    """
    Clean Gemini/Flow sparkle mark from raw image bytes.

    Returns dict: buffer, mime_type, cleaned, ncc (optional).
    """
    with Image.open(__import__("io").BytesIO(data)) as raw:
        input_format = (raw.format or "JPEG").upper()
        rgba_img = raw.convert("RGBA")
        rgba = np.array(rgba_img, dtype=np.uint8)

    alpha48, alpha96 = get_image_alpha_maps()
    threshold = (
        ncc_threshold
        if ncc_threshold is not None
        else (0.12 if flow_known_watermark else NCC_THRESHOLD)
    )
    best = detect_mark_erasio(
        rgba,
        alpha48,
        alpha96,
        threshold,
        flow_known_watermark=flow_known_watermark,
    )
    height, width = rgba.shape[:2]

    if best is None:
        logger.info(
            "erasio image skip %sx%s — no detect (ncc < %s)",
            width,
            height,
            threshold,
        )
        mime, buf = _encode_rgba(rgba, input_format)
        return {"buffer": buf, "mime_type": mime, "cleaned": False}

    drifted = best.x != best.auto_x or best.y != best.auto_y
    # Flow auto margin is trusted: use full gain even after slight near-auto nudge.
    if gain is not None:
        used_gain = gain
    elif flow_known_watermark and best.method == "auto":
        used_gain = 1.0
    elif drifted:
        used_gain = GAIN_DRIFT
    else:
        used_gain = 1.0

    reverse_blend(
        rgba,
        MarkRegion(best.x, best.y, best.side, best.side),
        best.alpha_map,
        used_gain,
        floor=floor,
        ceiling=ceiling,
    )
    logger.info(
        "erasio image remove %spx at (%s,%s) %sx%s method=%s ncc=%.3f gain=%.2f%s",
        best.side,
        best.x,
        best.y,
        width,
        height,
        best.method,
        best.ncc,
        used_gain,
        " (drift)" if drifted else "",
    )
    mime, buf = _encode_rgba(rgba, input_format)
    return {"buffer": buf, "mime_type": mime, "cleaned": True, "ncc": best.ncc}


def _encode_rgba(rgba: np.ndarray, input_format: str) -> tuple[str, bytes]:
    import io

    img = Image.fromarray(rgba, mode="RGBA")
    fmt = input_format.upper()
    out = io.BytesIO()
    if fmt in ("JPEG", "JPG"):
        img.convert("RGB").save(out, format="JPEG", quality=95)
        return "image/jpeg", out.getvalue()
    if fmt == "PNG":
        img.save(out, format="PNG")
        return "image/png", out.getvalue()
    if fmt == "WEBP":
        img.save(out, format="WEBP", quality=95)
        return "image/webp", out.getvalue()
    # Default: keep png for lossless after edit
    img.save(out, format="PNG")
    return "image/png", out.getvalue()


def clean_image_file_erasio(
    source: Path,
    output: Path,
    *,
    flow_known_watermark: bool = True,
) -> bool:
    """
    Clean image file with Erasio reverse-blend.

    Returns True if watermark was detected and removed; False if skipped
    (output is still written with original pixels when skipped).
    """
    data = Path(source).read_bytes()
    result = remove_image_watermark(data, flow_known_watermark=flow_known_watermark)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)

    # Prefer keeping original extension / format when writing cleaned buffer.
    suffix = Path(source).suffix.lower()
    buf = result["buffer"]
    if result["cleaned"]:
        # Re-encode to match destination suffix when possible
        if suffix in {".jpg", ".jpeg"} and result["mime_type"] != "image/jpeg":
            with Image.open(__import__("io").BytesIO(buf)) as im:
                out = __import__("io").BytesIO()
                im.convert("RGB").save(out, format="JPEG", quality=95)
                buf = out.getvalue()
        elif suffix == ".png" and result["mime_type"] != "image/png":
            with Image.open(__import__("io").BytesIO(buf)) as im:
                out = __import__("io").BytesIO()
                im.save(out, format="PNG")
                buf = out.getvalue()
        elif suffix == ".webp" and result["mime_type"] != "image/webp":
            with Image.open(__import__("io").BytesIO(buf)) as im:
                out = __import__("io").BytesIO()
                im.save(out, format="WEBP", quality=95)
                buf = out.getvalue()
    output.write_bytes(buf)
    return bool(result["cleaned"])
