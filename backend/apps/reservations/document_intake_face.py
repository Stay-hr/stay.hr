"""Face crop from document front photos."""

from __future__ import annotations

import io
import logging
from typing import Any

from django.core.files.base import ContentFile
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

OUTPUT_SIZE = 256
# Reject Haar boxes whose center sits too far below the card (table / wood grain).
# Czech TD1 portraits on upright phone photos often sit at cy ≈ 0.72–0.74 (res #861).
_MAX_FACE_CENTER_Y_RATIO = 0.78
# Generic bbox copied from LLM prompt examples — ignore when detected.
_LLM_PLACEHOLDER_BBOXES = frozenset(
    {
        (0.1, 0.15, 0.25, 0.35),
        (0.05, 0.12, 0.35, 0.45),
        (0.05, 0.12, 0.38, 0.48),
        (0.1, 0.2, 0.3, 0.4),
    }
)


def _load_rgb_image(image_path: str) -> Image.Image:
    """Open image and apply EXIF orientation so Haar sees the display-correct pixels."""
    with Image.open(image_path) as im:
        # copy(): exif_transpose may return the same object; closing the file must not
        # invalidate pixels used by OpenCV.
        return ImageOps.exif_transpose(im).convert("RGB").copy()


def _coerce_bbox_dict(bbox: Any) -> dict[str, Any] | None:
    if isinstance(bbox, dict):
        return bbox
    if isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
        return {"x": bbox[0], "y": bbox[1], "w": bbox[2], "h": bbox[3]}
    return None


def _normalize_bbox_dict(bbox: dict[str, Any]) -> tuple[float, float, float, float] | None:
    try:
        x = float(bbox["x"])
        y = float(bbox["y"])
        w = float(bbox["w"])
        h = float(bbox["h"])
    except (KeyError, TypeError, ValueError):
        return None
    if w <= 0 or h <= 0 or x < 0 or y < 0 or x > 1 or y > 1:
        return None
    return (x, y, w, h)


def _is_placeholder_llm_bbox(bbox: dict[str, Any] | list | tuple | None) -> bool:
    normalized = _normalize_bbox_dict(_coerce_bbox_dict(bbox)) if bbox else None
    if normalized is None:
        return True
    rounded = tuple(round(v, 2) for v in normalized)
    return rounded in _LLM_PLACEHOLDER_BBOXES


def _european_id_portrait_bbox(w: int, h: int) -> tuple[int, int, int, int]:
    """Fallback: portrait strip on the left of EU ID cards."""
    x1 = int(0.02 * w)
    y1 = int(0.12 * h)
    x2 = int(0.34 * w)
    y2 = int(0.88 * h)
    return x1, y1, x2, y2


def _max_face_side(*, image_w: int, image_h: int) -> int:
    """Upper bound for Haar face box — landscape ID photos need a wider portrait strip."""
    min_side = min(image_w, image_h)
    if image_h > image_w:
        # Portrait snapshot of a landscape ID — real face is ~25–32% of width; header
        # holograms often trigger oversized Haar boxes (res #165).
        max_side = int(min_side * 0.38)
    else:
        max_side = int(min_side * 0.42)
    if image_w > image_h * 1.25:
        max_side = max(max_side, int(min_side * 0.52))
    return max_side


def _face_box_is_plausible(
    x: int,
    y: int,
    fw: int,
    fh: int,
    *,
    image_w: int,
    image_h: int,
) -> bool:
    """Reject Haar boxes that sit on table edges / wood grain below the card."""
    cy_ratio = (y + fh / 2) / image_h
    if cy_ratio > _MAX_FACE_CENTER_Y_RATIO:
        return False

    min_side = int(min(image_w, image_h) * 0.08)
    max_side = _max_face_side(image_w=image_w, image_h=image_h)
    if fw < min_side or fh < min_side:
        return False
    if fw > max_side or fh > max_side:
        return False
    aspect = fw / fh if fh else 0
    return 0.65 <= aspect <= 1.45


def _score_face_box(
    x: int,
    y: int,
    fw: int,
    fh: int,
    *,
    image_w: int,
    image_h: int,
) -> float:
    cx = x + fw / 2
    cy = y + fh / 2
    cx_ratio = cx / image_w
    cy_ratio = cy / image_h

    # EU ID portrait sits in the left strip; holograms often trigger false positives mid-card.
    if cx_ratio < 0.30:
        left_bonus = 0.35
    elif cx_ratio < 0.40:
        left_bonus = 0.20
    elif cx_ratio < 0.55:
        left_bonus = 0.05
    else:
        left_bonus = -0.40

    # Portrait center is usually below mid-height on TD1 cards.
    vertical_center = abs(cy_ratio - 0.58)
    vertical_bonus = max(0.0, 0.15 - vertical_center)

    # EU header / eagle / star-circle holograms sit in the top ~35% of the card.
    header_penalty = -0.45 if cy_ratio < 0.35 else 0.0

    # Portrait snapshot of a landscape ID — biodata face usually sits mid-card vertically.
    portrait_id_bonus = 0.0
    if image_h > image_w and 0.36 <= cy_ratio <= 0.55:
        portrait_id_bonus = 0.22

    area_score = (fw * fh) / (image_w * image_h) * 2.5
    tiny_penalty = -0.25 if max(fw, fh) < min(image_w, image_h) * 0.14 else 0.0

    return (
        area_score
        + left_bonus
        + vertical_bonus
        + header_penalty
        + portrait_id_bonus
        + tiny_penalty
    )


