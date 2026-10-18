"""Bounded, source-preserving raster preparation for selected-page OCR."""

from __future__ import annotations

import hashlib
import io
import math
from pathlib import Path
from typing import Any

from .library import EvidenceError

OCR_RENDER_VERSION = "poppler-ocr-page-3200-region-v2"
OCR_MAX_DIMENSION = 3200
OCR_MAX_PIXELS = 12_000_000
OCR_CROP_MAX_DIMENSION = 2400
OCR_CROP_MAX_PIXELS = 5_000_000
OCR_MAX_IMAGE_BYTES = 12 * 1024 * 1024
OCR_SOURCE_MAX_PIXELS = 24_000_000
OCR_REGION_BORDER_PIXELS = 12


def normalized_region(region: dict | None) -> dict[str, float] | None:
    """Validate a page-relative rectangle without silently clipping its bounds."""
    if region is None:
        return None
    if not isinstance(region, dict) or set(region) != {"x", "y", "width", "height"}:
        raise EvidenceError("OCR region needs x, y, width and height")
    values: dict[str, float] = {}
    for key, value in region.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EvidenceError("OCR region coordinates must be finite numbers")
        number = float(value)
        if not math.isfinite(number) or not 0 <= number <= 1:
            raise EvidenceError("OCR region coordinates must be within 0 to 1")
        values[key] = number
    if (values["width"] <= 0 or values["height"] <= 0
            or values["x"] + values["width"] > 1
            or values["y"] + values["height"] > 1):
        raise EvidenceError("OCR region must have positive size within the page")
    return values


def _dimensions(image: Any) -> tuple[int, int]:
    width, height = image.size
    if width < 1 or height < 1 or width * height > OCR_SOURCE_MAX_PIXELS:
        raise EvidenceError("OCR source image exceeds the pixel bound")
    return width, height


def prepare_ocr_raster(content: bytes, *, mime_type: str, region: dict | None,
                       enlarge_small: bool) -> tuple[bytes, dict]:
    """Crop an actual rendered/source image; resize only small standalone images."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    region = normalized_region(region)
    try:
        with Image.open(io.BytesIO(content)) as opened:
            expected = {"image/png": "PNG", "image/jpeg": "JPEG"}.get(mime_type)
            if opened.format != expected:
                raise EvidenceError("OCR image bytes disagree with their format")
            _dimensions(opened)
            orientation = opened.getexif().get(274, 1)
            oriented = ImageOps.exif_transpose(opened)
            original_width, original_height = _dimensions(oriented)
            image = oriented.convert("RGB")
    except EvidenceError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise EvidenceError("OCR source image cannot be decoded") from exc
    box = [0, 0, original_width, original_height]
    if region is not None:
        box = [
            math.floor(region["x"] * original_width),
            math.floor(region["y"] * original_height),
            math.ceil((region["x"] + region["width"]) * original_width),
            math.ceil((region["y"] + region["height"]) * original_height),
        ]
        if box[2] - box[0] < 16 or box[3] - box[1] < 16:
            raise EvidenceError("OCR region is too small to inspect")
        image = image.crop(tuple(box))
    crop_width, crop_height = image.size
    scale = 1.0
    if enlarge_small and max(image.size) < 1600:
        scale = min(2.0, 2400 / max(image.size))
        image = image.resize(
            (max(1, round(crop_width * scale)), max(1, round(crop_height * scale))),
            Image.Resampling.LANCZOS,
        )
    if region is not None:
        image = ImageOps.expand(image, border=OCR_REGION_BORDER_PIXELS, fill="white")
    width, height = image.size
    dimension_bound = OCR_CROP_MAX_DIMENSION if region else OCR_MAX_DIMENSION
    pixel_bound = OCR_CROP_MAX_PIXELS if region else OCR_MAX_PIXELS
    if (width > dimension_bound or height > dimension_bound
            or width * height > pixel_bound):
        raise EvidenceError("Prepared OCR image exceeds the selected-page pixel bound")
    output = io.BytesIO()
    image.save(output, format="PNG")
    prepared = output.getvalue()
    if len(prepared) > OCR_MAX_IMAGE_BYTES:
        raise EvidenceError("Prepared OCR image exceeds the byte bound")
    return prepared, {
        "original_dimensions": [original_width, original_height],
        "coordinate_space": "Poppler-rendered page or EXIF-oriented source image",
        "exif_orientation_applied": orientation not in {None, 1},
        "crop_pixels": box, "prepared_dimensions": [width, height],
        "region": region, "scale": scale, "image_sha256": hashlib.sha256(prepared).hexdigest(),
        "white_border_pixels": OCR_REGION_BORDER_PIXELS if region is not None else 0,
    }


def traineddata_digest(path: Path) -> str:
    """Hash exact local English language data for OCR cache identity."""
    if not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
        raise EvidenceError("English OCR traineddata is unavailable or oversized")
    return hashlib.sha256(path.read_bytes()).hexdigest()
