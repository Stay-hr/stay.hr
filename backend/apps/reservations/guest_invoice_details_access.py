"""Guest invoice-details access token CRUD, gate, and URL helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from django.db import transaction
from django.utils import timezone

from apps.billing.models import BillingRecipient, Invoice
from apps.reservations.guest_checkin_session import resolve_guest_checkin_base_url
from apps.reservations.models import (
    GuestInvoiceDetailsAccess,
    GuestInvoiceDetailsAccessCreatedFrom,
    GuestInvoiceDetailsAccessStatus,
    Reservation,
)

InvoiceDetailsGateStatus = Literal[
    "active",
    "expired",
    "revoked",
    "unavailable",
    "issued",
]

INACTIVE_RESERVATION_STATUSES = frozenset(
    {
        Reservation.Status.CANCELED,
        Reservation.Status.REFUSED,
        Reservation.Status.NO_SHOW,
    }
)

WRITABLE_RESERVATION_STATUSES = frozenset(
    {
        Reservation.Status.EXPECTED,
        Reservation.Status.CHECKED_IN,
        Reservation.Status.CHECKED_OUT,
    }
)


@dataclass(frozen=True)
class InvoiceDetailsAccessResult:
    allowed: bool
    writable: bool
    http_status: int
    gate_status: InvoiceDetailsGateStatus


def reservation_has_issued_invoice(reservation: Reservation) -> bool:
    if Invoice.objects.filter(
        reservation_id=reservation.pk,
        tenant_id=reservation.tenant_id,
    ).exists():
        return True
    return BillingRecipient.objects.filter(
        reservation_id=reservation.pk,
        tenant_id=reservation.tenant_id,
        status=BillingRecipient.Status.APPLIED,
    ).exists()


def get_invoice_details_access_by_token(token) -> GuestInvoiceDetailsAccess | None:
    return (
        GuestInvoiceDetailsAccess.objects.select_related(
            "reservation",
            "reservation__property",
            "reservation__tenant",
        )
        .filter(token=token)
        .first()
    )


def get_active_invoice_details_access(
    reservation: Reservation,
) -> GuestInvoiceDetailsAccess | None:
    return (
        GuestInvoiceDetailsAccess.objects.filter(
            reservation=reservation,
            status=GuestInvoiceDetailsAccessStatus.ACTIVE,
        )
        .order_by("-created_at")
        .first()
    )


def evaluate_invoice_details_access(
    access: GuestInvoiceDetailsAccess,
    *,
    now: datetime | None = None,
) -> InvoiceDetailsAccessResult:
    now = now or timezone.now()
    reservation = access.reservation

    if reservation.status in INACTIVE_RESERVATION_STATUSES:
        return InvoiceDetailsAccessResult(False, False, 410, "unavailable")

    if access.status == GuestInvoiceDetailsAccessStatus.REVOKED or access.revoked_at is not None:
        return InvoiceDetailsAccessResult(False, False, 410, "revoked")

    if access.expires_at is not None and now > access.expires_at:
        return InvoiceDetailsAccessResult(False, False, 410, "expired")

    if reservation.status not in WRITABLE_RESERVATION_STATUSES:
        return InvoiceDetailsAccessResult(False, False, 410, "unavailable")

    if reservation_has_issued_invoice(reservation):
        return InvoiceDetailsAccessResult(True, False, 200, "issued")

    return InvoiceDetailsAccessResult(True, True, 200, "active")


@transaction.atomic
def ensure_active_invoice_details_access(
    reservation: Reservation,
    *,
    created_from: str = GuestInvoiceDetailsAccessCreatedFrom.SYSTEM,
) -> GuestInvoiceDetailsAccess:
    """Return the single active invoice-details access, creating if needed."""
    if reservation.status in INACTIVE_RESERVATION_STATUSES:
        raise ValueError("Invoice details are not available for this reservation.")

    existing = get_active_invoice_details_access(reservation)
    if existing is not None:
        return existing

    return GuestInvoiceDetailsAccess.objects.create(
        tenant_id=reservation.tenant_id,
        reservation=reservation,
        status=GuestInvoiceDetailsAccessStatus.ACTIVE,
        created_from=created_from,
    )


@transaction.atomic
def revoke_invoice_details_access(
    access: GuestInvoiceDetailsAccess,
) -> GuestInvoiceDetailsAccess:
    if access.status == GuestInvoiceDetailsAccessStatus.REVOKED:
        return access
    access.status = GuestInvoiceDetailsAccessStatus.REVOKED
    access.revoked_at = timezone.now()
    access.save(update_fields=["status", "revoked_at", "updated_at"])
    return access


def build_guest_invoice_details_url(
    access: GuestInvoiceDetailsAccess,
    reservation: Reservation,
) -> str:
    base = resolve_guest_checkin_base_url(reservation)
    return f"{base}/invoice-details/{access.token}"
