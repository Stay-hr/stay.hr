"""Pre-canonical image validation gate (ADR 0015)."""

from __future__ import annotations

import io
from dataclasses import dataclass

from django.conf import settings
from PIL import Image, UnidentifiedImageError

from apps.properties.unit_photos.exceptions import UnitPhotoValidationError
from apps.properties.unit_photos.storage import sha256_hex

ALLOWED_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})
FORMAT_TO_SUFFIX = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
FORMAT_TO_MIME = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}


@dataclass(frozen=True)
class ValidatedImage:
    data: bytes
    checksum: str
    width: int
    height: int
    format: str
    mime: str
    suffix: str


def _max_bytes() -> int:
    return int(getattr(settings, "UNIT_PHOTO_MAX_BYTES", 8 * 1024 * 1024))


def _max_edge() -> int:
    return int(getattr(settings, "UNIT_PHOTO_MAX_EDGE", 8000))


def _min_edge() -> int:
    return int(getattr(settings, "UNIT_PHOTO_MIN_EDGE", 100))


def validate_image_bytes(data: bytes, *, strip_exif: bool = True) -> ValidatedImage:
    """Validate image; never returns for invalid input — raises UnitPhotoValidationError."""
    if not data:
        raise UnitPhotoValidationError("Empty file.")
    max_bytes = _max_bytes()
    if len(data) > max_bytes:
        raise UnitPhotoValidationError(
            f"File too large ({len(data)} bytes; max {max_bytes})."
        )

    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            fmt = (img.format or "").upper()
            if fmt not in ALLOWED_FORMATS:
                raise UnitPhotoValidationError(
                    f"Unsupported format {fmt!r}; allowed: {sorted(ALLOWED_FORMATS)}."
                )
            width, height = img.size
            min_edge = _min_edge()
            max_edge = _max_edge()
            if width < min_edge or height < min_edge:
                raise UnitPhotoValidationError(
                    f"Image too small ({width}x{height}; min edge {min_edge})."
                )
            if width > max_edge or height > max_edge:
                raise UnitPhotoValidationError(
                    f"Image too large ({width}x{height}; max edge {max_edge})."
                )

            out = io.BytesIO()
            save_kwargs: dict = {}
            if strip_exif and fmt == "JPEG":
                # Re-encode without EXIF for privacy / size consistency
                rgb = img.convert("RGB") if img.mode not in ("RGB", "L") else img
                rgb.save(out, format="JPEG", quality=92, optimize=True)
                data = out.getvalue()
                width, height = rgb.size
            elif strip_exif and fmt == "PNG":
                rgb = img.convert("RGBA") if img.mode == "P" else img
                rgb.save(out, format="PNG", optimize=True)
                data = out.getvalue()
            else:
                # WEBP or keep as-is when not stripping
                if strip_exif and fmt == "WEBP":
                    img.save(out, format="WEBP", quality=90, method=4)
                    data = out.getvalue()
    except UnidentifiedImageError as exc:
        raise UnitPhotoValidationError("Unrecognized image file.") from exc
    except UnitPhotoValidationError:
        raise
    except OSError as exc:
        raise UnitPhotoValidationError(f"Cannot read image: {exc}") from exc

    return ValidatedImage(
        data=data,
        checksum=sha256_hex(data),
        width=width,
        height=height,
        format=fmt,
        mime=FORMAT_TO_MIME[fmt],
        suffix=FORMAT_TO_SUFFIX[fmt],
    )
