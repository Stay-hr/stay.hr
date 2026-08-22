"""Build immutable booking offer snapshot from reservation + fiscal settings."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from apps.billing.exceptions import FiscalConfigError, InvoiceBuildError
from apps.billing.models import Invoice, TenantFiscalSettings
from apps.billing.services.invoice_builder import build_invoice_from_reservation
from apps.reservations.guest_payment_access import build_payment_reference
from apps.reservations.models import Reservation


def _unit_label(reservation: Reservation) -> str:
    unit_row = reservation.units.order_by("sort_order", "id").first()
    if unit_row is None:
        return ""
    if unit_row.unit_id and unit_row.unit:
        return unit_row.unit.code or unit_row.room_name
    return unit_row.room_name or ""


def _guest_label(reservation: Reservation) -> str:
    primary = reservation.guests.filter(is_primary=True).first()
    if primary is not None:
        name = f"{primary.first_name} {primary.last_name}".strip() or primary.name.strip()
        if name:
            return name
    return (reservation.booker_name or "").strip()


def _offer_payment_note(
    *,
    iban: str,
    payment_reference: str,
) -> str:
    parts = ["Uplata transakcijskim računom prije dolaska."]
    if iban:
        parts.append(f"IBAN: {iban}")
    if payment_reference:
        parts.append(f"Poziv na broj: {payment_reference}")
    return " ".join(parts)


def _offer_number(reservation: Reservation) -> str:
    code = (reservation.booking_code or "").strip()
    if code:
        return f"PON-{code}"
    return f"PON-{reservation.pk}"


def build_offer_snapshot(
    reservation: Reservation,
    settings: TenantFiscalSettings,
    *,
    valid_until: date | None = None,
) -> dict[str, Any]:
    """Pricing/lines from invoice_builder; seller frozen from settings at generation."""
    if not settings.is_vat_registered:
        raise FiscalConfigError("Tenant is not marked as VAT registered.")
    if reservation.status in {
        Reservation.Status.CANCELED,
        Reservation.Status.REFUSED,
        Reservation.Status.NO_SHOW,
    }:
        raise InvoiceBuildError("Offer is not available for this reservation status.")

    built = build_invoice_from_reservation(reservation, settings)
    payment_reference = build_payment_reference(reservation)
    seller_iban = (settings.issuer_iban or "").strip()
    valid = valid_until or reservation.check_in

    lines = [
        {
            "sort_order": line.sort_order,
            "line_kind": line.line_kind,
            "description": line.description,
            "quantity": str(line.quantity),
            "unit_price": str(line.unit_price),
            "vat_rate": str(line.vat_rate),
            "vat_amount": str(line.vat_amount),
            "line_total": str(line.line_total),
        }
        for line in built.lines
    ]

    return {
        "schema_version": 1,
        "offer_number": _offer_number(reservation),
        "seller": {
            "name": (settings.issuer_name or "").strip(),
            "address": (settings.issuer_address or "").strip(),
            "oib": (settings.issuer_oib or "").strip(),
            "iban": seller_iban,
        },
        "buyer": {
            "name": built.buyer_name,
            "document_number": built.buyer_document_number,
            "address": built.buyer_address,
            "country": built.buyer_country,
        },
        "stay": {
            "check_in": reservation.check_in.isoformat(),
            "check_out": reservation.check_out.isoformat(),
            "unit_label": _unit_label(reservation),
            "guest_label": _guest_label(reservation),
            "property_name": reservation.property.name,
        },
        "lines": lines,
        "subtotal": str(built.subtotal),
        "vat_amount": str(built.vat_amount),
        "total": str(built.total),
        "currency": built.currency,
        "payment_method": Invoice.PaymentMethod.TRANSFER,
        "payment_reference": payment_reference,
        "payment_note": _offer_payment_note(
            iban=seller_iban,
            payment_reference=payment_reference,
        ),
        "valid_until": valid.isoformat() if valid else None,
        "includes_tourist_tax": True,
    }


def snapshot_decimal_fields(snapshot: dict[str, Any]) -> dict[str, Decimal]:
    return {
        "subtotal": Decimal(snapshot["subtotal"]),
        "vat_amount": Decimal(snapshot["vat_amount"]),
        "total": Decimal(snapshot["total"]),
    }