def _select_best_face(
    faces: list[tuple[int, int, int, int]],
    *,
    image_w: int,
    image_h: int,
) -> tuple[int, int, int, int] | None:
    """Pick the most likely portrait face on an ID card front."""
    if not faces:
        return None

    candidates: list[tuple[float, tuple[int, int, int, int]]] = []

    for x, y, fw, fh in faces:
        if not _face_box_is_plausible(x, y, fw, fh, image_w=image_w, image_h=image_h):
            continue

        score = _score_face_box(x, y, fw, fh, image_w=image_w, image_h=image_h)
        candidates.append((score, (x, y, fw, fh)))

    if not candidates:
        return None

    # WhatsApp passport photos often show two open pages; Haar false positives on the
    # eagle/header sit above the real biodata portrait on the left strip.
    open_book_photo = image_h > image_w
    if open_book_photo:
        left_portraits = [
            box
            for _, box in candidates
            if (box[0] + box[2] / 2) / image_w < 0.30
        ]
        if len(left_portraits) >= 2:
            return max(left_portraits, key=lambda b: b[1])

    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _detect_faces_in_bgr(img) -> tuple[int, int, int, int] | None:
    """Run Haar face detection on a BGR numpy array; return best box or None."""
    import cv2

    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)

    cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    cascade = cv2.CascadeClassifier(cascade_path)
    faces_raw = cascade.detectMultiScale(
        gray,
        scaleFactor=1.05,
        minNeighbors=4,
        minSize=(int(min(w, h) * 0.08), int(min(w, h) * 0.08)),
    )
    faces = [tuple(int(v) for v in face) for face in faces_raw]
    return _select_best_face(faces, image_w=w, image_h=h)


def detect_face_bbox_pixels(image_path: str) -> tuple[int, int, int, int] | None:
    """Detect face bounding box in pixel coordinates using OpenCV."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        logger.debug("opencv not installed; skipping face detection")
        return None

    try:
        im = _load_rgb_image(image_path)
        img = cv2.cvtColor(np.array(im), cv2.COLOR_RGB2BGR)
        return _detect_faces_in_bgr(img)
    except Exception:
        logger.exception("opencv face detection failed", extra={"path": image_path})
        return None


def _detect_faces_in_bgr_all(img) -> list[tuple[int, int, int, int]]:
    """Return all plausible Haar face boxes in a BGR image."""
    import cv2

    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)

    cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    cascade = cv2.CascadeClassifier(cascade_path)
    faces_raw = cascade.detectMultiScale(
        gray,
        scaleFactor=1.05,
        minNeighbors=4,
        minSize=(int(min(w, h) * 0.08), int(min(w, h) * 0.08)),
    )
    faces = [tuple(int(v) for v in face) for face in faces_raw]
    return [
        box
        for box in faces
        if _face_box_is_plausible(*box, image_w=w, image_h=h)
    ]


def _detect_face_with_portrait_rotation(
    im: Image.Image,
) -> tuple[tuple[int, int, int, int] | None, int]:
    """Try face detection on image and ±90° rotations for sideways ID snapshots."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None, 0

    w, h = im.size
    angles = (0,)
    # Portrait phone photo of a landscape ID, or landscape photo of a portrait ID.
    if h > w * 1.15 or w > h * 1.15:
        angles = (0, 90, -90)

    per_angle: dict[int, tuple[float, tuple[int, int, int, int]]] = {}
    for angle in angles:
        working = im if angle == 0 else im.rotate(angle, expand=True)
        bgr = cv2.cvtColor(np.array(working.convert("RGB")), cv2.COLOR_RGB2BGR)
        iw = bgr.shape[1]
        ih = bgr.shape[0]
        faces = _detect_faces_in_bgr_all(bgr)
        face_px = _select_best_face(faces, image_w=iw, image_h=ih)
        if face_px is None:
            continue
        x, y, fw, fh = face_px
        score = _score_face_box(x, y, fw, fh, image_w=iw, image_h=ih)
        prev = per_angle.get(angle)
        if prev is None or score > prev[0]:
            per_angle[angle] = (score, face_px)

    if not per_angle:
        return None, 0

    best_angle = max(per_angle, key=lambda angle: per_angle[angle][0])
    best_score, best_face = per_angle[best_angle]

    # Rotated snapshots can yield oversized false positives (signature strip, hologram).
    if best_angle != 0 and 0 in per_angle:
        zero_score, zero_face = per_angle[0]
        _, _, rfw, rfh = best_face
        _, _, zfw, zfh = zero_face
        rot_area = rfw * rfh
        zero_area = max(zfw * zfh, 1)
        if rot_area > zero_area * 1.8 and zero_score >= best_score - 0.15:
            return zero_face, 0

    return best_face, best_angle


