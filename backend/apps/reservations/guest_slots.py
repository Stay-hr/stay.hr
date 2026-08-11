from __future__ import annotations

from apps.integrations.evisitor.summary import evisitor_status_for_guest
from apps.reservations.models import EvisitorGuestStatus, Guest, Reservation
from apps.reservations.nationality_display import reservation_nationality_iso2
from apps.tenants.models import Tenant

PLACEHOLDER_FIRST = "Novi"
PLACEHOLDER_LAST = "gost"
PLACEHOLDER_NAME = "Novi gost"


def _placeholder_guest_kwargs(*, reservation: Reservation) -> dict[str, str | bool]:
    kwargs: dict[str, str | bool] = {
        "first_name": PLACEHOLDER_FIRST,
        "last_name": PLACEHOLDER_LAST,
        "name": PLACEHOLDER_NAME,
        "is_primary": False,
    }
    iso2 = reservation_nationality_iso2(reservation)
    if iso2:
        kwargs["nationality"] = iso2
        kwargs["document_country_iso2"] = iso2
    return kwargs


def target_adult_guest_count(*, adults_count: int | None, existing_count: int) -> int:
    """Minimum guest records that should exist for adult slots."""
    base = adults_count if adults_count and adults_count > 0 else existing_count
    return max(base, existing_count, 1)


def target_intake_guest_count(
    *,
    reservation: Reservation,
    min_count: int,
) -> int:
    """Guest records needed for general intake/occupancy (persons on reservation or OCR batch).

    Not used for document-intake slot creation — see target_document_guest_count().
    """
    existing_count = reservation.guests.count()
    adults = reservation.adults_count if reservation.adults_count and reservation.adults_count > 0 else 0
    persons = reservation.persons_count if reservation.persons_count and reservation.persons_count > 0 else 0
    floor = max(adults, persons, min_count, 1)
    return max(floor, existing_count)


def target_document_guest_count(
    *,
    reservation: Reservation,
    min_count: int,
) -> int:
    """Guest records needed for document intake only (policy count, not persons_count).

    ``expected_document_count`` (``expected_checkin_adults`` or OTA adults) is the
    ceiling. OCR ``min_count`` must not inflate guest rows above that policy — excess
    persons are a mismatch signal for the client, not an implicit slot create.
    """
    from apps.reservations.document_expectations import expected_document_count

    existing_count = reservation.guests.count()
    document_floor = expected_document_count(reservation)
    if document_floor <= 0:
        # No policy ceiling (e.g. adults_count=0): allow OCR min_count as before.
        floor = max(int(min_count or 0), 0)
        if floor == 0:
            return existing_count
        return max(floor, existing_count, 1)
    # Cap OCR min_count at the check-in document policy ceiling.
    capped_min = min(max(int(min_count or 0), 0), document_floor)
    floor = max(document_floor, capped_min)
    return max(floor, existing_count, 1)


def _ensure_primary_booker_guest(*, tenant: Tenant, reservation: Reservation) -> None:
    """Mark booker as primary guest when import/sync left no primary slot."""
    if reservation.guests.filter(is_primary=True).exists():
        return
    booker_name = (reservation.booker_name or "").strip()
    if not booker_name:
        return
    guest = reservation.guests.order_by("id").first()
    if guest is None:
        parts = booker_name.split(None, 1)
        first_name = parts[0] if parts else booker_name
        last_name = parts[1] if len(parts) > 1 else ""
        Guest.objects.create(
            tenant=tenant,
            reservation=reservation,
            first_name=first_name,
            last_name=last_name,
            name=booker_name,
            is_primary=True,
        )
        return
    guest.is_primary = True
    if not (guest.name or "").strip():
        guest.name = booker_name
    guest.save(update_fields=["is_primary", "name", "updated_at"])


def ensure_guest_slots_for_intake(
    *,
    tenant: Tenant,
    reservation: Reservation,
    min_count: int,
) -> int:
    """Ensure enough guest slots for document intake OCR batch. Returns created."""
    if reservation.status in {Reservation.Status.CANCELED, Reservation.Status.NO_SHOW}:
        return 0

    existing_count = reservation.guests.count()
    target = target_document_guest_count(reservation=reservation, min_count=min_count)
    created = 0
    for _ in range(target - existing_count):
        Guest.objects.create(
            tenant=tenant,
            reservation=reservation,
            **_placeholder_guest_kwargs(reservation=reservation),
        )
        created += 1
    if created and hasattr(reservation, "_prefetched_objects_cache"):
        reservation._prefetched_objects_cache.pop("guests", None)
    return created


def ensure_adult_guest_slots(
    *,
    tenant: Tenant,
    reservation: Reservation,
    adults_count: int | None,
) -> int:
    """Add placeholder guests until count matches adults_count. Returns number created."""
    if reservation.status in {Reservation.Status.CANCELED, Reservation.Status.NO_SHOW}:
        return 0

    _ensure_primary_booker_guest(tenant=tenant, reservation=reservation)

    existing_count = reservation.guests.count()
    target = target_adult_guest_count(
        adults_count=adults_count,
        existing_count=existing_count,
    )
    created = 0
    for _ in range(target - existing_count):
        Guest.objects.create(
            tenant=tenant,
            reservation=reservation,
            **_placeholder_guest_kwargs(reservation=reservation),
        )
        created += 1
    return created


def is_unfilled_guest(guest: Guest) -> bool:
    """True when guest lacks identity data required for eVisitor check-in."""
    has_document = bool((guest.document_number or "").strip())
    has_core_identity = (
        guest.date_of_birth is not None
        and bool((guest.nationality or "").strip())
        and bool((guest.sex or "").strip())
    )
    return not has_document and not has_core_identity


def is_removable_empty_guest(guest: Guest) -> bool:
    """Secondary guest that was never submitted to eVisitor and has no identity data."""
    if guest.is_primary:
        return False
    status = evisitor_status_for_guest(guest)
    if status != EvisitorGuestStatus.NOT_SENT:
        return False
    return is_unfilled_guest(guest)


def guests_for_checkout(reservation: Reservation) -> list[Guest]:
    """Guests that remain after removing unfilled secondary slots (simulation, no delete)."""
    if hasattr(reservation, "_prefetched_objects_cache") and "guests" in getattr(
        reservation, "_prefetched_objects_cache", {}
    ):
        guest_list = list(reservation.guests.all())
    else:
        guest_list = list(Guest.objects.filter(reservation_id=reservation.pk))
    return [guest for guest in guest_list if not is_removable_empty_guest(guest)]


def remove_unfilled_secondary_guests(reservation: Reservation) -> int:
    """Delete unfilled secondary guests before checkout. Returns number deleted."""
    if hasattr(reservation, "_prefetched_objects_cache") and "guests" in getattr(
        reservation, "_prefetched_objects_cache", {}
    ):
        to_remove = [
            guest.pk
            for guest in reservation.guests.all()
            if is_removable_empty_guest(guest)
        ]
    else:
        to_remove = [
            guest.pk
            for guest in Guest.objects.filter(reservation_id=reservation.pk)
            if is_removable_empty_guest(guest)
        ]
    if not to_remove:
        return 0
    deleted, _ = Guest.objects.filter(pk__in=to_remove).delete()
    if hasattr(reservation, "_prefetched_objects_cache"):
        reservation._prefetched_objects_cache.pop("guests", None)
    return deleted
