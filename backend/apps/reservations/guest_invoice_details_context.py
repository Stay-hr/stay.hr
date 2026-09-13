"""Public guest invoice-details form payload."""

from __future__ import annotations

from typing import Any

from apps.billing.models import BillingRecipient
from apps.billing.services.billing_recipient_service import get_open_recipient
from apps.communications.guest_email_quality import first_usable_invoice_email
from apps.reservations.guest_invoice_details_access import InvoiceDetailsAccessResult
from apps.reservations.models import GuestInvoiceDetailsAccess, Reservation


def _dt(value) -> str | None:
    return value.isoformat() if value else None


def serialize_recipient(row: BillingRecipient) -> dict[str, Any]:
    return {
        "id": row.pk,
        "reservation_id": row.reservation_id,
        "status": row.status,
        "identity_confidence": row.identity_confidence,
        "company_name": row.company_name,
        "tax_id": row.tax_id,
        "tax_id_country": row.tax_id_country,
        "country": row.country,
        "address": row.address,
        "postal_code": row.postal_code,
        "city": row.city,
        "email": row.email,
        "phone": row.phone,
        "source": row.source,
        "source_ref": row.source_ref,
        "source_excerpt": row.source_excerpt,
        "requested_at": _dt(row.requested_at),
        "ready_at": _dt(row.ready_at),
    }


def _guest_label(reservation: Reservation) -> str:
    primary = reservation.guests.filter(is_primary=True).first()
    if primary is not None:
        name = (getattr(primary, "name", None) or "").strip()
        if not name:
            name = f"{primary.first_name} {primary.last_name}".strip()
        if name:
            return name
    return (reservation.booker_name or "").strip()


def _latest_applied_recipient(reservation: Reservation) -> BillingRecipient | None:
    return (
        BillingRecipient.objects.filter(
            reservation=reservation,
            status=BillingRecipient.Status.APPLIED,
        )
        .order_by("-applied_at", "-pk")
        .first()
    )


def build_guest_invoice_details_context(
    access: GuestInvoiceDetailsAccess,
    gate: InvoiceDetailsAccessResult,
) -> dict[str, Any]:
    reservation = access.reservation
    open_row = get_open_recipient(reservation)
    applied = None if open_row is not None else _latest_applied_recipient(reservation)
    recipient = open_row or applied
    kind = "company" if recipient is not None else "personal"
    return {
        "status": gate.gate_status,
        "writable": gate.writable,
        "kind": kind,
        "property_name": reservation.property.name,
        "check_in": reservation.check_in.isoformat(),
        "check_out": reservation.check_out.isoformat(),
        "guest_label": _guest_label(reservation),
        "booking_code": reservation.booking_code or "",
        "personal_email": first_usable_invoice_email(reservation) or "",
        "recipient": serialize_recipient(recipient) if recipient is not None else None,
    }


def serialize_guest_invoice_details_context(ctx: dict[str, Any]) -> dict[str, Any]:
    return ctx
