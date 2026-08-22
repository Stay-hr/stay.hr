"""Public guest payment instructions payload."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from apps.billing.models import TenantFiscalSettings
from apps.reservations.guest_payment_access import build_payment_reference
from apps.reservations.models import GuestPaymentAccess, Reservation


def _guest_label(reservation: Reservation) -> str:
    primary = reservation.guests.filter(is_primary=True).first()
    if primary is not None:
        name = f"{primary.first_name} {primary.last_name}".strip() or primary.name.strip()
        if name:
            return name
    company = (getattr(reservation, "buyer_company_name", None) or "").strip()
    if company:
        return company
    return (reservation.booker_name or "").strip()


def _unit_label(reservation: Reservation) -> str:
    unit_row = reservation.units.order_by("sort_order", "id").first()
    if unit_row is None:
        return ""
    if unit_row.unit_id and unit_row.unit:
        return unit_row.unit.code or unit_row.room_name
    return unit_row.room_name or ""


def _payment_note(reservation: Reservation) -> str:
    guest = _guest_label(reservation)
    period = f"{reservation.check_in:%d.%m.%Y} – {reservation.check_out:%d.%m.%Y}"
    if guest:
        return f"Boravak {period}, gost: {guest}"
    return f"Boravak {period}"


def build_guest_payment_context(
    access: GuestPaymentAccess,
) -> dict[str, Any]:
    reservation = access.reservation
    fiscal = TenantFiscalSettings.objects.filter(tenant_id=reservation.tenant_id).first()
    amount = reservation.amount
    if amount is None:
        raise ValueError("Reservation amount is required.")

    return {
        "status": "active",
        "property_name": reservation.property.name,
        "check_in": reservation.check_in.isoformat(),
        "check_out": reservation.check_out.isoformat(),
        "unit_label": _unit_label(reservation),
        "guest_label": _guest_label(reservation),
        "payment_amount": str(Decimal(amount).quantize(Decimal("0.01"))),
        "currency": (reservation.currency or "EUR").strip().upper() or "EUR",
        "includes_tourist_tax": True,
        "iban": (fiscal.issuer_iban if fiscal else "") or "",
        "beneficiary": (fiscal.issuer_name if fiscal else "") or "",
        "payment_reference": build_payment_reference(reservation),
        "payment_note": _payment_note(reservation),
    }


def serialize_guest_payment_context(ctx: dict[str, Any]) -> dict[str, Any]:
    return dict(ctx)
