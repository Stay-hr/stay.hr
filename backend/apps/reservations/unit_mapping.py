from __future__ import annotations

import re

from apps.properties.models import Property, Unit
from apps.tenants.models import Tenant

# Booking.com "Unit type" / Channex titles → stay.hr Unit.code (uzorita)
_BOOKING_TITLE_TO_UNIT_CODE: tuple[tuple[str, str], ...] = (
    ("deluxe king 1", "R1"),
    ("luxury room uzorita - r2", "R2"),
    ("luxury room uzorita - r3", "R3"),
    ("luxury room uzorita - r4", "R4"),
    ("standard king room r4", "R4"),
    ("standard king room", "R4"),
    ("deluxe double", "R6"),
    ("deluxe trokrevetna", "R3"),
    ("deluxe dvokrevetna", "R2"),
    ("deluxe kingsize", "R1"),
    ("deluxe king room", "R1"),
    ("deluxe triple room", "R3"),
    ("standard kingsize", "R6"),
    # D1 = ista fizička soba kao R6 (D1 ukinjen u bazi)
    ("luxury room uzorita - d1", "R6"),
    ("deluxe d1", "R6"),
)

_ROOM_CODE_RE = re.compile(r"\bR\s*-?\s*([1-6])\b", re.IGNORECASE)
_D1_ALIAS_RE = re.compile(r"\bD\s*-?\s*1\b", re.IGNORECASE)


def unit_code_from_room_name(room_name: str) -> str | None:
    text = (room_name or "").strip()
    if not text:
        return None
    match = _ROOM_CODE_RE.search(text)
    if match:
        return f"R{match.group(1)}"
    if _D1_ALIAS_RE.search(text):
        return "R6"
    lowered = text.lower()
    for needle, code in _BOOKING_TITLE_TO_UNIT_CODE:
        if needle in lowered:
            return code
    return None


def resolve_unit(
    *,
    tenant: Tenant,
    property: Property,
    room_name: str,
) -> Unit | None:
    code = unit_code_from_room_name(room_name)
    if not code:
        return None
    return Unit.objects.filter(
        tenant=tenant,
        property=property,
        code=code,
        is_active=True,
    ).first()
