"""Parse identity fields from MRZ text (TD1/TD3)."""

from __future__ import annotations

import re
from datetime import date, datetime


def _clean_mrz_lines(mrz_text: str) -> list[str]:
    lines: list[str] = []
    for raw in (mrz_text or "").splitlines():
        line = re.sub(r"\s+", "", raw.upper())
        if line:
            lines.append(line)
    return lines


def parse_sex_from_mrz(mrz_text: str) -> str:
    """Return M or F when encoded in MRZ; empty string if unknown."""
    lines = _clean_mrz_lines(mrz_text)
    if not lines:
        return ""

    for line in lines:
        if len(line) >= 44:
            # TD3 passport line 2
            sex = line[20:21]
            if sex in {"M", "F"}:
                return sex
        if 26 <= len(line) <= 36:
            # TD1 ID card line 2 (OCR may truncate filler chars)
            sex = line[7:8]
            if sex in {"M", "F"}:
                return sex

    return ""


def _yymmdd_to_date(raw: str) -> date | None:
    digits = re.sub(r"[^0-9]", "", raw or "")
    if len(digits) != 6:
        return None
    try:
        parsed = datetime.strptime(digits, "%y%m%d").date()
    except ValueError:
        return None
    # MRZ uses 2-digit year; reject absurd futures far beyond expiry horizon.
    today = date.today()
    if parsed.year > today.year + 50:
        parsed = parsed.replace(year=parsed.year - 100)
    return parsed


def parse_document_number_from_mrz(mrz_text: str) -> str:
    """Best-effort document number from TD1 line 1 or TD3 line 2."""
    lines = _clean_mrz_lines(mrz_text)
    if not lines:
        return ""

    # TD3: prefer line 2 (44 chars) that is not the name line (P</I</A< …).
    td3_candidates = [
        line
        for line in lines
        if len(line) >= 44 and not line.startswith(("P<", "V<", "I<", "A<"))
    ]
    if td3_candidates:
        return re.sub(r"[^A-Z0-9]", "", td3_candidates[0][0:9])

    # TD1: first line document number at positions 5-13.
    line0 = lines[0]
    if len(line0) >= 14:
        return re.sub(r"[^A-Z0-9]", "", line0[5:14])
    return ""


def parse_date_of_birth_from_mrz(mrz_text: str) -> date | None:
    """Best-effort DOB from TD1 line 2 or TD3 line 2."""
    lines = _clean_mrz_lines(mrz_text)
    if not lines:
        return None

    for line in lines:
        if len(line) >= 44:
            return _yymmdd_to_date(line[13:19])
        if 26 <= len(line) <= 36 or len(line) == 30:
            # Prefer line that looks like TD1 line 2 (starts with digits)
            if line[0:6].isdigit() or line[0:1].isdigit():
                dob = _yymmdd_to_date(line[0:6])
                if dob is not None:
                    return dob
    return None


def normalize_residence_address(address: str) -> str:
    """eVisitor expects City, street — strip postal code prefix from city segment."""
    raw = (address or "").strip()
    if not raw or "," not in raw:
        return raw

    city_part, rest = raw.split(",", 1)
    city_part = city_part.strip()
    rest = rest.strip()
    match = re.match(r"^(\d{4,5})\s+(.+)$", city_part)
    if match:
        city_part = match.group(2).strip()
    return f"{city_part}, {rest}" if rest else city_part
