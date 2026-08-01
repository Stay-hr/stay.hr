from __future__ import annotations

from datetime import date

from apps.integrations.evisitor.eligibility import guest_requires_evisitor
from apps.reservations.models import EvisitorGuestStatus, Guest, Reservation


def evisitor_status_for_guest(guest: Guest) -> str:
    status = (guest.evisitor_status or "").strip()
    if status in {c[0] for c in EvisitorGuestStatus.choices}:
        return status
    return EvisitorGuestStatus.NOT_SENT


def guests_requiring_evisitor(guests, *, reference_date: date) -> list[Guest]:
    return [
        guest
        for guest in guests
        if guest_requires_evisitor(guest, reference_date=reference_date)
    ]


def evisitor_summary_for_guests(guests, *, reference_date: date | None = None) -> str:
    guest_list = list(guests)
    if not guest_list:
        return "none"

    ref = reference_date
    if ref is None:
        ref = guest_list[0].reservation.check_in

    required_guests = guests_requiring_evisitor(guest_list, reference_date=ref)
    if not required_guests:
        return "complete"

    statuses = [evisitor_status_for_guest(g) for g in required_guests]
    registered = {
        EvisitorGuestStatus.SENT,
        EvisitorGuestStatus.CHECKED_OUT,
        EvisitorGuestStatus.CHECKOUT_FAILED,
    }
    if all(s == EvisitorGuestStatus.CHECKED_OUT for s in statuses):
        return "checked_out"
    # "complete" = all eligible guests are registered in eVisitor (check-in done).
    # Does NOT mean reservation checkout finished — checkout_failed still needs CheckOut.
    if all(s in registered for s in statuses):
        return "complete"
    return "incomplete"


def evisitor_summary_for_reservation(reservation: Reservation) -> str:
    if hasattr(reservation, "_prefetched_objects_cache") and "guests" in getattr(
        reservation, "_prefetched_objects_cache", {}
    ):
        return evisitor_summary_for_guests(
            reservation.guests.all(),
            reference_date=reservation.check_in,
        )
    return evisitor_summary_for_guests(
        Guest.objects.filter(reservation_id=reservation.pk).select_related("reservation"),
        reference_date=reservation.check_in,
    )


def evisitor_progress_for_guests(guests, *, reference_date: date | None = None) -> dict[str, int]:
    guest_list = list(guests)
    if not guest_list:
        return {"required": 0, "sent": 0, "failed": 0, "pending": 0}

    ref = reference_date
    if ref is None:
        ref = guest_list[0].reservation.check_in

    required_guests = guests_requiring_evisitor(guest_list, reference_date=ref)
    sent = failed = pending = 0
    for guest in required_guests:
        status = evisitor_status_for_guest(guest)
        if status in (
            EvisitorGuestStatus.SENT,
            EvisitorGuestStatus.CHECKED_OUT,
            EvisitorGuestStatus.CHECKOUT_FAILED,
        ):
            sent += 1
        elif status == EvisitorGuestStatus.FAILED:
            failed += 1
        else:
            pending += 1

    return {
        "required": len(required_guests),
        "sent": sent,
        "failed": failed,
        "pending": pending,
    }


def evisitor_progress_for_reservation(reservation: Reservation) -> dict[str, int]:
    if hasattr(reservation, "_prefetched_objects_cache") and "guests" in getattr(
        reservation, "_prefetched_objects_cache", {}
    ):
        return evisitor_progress_for_guests(
            reservation.guests.all(),
            reference_date=reservation.check_in,
        )
    return evisitor_progress_for_guests(
        Guest.objects.filter(reservation_id=reservation.pk).select_related("reservation"),
        reference_date=reservation.check_in,
    )
