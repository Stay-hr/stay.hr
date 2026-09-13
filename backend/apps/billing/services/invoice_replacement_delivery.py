"""Post-commit email delivery for replacement documents (ADR 0022).

Storno follows the original invoice's recorded email_recipient.
Replacement follows the frozen case recipient email.
Neither uses reservation guest-email resolution.
"""

from __future__ import annotations

from django.db import transaction

from apps.billing.models import Invoice, InvoiceReplacement
from apps.billing.services.invoice_replacement import ReplacementRejectReason
from apps.billing.services.invoice_replacement_service import _lock_case, _lock_recipient, _raise
from apps.communications.invoice_email import send_invoice_email_to
from apps.reservations.models import Reservation


def send_replacement_storno_email(*, case: InvoiceReplacement) -> dict:
    with transaction.atomic():
        reservation = Reservation.objects.select_for_update().get(pk=case.reservation_id)
        locked = _lock_case(case)
        if locked.reservation_id != reservation.pk:
            _raise(ReplacementRejectReason.SCOPE_MISMATCH, "Case reservation mismatch.")
        if not locked.storno_invoice_id:
            _raise(
                ReplacementRejectReason.STORNO_MISSING,
                "Storno invoice is missing; cannot send storno email.",
            )
        original = Invoice.objects.get(pk=locked.original_invoice_id)
        recipient = (original.email_recipient or "").strip()
        invoice_id = locked.storno_invoice_id
    if not recipient:
        return {"status": "skipped", "reason": "no_recipient", "invoice_id": invoice_id}
    return send_invoice_email_to(invoice_id, recipient)


def send_replacement_invoice_email(*, case: InvoiceReplacement) -> dict:
    with transaction.atomic():
        reservation = Reservation.objects.select_for_update().get(pk=case.reservation_id)
        locked = _lock_case(case)
        if locked.reservation_id != reservation.pk:
            _raise(ReplacementRejectReason.SCOPE_MISMATCH, "Case reservation mismatch.")
        if not locked.replacement_invoice_id:
            _raise(
                ReplacementRejectReason.REPLACEMENT_MISSING,
                "Replacement invoice is missing; cannot send replacement email.",
            )
        recipient_row = _lock_recipient(locked)
        recipient = (recipient_row.email if recipient_row is not None else "") or ""
        recipient = recipient.strip()
        invoice_id = locked.replacement_invoice_id
    if not recipient:
        return {"status": "skipped", "reason": "no_recipient", "invoice_id": invoice_id}
    return send_invoice_email_to(invoice_id, recipient)
