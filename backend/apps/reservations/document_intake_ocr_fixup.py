"""Deterministic corrections after LLM document OCR (side detection, image indices)."""

from __future__ import annotations

import re
from typing import Any

_BACK_OCR_MARKERS = frozenset(
    {
        "PESEL",
        "PERSONAL NUMBER",
        "ISSUING AUTHORITY",
        "ORGAN WYDAJ",
        "DATE OF ISSUE",
        "DATA WYDANIA",
        "PARENTS",
        "RODOWE",
        "RODZIC",
        "MIEJSCE URODZENIA",
        "PLACE OF BIRTH",
    }
)

_FRONT_OCR_MARKERS = frozenset(
    {
        "PODPIS",
        "SIGNATURE",
        "IMIONA/",
        "GIVEN NAMES",
        "NAZWISKO/",
        "SURNAME",
        "IDENTITY CARD",
        "DOWOD OSOBISTY",
    }
)


def _ocr_text_upper(item: dict) -> str:
    return str(item.get("ocr_text") or "").upper()


def _has_mrz_lines(item: dict) -> bool:
    lines = item.get("mrz_lines") or []
    if not isinstance(lines, list):
        return bool(str(lines or "").strip())
    return any(str(line).strip() for line in lines)


def infer_image_side(item: dict) -> str:
    """Return front|back|passport|unknown for a single images[] OCR row."""
    if not isinstance(item, dict):
        return "unknown"

    side = str(item.get("side") or "").strip().lower()
    if side in {"front", "back", "passport"}:
        inferred = side
    else:
        inferred = "unknown"

    text = _ocr_text_upper(item)
    has_mrz = _has_mrz_lines(item)

    back_score = sum(1 for marker in _BACK_OCR_MARKERS if marker in text)
    front_score = sum(1 for marker in _FRONT_OCR_MARKERS if marker in text)

    if has_mrz and back_score >= 1:
        return "back"
    if back_score >= 2 and back_score > front_score:
        return "back"
    if inferred == "front" and has_mrz and "PESEL" in text:
        return "back"
    if inferred == "unknown" and front_score >= 2:
        return "front"
    if inferred in {"front", "back", "passport"}:
        return inferred
    return "unknown"


def _image_meta_at(images: list, index: Any) -> dict | None:
    try:
        idx = int(index)
    except (TypeError, ValueError):
        return None
    if idx < 0 or idx >= len(images):
        return None
    item = images[idx]
    return item if isinstance(item, dict) else None


_NAME_TOKEN_RE = re.compile(r"^[A-Za-zÀ-žÄÖÜẞ][A-Za-zÀ-žÄÖÜẞ'\-]{1,39}$")
_FIELD_LABEL_MARKERS = frozenset(
    {
        "VORNAMEN",
        "GIVEN NAMES",
        "GEBURTSNAME",
        "NAME AT BIRTH",
        "NOM DE NAISSANCE",
        "PERSONALAUSWEIS",
        "BUNDESREPUBLIK",
        "DEUTSCH",
    }
)


def _looks_like_name_token(line: str) -> bool:
    token = (line or "").strip()
    if not token or not _NAME_TOKEN_RE.match(token):
        return False
    upper = token.upper()
    return not any(marker in upper for marker in _FIELD_LABEL_MARKERS)


def _surname_from_mrz_lines(mrz_lines: list | None) -> str | None:
    """Extract surname from TD1 / German ID MRZ name line (last line with letters)."""
    if not isinstance(mrz_lines, list):
        return None
    for line in reversed(mrz_lines):
        cleaned = re.sub(r"\s+", "", str(line or "").upper())
        if not cleaned or cleaned[0].isdigit() or cleaned.startswith("IDD"):
            continue
        match = re.match(r"^([A-ZÀ-žÄÖÜẞ][A-ZÀ-žÄÖÜẞ'\-]*)(?:<<|<)", cleaned)
        if match:
            surname = match.group(1).strip()
            if len(surname) >= 2:
                return surname
        if "<<" in cleaned:
            before = cleaned.split("<<", 1)[0]
            if before and len(before) >= 2 and before.isalpha():
                return before
    return None


def _collect_mrz_lines_for_person(person: dict, images: list[dict]) -> list[str]:
    lines: list[str] = []
    raw = person.get("mrz_lines") or []
    if isinstance(raw, list):
        lines.extend(str(line) for line in raw if str(line).strip())
    for key in ("back_image_index", "front_image_index"):
        meta = _image_meta_at(images, person.get(key))
        if meta is None:
            continue
        img_lines = meta.get("mrz_lines") or []
        if isinstance(img_lines, list):
            lines.extend(str(line) for line in img_lines if str(line).strip())
    return lines


