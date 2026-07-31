from __future__ import annotations

from typing import TypedDict


class UnitBedSpec(TypedDict):
    bed_type: str
    count: int
    sort_order: int


UZORITA_STANDARD_BEDS: tuple[UnitBedSpec, ...] = (
    {"bed_type": "queen", "count": 1, "sort_order": 0},
    {"bed_type": "sofa", "count": 1, "sort_order": 1},
)

UZORITA_R4_BEDS: tuple[UnitBedSpec, ...] = (
    {"bed_type": "king", "count": 1, "sort_order": 0},
)

# Per-unit bed arrangement (Booking.com Standard Arrangement).
UZORITA_BEDS_BY_UNIT: dict[str, tuple[UnitBedSpec, ...]] = {
    "R1": UZORITA_STANDARD_BEDS,
    "R2": UZORITA_STANDARD_BEDS,
    "R3": UZORITA_STANDARD_BEDS,
    "R4": UZORITA_R4_BEDS,
    "R6": UZORITA_STANDARD_BEDS,
}

UZORITA_BED_SEED_UNIT_CODES: tuple[str, ...] = tuple(UZORITA_BEDS_BY_UNIT.keys())


def beds_for_unit(unit_code: str) -> tuple[UnitBedSpec, ...]:
    return UZORITA_BEDS_BY_UNIT.get(unit_code.upper(), UZORITA_STANDARD_BEDS)
