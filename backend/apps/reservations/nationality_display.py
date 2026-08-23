"""ISO2 country code for reservation/guest display (flags in Hospira)."""

from __future__ import annotations

from apps.core.countries import iso2_to_iso3, iso3_to_iso2, is_known_iso2
from apps.reservations.models import Guest, Reservation

# Truncated / invalid ISO2 codes that should fall through to document ISO3.
_INVALID_ISO2 = frozenset({"PO"})


def normalize_country_iso2(raw: str) -> str:
    value = (raw or "").strip().upper()
    if not value:
        return ""
    if len(value) == 3:
        return iso3_to_iso2(value)
    if len(value) == 2:
        if value in _INVALID_ISO2:
            return ""
        if is_known_iso2(value):
            return value
    return ""


def guest_nationality_iso2(guest: Guest) -> str:
    for field in (guest.nationality, guest.document_country_iso2):
        iso2 = normalize_country_iso2(str(field or ""))
        if iso2:
            return iso2
    iso2_from_iso3 = iso3_to_iso2(str(guest.document_country_iso3 or ""))
    if iso2_from_iso3:
        return iso2_from_iso3
    return ""


def reservation_nationality_iso2(reservation: Reservation) -> str:
    primary = next((g for g in reservation.guests.all() if g.is_primary), None)
    if primary:
        iso2 = guest_nationality_iso2(primary)
        if iso2:
            return iso2
    for guest in reservation.guests.all():
        iso2 = guest_nationality_iso2(guest)
        if iso2:
            return iso2
    return normalize_country_iso2(reservation.booker_country)


def apply_reservation_country_to_guest_if_empty(
    guest: Guest,
    *,
    reservation: Reservation | None = None,
) -> list[str]:
    """Fill guest nationality fields from reservation when the guest has none."""
    if guest_nationality_iso2(guest):
        return []
    reservation = reservation or guest.reservation
    iso2 = reservation_nationality_iso2(reservation)
    if not iso2:
        return []
    changed: list[str] = []
    if not (guest.nationality or "").strip():
        guest.nationality = iso2
        changed.append("nationality")
    if not (guest.document_country_iso2 or "").strip():
        guest.document_country_iso2 = iso2
        changed.append("document_country_iso2")
    return changed


__all__ = [
    "apply_reservation_country_to_guest_if_empty",
    "guest_nationality_iso2",
    "iso2_to_iso3",
    "iso3_to_iso2",
    "normalize_country_iso2",
    "reservation_nationality_iso2",
]
