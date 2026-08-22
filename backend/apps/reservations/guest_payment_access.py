"""Guest payment access token CRUD, gate, and URL helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from django.db import transaction
from django.utils import timezone

from apps.reservations.guest_checkin_session import resolve_guest_checkin_base_url
from apps.reservations.models import (
    GuestPaymentAccess,
    GuestPaymentAccessCreatedFrom,
    GuestPaymentAccessStatus,
    Reservation,
)

PaymentGateStatus = Literal[
    "active",
    "expired",
    "revoked",
    "unavailable",
]

INACTIVE_RESERVATION_STATUSES = frozenset(
    {
        Reservation.Status.CANCELED,
        Reservation.Status.REFUSED,
        Reservation.Status.NO_SHOW,
    }
)


@dataclass(frozen=True)
class PaymentAccessResult:
    allowed: bool
    http_status: int
    gate_status: PaymentGateStatus


def build_payment_reference(reservation: Reservation) -> str:
    """Stable payment reference from booking code (canonical for /pay, email, WhatsApp)."""
    code = (reservation.booking_code or "").strip()
    if code:
        return code
    return f"STAY-{reservation.pk}"


def get_payment_access_by_token(token) -> GuestPaymentAccess | None:
    return (
        GuestPaymentAccess.objects.select_related(
            "reservation",
            "reservation__property",
            "reservation__tenant",
        )
        .filter(token=token)
        .first()
    )


def get_active_payment_access(reservation: Reservation) -> GuestPaymentAccess | None:
    return (
        GuestPaymentAccess.objects.filter(
            reservation=reservation,
            status=GuestPaymentAccessStatus.ACTIVE,
        )
        .order_by("-created_at")
        .first()
    )


def evaluate_payment_access(
    access: GuestPaymentAccess,
    *,
    now: datetime | None = None,
) -> PaymentAccessResult:
    now = now or timezone.now()
    reservation = access.reservation

    if reservation.status in INACTIVE_RESERVATION_STATUSES:
        return PaymentAccessResult(False, 410, "unavailable")

    if access.status == GuestPaymentAccessStatus.REVOKED or access.revoked_at is not None:
        return PaymentAccessResult(False, 410, "revoked")

    if access.expires_at is not None and now > access.expires_at:
        return PaymentAccessResult(False, 410, "expired")

    if reservation.amount is None:
        return PaymentAccessResult(False, 410, "unavailable")

    return PaymentAccessResult(True, 200, "active")


@transaction.atomic
def ensure_active_payment_access(
    reservation: Reservation,
    *,
    created_from: str = GuestPaymentAccessCreatedFrom.SYSTEM,
) -> GuestPaymentAccess:
    """Return the single active payment access for a reservation, creating if needed."""
    if reservation.status in INACTIVE_RESERVATION_STATUSES:
        raise ValueError("Payment instructions are not available for this reservation.")
    if reservation.amount is None:
        raise ValueError("Reservation amount is required for payment instructions.")

    existing = get_active_payment_access(reservation)
    if existing is not None:
        return existing

    return GuestPaymentAccess.objects.create(
        tenant_id=reservation.tenant_id,
        reservation=reservation,
        status=GuestPaymentAccessStatus.ACTIVE,
        created_from=created_from,
    )


@transaction.atomic
def revoke_payment_access(access: GuestPaymentAccess) -> GuestPaymentAccess:
    if access.status == GuestPaymentAccessStatus.REVOKED:
        return access
    access.status = GuestPaymentAccessStatus.REVOKED
    access.revoked_at = timezone.now()
    access.save(update_fields=["status", "revoked_at", "updated_at"])
    return access


def build_guest_payment_url(access: GuestPaymentAccess, reservation: Reservation) -> str:
    base = resolve_guest_checkin_base_url(reservation)
    return f"{base}/pay/{access.token}"
