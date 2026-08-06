"""Document expectations policy.

This module is the single source of truth for document-intake expectations.

Responsibilities:
- determine how many identity documents are expected
- determine which guest slots require documents
- determine which document slots are still missing

Non-responsibilities:
- guest matching
- audit decisions
- OCR processing
- WhatsApp orchestration
- eVisitor eligibility
"""

from __future__ import annotations

from dataclasses import dataclass

from apps.reservations.guest_slots import PLACEHOLDER_NAME, is_unfilled_guest
from apps.reservations.models import Guest, Reservation


@dataclass(frozen=True)
class MissingGuest:
    guest_id: int
    guest_name: str
    adult_ordinal: int


def expected_document_count(reservation: Reservation) -> int:
    """How many identity documents are expected during intake / web check-in.

    Sole place for count rules:
    1. ``expected_checkin_adults`` (check-in ops override) when set
    2. else OTA ``adults_count`` / ``persons_count`` / guest rows

    OTA importers must never write ``expected_checkin_adults``.
    """
    override = reservation.expected_checkin_adults
    if override is not None:
        return max(int(override), 0)
    adults = reservation.adults_count
    if adults is not None:
        return max(int(adults), 0)
    persons = reservation.persons_count
    if persons is not None and int(persons) > 0:
        return int(persons)
    guest_count = reservation.guests.count()
    if guest_count > 0:
        return guest_count
    return 1


def booked_adults_ceiling(reservation: Reservation) -> int:
    """OTA booked adults upper bound for check-in occupancy PATCH."""
    adults = reservation.adults_count
    if adults is not None:
        return max(int(adults), 0)
    persons = reservation.persons_count
    if persons is not None and int(persons) > 0:
        return int(persons)
    return max(reservation.guests.count(), 1)


def expected_document_slots(reservation: Reservation) -> list[Guest]:
    """Which guest slots require documents (primary first, capped at expected count).

    Does not judge OCR mapping quality.
    """
    count = expected_document_count(reservation)
    if count == 0:
        return []
    guests = list(reservation.guests.order_by("-is_primary", "pk"))
    return guests[:count]


def missing_document_slots(
    reservation: Reservation,
    *,
    persons: list[dict],
    matches: list[dict],
    images: list,
) -> list[MissingGuest]:
    """What document slots are still missing vs OCR/match state.

    Compares matches to expected_document_slots(); never re-decides expected count.
    """
    del images  # reserved for future side-level gaps; slot presence uses matches only
    if not isinstance(persons, list):
        persons = []
    if not isinstance(matches, list):
        matches = []

    slots = expected_document_slots(reservation)
    if not slots:
        return []

    match_by_person: dict[int, dict] = {}
    for match in matches:
        if not isinstance(match, dict):
            continue
        try:
            idx = int(match.get("person_index", -1))
        except (TypeError, ValueError):
            continue
        if idx >= 0:
            match_by_person[idx] = match

    matched_guest_ids: set[int] = set()
    for idx in range(len(persons)):
        match = match_by_person.get(idx)
        if not match or not match.get("auto_apply") or not match.get("guest_id"):
            continue
        matched_guest_ids.add(int(match["guest_id"]))

    missing: list[MissingGuest] = []
    for ordinal, guest in enumerate(slots, start=1):
        if guest.pk in matched_guest_ids:
            continue
        name = _guest_display_name(guest)
        if is_unfilled_guest(guest) or name == PLACEHOLDER_NAME:
            label = f"{PLACEHOLDER_NAME} ({ordinal}. odrasli)"
        else:
            label = name
        missing.append(
            MissingGuest(guest_id=guest.pk, guest_name=label, adult_ordinal=ordinal)
        )
    return missing


def _guest_display_name(guest: Guest) -> str:
    name = (guest.name or f"{guest.first_name} {guest.last_name}".strip()).strip()
    return name or f"Guest #{guest.pk}"
