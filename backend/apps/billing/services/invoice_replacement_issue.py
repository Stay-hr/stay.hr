"""Privileged replacement invoice writers (ADR 0022).

Storno is a new negative Invoice in the normal tenant sequence.
No CIS submit and no email inside the transaction.
"""

from __future__ import annotations

import uuid

from django.db import IntegrityError, transaction

from apps.billing.exceptions import InvoiceReplacementError
from apps.billing.models import Invoice, InvoiceLine, InvoiceReplacement
from apps.billing.services.invoice_replacement import (
    InvoiceDocumentSnapshot,
    InvoiceLineSnapshot,
    ReplacementCaseStatus,
    ReplacementRejectReason,
    can_issue_storno,
    expected_issuer_oib,
    is_structurally_ready,
    negate_invoice_snapshot,
)
from apps.billing.services.invoice_replacement_service import (
    _lock_case,
    _lock_recipient,
    _raise,
)
from apps.billing.services.issue import (
    _next_invoice_number,
    get_fiscal_settings_for_reservation,
    validate_fiscal_settings,
)
from apps.billing.services.issuer_context import snapshot_issuer_document_context
from apps.billing.services.pdf import render_invoice_pdf
from apps.billing.services.zki import calculate_zki, load_fiscal_private_key
from apps.core.timezone import tenant_local_now
from apps.reservations.models import Reservation


def _snapshot_invoice(invoice: Invoice) -> InvoiceDocumentSnapshot:
    return InvoiceDocumentSnapshot(
        subtotal=invoice.subtotal,
        vat_amount=invoice.vat_amount,
        total=invoice.total,
        currency=invoice.currency,
        payment_method=invoice.payment_method,
        payment_note=invoice.payment_note,
        buyer_name=invoice.buyer_name,
        buyer_document_number=invoice.buyer_document_number,
        buyer_address=invoice.buyer_address,
        buyer_country=invoice.buyer_country,
        lines=tuple(
            InvoiceLineSnapshot(
                sort_order=line.sort_order,
                line_kind=line.line_kind,
                description=line.description,
                quantity=line.quantity,
                unit_price=line.unit_price,
                vat_rate=line.vat_rate,
                vat_amount=line.vat_amount,
                line_total=line.line_total,
            )
            for line in invoice.lines.order_by("sort_order", "id")
        ),
    )


def _persist_snapshot_lines(invoice: Invoice, snapshot: InvoiceDocumentSnapshot) -> None:
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
            for line in snapshot.lines
        ]
    )


def issue_replacement_storno(*, case) -> Invoice:
    try:
        with transaction.atomic():
            reservation = Reservation.objects.select_for_update().get(pk=case.reservation_id)
            locked_case = _lock_case(case)
            if locked_case.reservation_id != reservation.pk:
                _raise(ReplacementRejectReason.SCOPE_MISMATCH, "Case reservation mismatch.")
            if locked_case.storno_invoice_id:
                return Invoice.objects.select_related("reservation").get(
                    pk=locked_case.storno_invoice_id
                )

            recipient = _lock_recipient(locked_case)
            reject = can_issue_storno(
                status=ReplacementCaseStatus(locked_case.status),
                storno_invoice_id=locked_case.storno_invoice_id,
                recipient_ready=bool(recipient and is_structurally_ready(recipient.as_draft())),
                recipient_verified=bool(
                    recipient
                    and recipient.identity_confidence
                    == recipient.IdentityConfidence.VERIFIED
                ),
            )
            if reject is not None:
                _raise(reject, "Cannot issue a replacement storno for this case.")

            original = Invoice.objects.prefetch_related("lines").get(
                pk=locked_case.original_invoice_id
            )
            expected_oib = expected_issuer_oib(
                original_issuer_oib=original.issuer_oib,
                recorded_issuer_oib=locked_case.original_issuer_oib,
            )
            if not expected_oib:
                _raise(
                    ReplacementRejectReason.ISSUER_OIB_EVIDENCE_REQUIRED,
                    "Expected issuer OIB is unknown; cannot issue storno.",
                )

            settings = get_fiscal_settings_for_reservation(reservation)
            validate_fiscal_settings(settings)
            issuer_context = snapshot_issuer_document_context(settings, reservation)
            if issuer_context["issuer_oib"] != expected_oib:
                _raise(
                    ReplacementRejectReason.ISSUER_OIB_MISMATCH,
                    "Current issuer OIB does not match the original legal issuer.",
                )

            snapshot = negate_invoice_snapshot(_snapshot_invoice(original))
            issued_at = tenant_local_now(reservation.tenant)
            seq, invoice_number = _next_invoice_number(settings)
            zki = calculate_zki(
                oib=expected_oib,
                issued_at=issued_at,
                invoice_number=str(seq),
                business_premise_code=settings.business_premise_code,
                payment_device_code=settings.payment_device_code,
                total=snapshot.total,
                private_key=load_fiscal_private_key(settings),
            )
            storno = Invoice.objects.create(
                tenant=reservation.tenant,
                reservation=reservation,
                invoice_number=invoice_number,
                sequence_number=seq,
                issued_at=issued_at,
                buyer_name=snapshot.buyer_name,
                buyer_document_number=snapshot.buyer_document_number,
                buyer_address=snapshot.buyer_address,
                buyer_country=snapshot.buyer_country,
                payment_method=snapshot.payment_method,
                payment_note=snapshot.payment_note,
                subtotal=snapshot.subtotal,
                vat_amount=snapshot.vat_amount,
                total=snapshot.total,
                currency=snapshot.currency,
                zki=zki,
                fiscal_status=Invoice.FiscalStatus.PENDING,
                public_access_token=uuid.uuid4(),
                **issuer_context,
            )
            _persist_snapshot_lines(storno, snapshot)
            locked_case.storno_invoice = storno
            locked_case.save()
            render_invoice_pdf(storno, settings)
            return storno
    except IntegrityError as exc:
        refreshed = InvoiceReplacement.objects.filter(pk=case.pk).first()
        if refreshed is not None and refreshed.storno_invoice_id:
            return Invoice.objects.get(pk=refreshed.storno_invoice_id)
        raise InvoiceReplacementError(
            "Storno invoice could not be persisted.",
            reason=ReplacementRejectReason.STORNO_ALREADY_EXISTS.value,
        ) from exc