def _surname_from_german_id_front_ocr(text: str) -> str | None:
    """Field [a] surname on German Personalausweis (not Geburtsname [b])."""
    raw = (text or "").strip()
    if not raw:
        return None
    upper = raw.upper()
    if "PERSONALAUSWEIS" not in upper and "BUNDESREPUBLIK DEUTSCHLAND" not in upper:
        return None

    lines = [line.strip() for line in raw.splitlines() if line.strip()]

    for index, line in enumerate(lines):
        if re.search(r"\[a\].*(NAME/SURNAME|NAZWA/NOM)", line, re.IGNORECASE):
            for candidate_line in lines[index + 1 : index + 4]:
                if _looks_like_name_token(candidate_line):
                    return candidate_line
            break

    start = 0
    for index, line in enumerate(lines):
        if "PERSONALAUSWEIS" in line.upper():
            start = index + 1
            break

    surname_candidates: list[str] = []
    for line in lines[start:]:
        lowered = line.lower()
        if re.match(r"\[b\]", line, re.IGNORECASE) or "geburtsname" in lowered:
            break
        if re.match(r"vornamen|given names|prénoms", line, re.IGNORECASE):
            break
        if _looks_like_name_token(line):
            surname_candidates.append(line)

    if surname_candidates:
        return surname_candidates[0]
    return None


def _normalize_surname_token(value: str) -> str:
    from apps.reservations.booking_xls_import import _normalize_guest_name_key

    return _normalize_guest_name_key(value)


def _correct_person_surnames(person: dict, images: list[dict]) -> None:
    """Fix German ID OCR that used Geburtsname or given-name line as surname."""
    current = str(person.get("surnames") or "").strip()
    mrz_surname = _surname_from_mrz_lines(_collect_mrz_lines_for_person(person, images))

    front_meta = _image_meta_at(images, person.get("front_image_index"))
    front_text = str((front_meta or {}).get("ocr_text") or "")
    german_surname = _surname_from_german_id_front_ocr(front_text)

    preferred = mrz_surname or german_surname
    if not preferred:
        return

    if not current:
        person["surnames"] = preferred
        return

    current_key = _normalize_surname_token(current)
    preferred_key = _normalize_surname_token(preferred)
    if current_key == preferred_key:
        return

    # MRZ / field [a] wins over birth name or misread given-name line.
    if mrz_surname and _normalize_surname_token(mrz_surname) == preferred_key:
        person["surnames"] = mrz_surname
        return
    if german_surname and _normalize_surname_token(german_surname) == preferred_key:
        person["surnames"] = german_surname


def _reconcile_person_image_indices(person: dict, images: list[dict]) -> None:
    """Align front_image_index / back_image_index with corrected images[].side."""
    front_idx = person.get("front_image_index")
    back_idx = person.get("back_image_index")

    front_meta = _image_meta_at(images, front_idx)
    back_meta = _image_meta_at(images, back_idx)

    if front_meta is not None and front_meta.get("side") == "back" and back_meta is None:
        person["back_image_index"] = front_idx
        person["front_image_index"] = None
        return

    if back_meta is not None and back_meta.get("side") == "front" and front_meta is None:
        person["front_image_index"] = back_idx
        person["back_image_index"] = None
        return

    if front_meta is not None and front_meta.get("side") == "back" and back_meta is not None:
        if back_meta.get("side") == "front":
            person["front_image_index"] = back_idx
            person["back_image_index"] = front_idx

    doc_type = str(person.get("document_type") or "").lower()
    if doc_type == "passport":
        if person.get("front_image_index") is None and person.get("back_image_index") is not None:
            person["front_image_index"] = person["back_image_index"]
            person["back_image_index"] = None


def fixup_document_ocr_result(ocr_result: dict) -> dict:
    """Correct misclassified sides and person image indices before match/apply."""
    if not isinstance(ocr_result, dict):
        return ocr_result

    images_raw = ocr_result.get("images") or []
    images: list[dict] = [item for item in images_raw if isinstance(item, dict)]

    for item in images:
        item["side"] = infer_image_side(item)

    persons_raw = ocr_result.get("persons") or []
    persons: list[dict] = [item for item in persons_raw if isinstance(item, dict)]

    for person in persons:
        _reconcile_person_image_indices(person, images)
        _correct_person_surnames(person, images)

    ocr_result["images"] = images
    ocr_result["persons"] = persons
    return ocr_result


def normalize_document_number(value: str) -> str:
    """Canonical document number for identity compare / storage.

    Sole normalizer for intake identity — use everywhere (match, collision,
    migration, API validation). ``AB-123456``, ``ab 123456`` → ``AB123456``.
    """
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
