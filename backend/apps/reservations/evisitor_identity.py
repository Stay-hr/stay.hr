"""Staff-flagged invented eVisitor identity on a guest."""

from __future__ import annotations

from apps.reservations.models import Guest, Reservation


def is_evisitor_identity_invented(guest: Guest | None) -> bool:
    return bool(guest is not None and guest.evisitor_identity_invented_at)


def invoice_delivery_blocked_guest(reservation: Reservation) -> Guest | None:
    """Primary guest when invoice mail/link must not go to the stay recipient."""
    primary = reservation.guests.filter(is_primary=True).first()
    if is_evisitor_identity_invented(primary):
        return primary
    return None
