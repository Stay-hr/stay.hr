from __future__ import annotations

import uuid

from django.db import transaction

from apps.billing.exceptions import FiscalConfigError, InvoiceBuildError
from apps.billing.models import Invoice, InvoiceLine, TenantFiscalSettings
from apps.billing.services.invoice_builder import build_invoice_from_reservation
from apps.billing.services.pdf import render_invoice_pdf
from apps.billing.services.zki import calculate_zki, load_fiscal_private_key
from apps.core.timezone import tenant_local_now
from apps.reservations.models import Reservation


def _next_invoice_number(settings: TenantFiscalSettings) -> tuple[int, str]:
    locked = TenantFiscalSettings.objects.select_for_update().get(pk=settings.pk)
    locked.invoice_sequence += 1
    locked.save(update_fields=["invoice_sequence", "updated_at"])
    seq = locked.invoice_sequence
    display = f"{seq}-{locked.business_premise_code}-{locked.payment_device_code}"
    settings.invoice_sequence = locked.invoice_sequence
    return seq, display


def get_fiscal_settings_for_reservation(reservation: Reservation) -> TenantFiscalSettings:
    settings, _ = TenantFiscalSettings.objects.get_or_create(tenant=reservation.tenant)
    return settings


def validate_fiscal_settings(settings: TenantFiscalSettings) -> None:
    if not settings.is_vat_registered:
        raise FiscalConfigError("Tenant is not in the VAT system.")
    missing = []
    if not settings.issuer_oib:
        missing.append("issuer_oib")
    if not settings.issuer_name:
        missing.append("issuer_name")
    if not settings.business_premise_code:
        missing.append("business_premise_code")
    if not settings.payment_device_code:
        missing.append("payment_device_code")
    if not settings.has_certificate:
        missing.append("certificate_file")
    if not settings.has_certificate_password:
        missing.append("certificate_password")
    if missing:
        raise FiscalConfigError(f"Missing fiscal settings: {', '.join(missing)}")


@transaction.atomic
def issue_guest_invoice(reservation: Reservation) -> Invoice:
    if hasattr(reservation, "invoice"):
        return reservation.invoice

    settings = get_fiscal_settings_for_reservation(reservation)
    if not settings.is_vat_registered:
        raise FiscalConfigError("Tenant has no VAT fiscal settings.")

    validate_fiscal_settings(settings)
    built = build_invoice_from_reservation(reservation, settings)
    issued_at = tenant_local_now(reservation.tenant)
    seq, invoice_number = _next_invoice_number(settings)

    zki = calculate_zki(
        oib=settings.issuer_oib,
        issued_at=issued_at,
        invoice_number=str(seq),
        business_premise_code=settings.business_premise_code,
        payment_device_code=settings.payment_device_code,
        total=built.total,
        private_key=load_fiscal_private_key(settings),
    )

    invoice = Invoice.objects.create(
        tenant=reservation.tenant,
        reservation=reservation,
        invoice_number=invoice_number,
        sequence_number=seq,
        issued_at=issued_at,
        buyer_name=built.buyer_name,
        buyer_document_number=built.buyer_document_number,
        buyer_address=built.buyer_address,
        buyer_country=built.buyer_country,
        payment_method=built.payment_method,
        payment_note=built.payment_note,
        subtotal=built.subtotal,
        vat_amount=built.vat_amount,
        total=built.total,
        currency=built.currency,
        zki=zki,
        fiscal_status=Invoice.FiscalStatus.PENDING,
        public_access_token=uuid.uuid4(),
    )

    InvoiceLine.objects.bulk_create(
        [
            InvoiceLine(
                invoice=invoice,
                sort_order=line.sort_order,
                line_kind=line.line_kind,
                description=line.description,
                quantity=line.quantity,
                unit_price=line.unit_price,
                vat_rate=line.vat_rate,
                vat_amount=line.vat_amount,
                line_total=line.line_total,
            )
            for line in built.lines
        ]
    )

    render_invoice_pdf(invoice, settings)
    return invoice


def should_issue_invoice_on_checkout(reservation: Reservation) -> bool:
    settings = get_fiscal_settings_for_reservation(reservation)
    return settings.is_vat_registered


def refresh_invoice_buyer_from_reservation(invoice: Invoice) -> None:
    """Refresh buyer snapshot from reservation before PDF regeneration."""
    from apps.billing.services.invoice_builder import (
        resolve_buyer_country,
        resolve_buyer_identity,
        resolve_buyer_name,
    )

    reservation = invoice.reservation
    invoice.buyer_name = resolve_buyer_name(reservation)
    document_number, address = resolve_buyer_identity(reservation)
    invoice.buyer_document_number = document_number
    invoice.buyer_address = address
    invoice.buyer_country = resolve_buyer_country(reservation)
    invoice.save(
        update_fields=[
            "buyer_name",
            "buyer_document_number",
            "buyer_address",
            "buyer_country",
            "updated_at",
        ]
    )