def _square_crop_around_face(
    im: Image.Image,
    *,
    x: int,
    y: int,
    fw: int,
    fh: int,
    padding: float = 0.35,
) -> Image.Image:
    """Expand face box to a square crop centered on the face."""
    w, h = im.size
    cx = x + fw / 2
    cy = y + fh / 2
    side = int(max(fw, fh) * (1 + padding))
    side = max(side, 80)

    x1 = int(cx - side / 2)
    y1 = int(cy - side / 2)
    x2 = x1 + side
    y2 = y1 + side

    # Shift crop to stay inside image bounds while keeping size when possible.
    if x1 < 0:
        x2 -= x1
        x1 = 0
    if y1 < 0:
        y2 -= y1
        y1 = 0
    if x2 > w:
        shift = x2 - w
        x1 = max(0, x1 - shift)
        x2 = w
    if y2 > h:
        shift = y2 - h
        y1 = max(0, y1 - shift)
        y2 = h

    x1 = max(0, x1)
    y1 = max(0, y1)
    x2 = min(w, x2)
    y2 = min(h, y2)
    if x2 <= x1 or y2 <= y1:
        return im.crop((x, y, min(w, x + fw), min(h, y + fh)))

    crop = im.crop((x1, y1, x2, y2))
    cw, ch = crop.size
    if cw < 40 or ch < 40:
        return crop

    side = min(cw, ch)
    if cw > ch:
        trim = (cw - side) // 2
        crop = crop.crop((trim, 0, trim + side, side))
    elif ch > cw:
        top_trim = max(0, min(int((ch - side) * 0.08), ch - side))
        crop = crop.crop((0, top_trim, side, top_trim + side))

    return crop.resize((OUTPUT_SIZE, OUTPUT_SIZE), Image.Resampling.LANCZOS)


def _crop_from_normalized_bbox(
    im: Image.Image,
    bbox: dict[str, Any],
) -> Image.Image | None:
    normalized = _normalize_bbox_dict(bbox)
    if normalized is None:
        return None
    w, h = im.size
    x, y, bw, bh = normalized
    x1 = max(0, int(x * w))
    y1 = max(0, int(y * h))
    x2 = min(w, int((x + bw) * w))
    y2 = min(h, int((y + bh) * h))
    if x2 <= x1 or y2 <= y1:
        return None
    return _square_crop_around_face(im, x=x1, y=y1, fw=x2 - x1, fh=y2 - y1)


def _crop_from_eu_fallback(im: Image.Image) -> Image.Image:
    w, h = im.size
    x1, y1, x2, y2 = _european_id_portrait_bbox(w, h)
    strip = im.crop((x1, y1, x2, y2))
    side = min(strip.size)
    top = max(0, min(int((strip.size[1] - side) * 0.05), strip.size[1] - side))
    square = strip.crop((0, top, side, top + side))
    return square.resize((OUTPUT_SIZE, OUTPUT_SIZE), Image.Resampling.LANCZOS)


def crop_face_jpeg(
    image_path: str,
    bbox: dict[str, Any] | list | tuple | None = None,
) -> ContentFile | None:
    """Crop portrait from document image. Prefers OpenCV face detection over LLM bbox."""
    try:
        im = _load_rgb_image(image_path)
        crop: Image.Image | None = None

        bbox_dict = _coerce_bbox_dict(bbox)
        face_px, rotate_angle = _detect_face_with_portrait_rotation(im)
        working = im if rotate_angle == 0 else im.rotate(rotate_angle, expand=True)
        if face_px is not None:
            x, y, fw, fh = face_px
            crop = _square_crop_around_face(working, x=x, y=y, fw=fw, fh=fh)
        elif bbox_dict and not _is_placeholder_llm_bbox(bbox_dict):
            crop = _crop_from_normalized_bbox(working, bbox_dict)
        else:
            crop = _crop_from_eu_fallback(working)

        if crop is None:
            return None

        buf = io.BytesIO()
        crop.save(buf, format="JPEG", quality=92)
        buf.seek(0)
        return ContentFile(buf.read(), name="face.jpg")
    except Exception:
        logger.exception("face crop failed", extra={"path": image_path})
        return None
