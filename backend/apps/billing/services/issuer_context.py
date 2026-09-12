"""Frozen issuer/document context on Invoice (ADR 0022).

Certificate and password are never snapshotted. Legacy invoices keep empty
fields and fall back to live TenantFiscalSettings for display only.
"""

from __future__ import annotations

from typing import Any

from apps.billing.exceptions import InvoiceIssuerContextMissing

ISSUER_CONTEXT_FIELDS: tuple[str, ...] = (
    "issuer_name",
    "issuer_address",
    "issuer_oib",
    "issuer_iban",
    "operator_code",
    "business_premise_code",
    "payment_device_code",
    "reservation_reference",
)

FROZEN_ISSUER_REQUIRED_FIELDS: tuple[str, ...] = (
    "issuer_name",
    "issuer_oib",
    "business_premise_code",
    "payment_device_code",
    "reservation_reference",
)


def reservation_reference_for(reservation) -> str:
    booking_code = (getattr(reservation, "booking_code", None) or "").strip()
    if booking_code:
        return booking_code
    external_id = (getattr(reservation, "external_id", None) or "").strip()
    if external_id:
        return external_id
    pk = getattr(reservation, "pk", None)
    return str(pk) if pk is not None else ""


def snapshot_issuer_document_context(settings, reservation) -> dict[str, str]:
    issuer_oib = (getattr(settings, "issuer_oib", None) or "").strip()
    operator_code = (getattr(settings, "operator_code", None) or "").strip() or issuer_oib
    return {
        "issuer_name": getattr(settings, "issuer_name", None) or "",
        "issuer_address": getattr(settings, "issuer_address", None) or "",
        "issuer_oib": issuer_oib,
        "issuer_iban": getattr(settings, "issuer_iban", None) or "",
        "operator_code": operator_code,
        "business_premise_code": getattr(settings, "business_premise_code", None) or "",
        "payment_device_code": getattr(settings, "payment_device_code", None) or "",
        "reservation_reference": reservation_reference_for(reservation),
    }


def has_frozen_issuer_context(invoice: Any) -> bool:
    return all((getattr(invoice, name, None) or "").strip() for name in FROZEN_ISSUER_REQUIRED_FIELDS)


def require_frozen_issuer_context(invoice: Any) -> None:
    if has_frozen_issuer_context(invoice):
        return
    invoice_number = getattr(invoice, "invoice_number", None) or getattr(invoice, "pk", "unknown")
    raise InvoiceIssuerContextMissing(
        f"Invoice {invoice_number} has no frozen issuer context and cannot be regenerated."
    )
