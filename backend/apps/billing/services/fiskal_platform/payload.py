from __future__ import annotations

from decimal import Decimal

from apps.billing.exceptions import FiscalizationError
from apps.billing.models import Invoice, InvoiceLine, TenantFiscalSettings
from apps.billing.services.fisk1.timing import issued_at_for_f1
from apps.billing.services.payment import fisk1_payment_code


def _operator_oib(settings: TenantFiscalSettings) -> str:
    code = (settings.operator_code or "").strip()
    digits = "".join(ch for ch in code if ch.isdigit())
    if len(digits) >= 11:
        return digits[:11]
    return settings.issuer_oib


def _amount(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')):.2f}"


def build_guest_invoice_f1_payload(
    invoice: Invoice,
    settings: TenantFiscalSettings,
) -> dict:
    accommodation = invoice.lines.filter(
        line_kind=InvoiceLine.LineKind.ACCOMMODATION
    ).first()
    if accommodation is None:
        raise FiscalizationError("Invoice has no accommodation line.")

    issued_at = issued_at_for_f1(invoice)

    vat_base = accommodation.unit_price * accommodation.quantity
    payload: dict = {
        "sequence_number": invoice.sequence_number,
        "issued_at": issued_at.strftime("%d.%m.%YT%H:%M:%S"),
        "issuer_oib": settings.issuer_oib,
        "operator_oib": _operator_oib(settings),
        "zki": invoice.zki,
        "business_premise_code": settings.business_premise_code,
        "payment_device_code": settings.payment_device_code,
        "payment_code": fisk1_payment_code(invoice.payment_method),
        "in_vat_system": True,
        "vat_rate": _amount(accommodation.vat_rate),
        "vat_base": _amount(vat_base),
        "vat_amount": _amount(accommodation.vat_amount),
        "total": _amount(invoice.total),
    }
    if invoice.reservation_id:
        payload["reservation_id"] = invoice.reservation_id
    if invoice.buyer_name:
        payload["guest_name"] = invoice.buyer_name
    return payload
