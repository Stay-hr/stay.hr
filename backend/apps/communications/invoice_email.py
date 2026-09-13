"""Send guest-facing invoice emails."""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone

from apps.billing.models import Invoice
from apps.communications.guest_email import (
    _language_for_reservation,
    _sender_for_reservation,
    _smtp_connection_for_reservation,
)
from apps.communications.guest_email_quality import (
    first_usable_invoice_email,
    invoice_email_candidates,
    is_ota_relay_email,
)

logger = logging.getLogger(__name__)


def resolve_invoice_recipient(reservation) -> str | None:
    usable = first_usable_invoice_email(reservation)
    if usable:
        return usable
    for candidate in invoice_email_candidates(reservation):
        if is_ota_relay_email(candidate):
            logger.info(
                "invoice_email_skipped_relay reservation_id=%s",
                reservation.pk,
                extra={
                    "event": "invoice_email_skipped_relay",
                    "reservation_id": reservation.pk,
                    "recipient": candidate,
                },
            )
            break
    return None


def _public_invoice_url(invoice: Invoice) -> str:
    base = (settings.STAY_PUBLIC_API_URL or "https://api.stay.hr").rstrip("/")
    return f"{base}/api/v1/public/invoices/{invoice.public_access_token}/"


def _load_invoice(invoice_id: int) -> Invoice | None:
    try:
        return Invoice.objects.select_related(
            "reservation",
            "reservation__property",
            "reservation__tenant",
        ).get(pk=invoice_id)
    except Invoice.DoesNotExist:
        return None


def _deliver_invoice_email(invoice: Invoice, recipient: str | None) -> dict:
    invoice_id = invoice.pk
    if not recipient:
        return {"status": "skipped", "reason": "no_recipient", "invoice_id": invoice_id}

    reservation = invoice.reservation
    connection = _smtp_connection_for_reservation(reservation)
    if connection is None:
        return {"status": "skipped", "reason": "no_smtp", "invoice_id": invoice_id}

    sender, _from_email = _sender_for_reservation(reservation)
    language = _language_for_reservation(reservation)
    context = {
        "booker_name": invoice.buyer_name,
        "booking_code": reservation.booking_code,
        "property_name": reservation.property.name,
        "invoice_number": invoice.invoice_number,
        "invoice_url": _public_invoice_url(invoice),
    }
    subject = f"Račun — {reservation.property.name}"
    text_body = render_to_string(f"communications/invoice_email_{language}.txt", context)
    html_body = render_to_string(f"communications/invoice_email_{language}.html", context)

    message = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=sender,
        to=[recipient],
        connection=connection,
    )
    message.attach_alternative(html_body, "text/html")
    message.send(fail_silently=False)

    invoice.email_recipient = recipient
    invoice.email_sent_at = timezone.now()
    invoice.save(update_fields=["email_recipient", "email_sent_at", "updated_at"])
    logger.info(
        "invoice_sent invoice_id=%s reservation_id=%s recipient=%s",
        invoice_id,
        reservation.pk,
        recipient,
        extra={
            "event": "invoice_sent",
            "invoice_id": invoice_id,
            "reservation_id": reservation.pk,
            "recipient": recipient,
        },
    )
    return {"status": "sent", "invoice_id": invoice_id, "recipient": recipient}


def send_invoice_email(invoice_id: int) -> dict:
    invoice = _load_invoice(invoice_id)
    if invoice is None:
        return {"status": "missing", "invoice_id": invoice_id}
    from apps.reservations.evisitor_identity import invoice_delivery_blocked_guest

    if invoice_delivery_blocked_guest(invoice.reservation) is not None:
        return {
            "status": "skipped",
            "reason": "invented_identity",
            "invoice_id": invoice_id,
        }
    return _deliver_invoice_email(invoice, resolve_invoice_recipient(invoice.reservation))


def send_invoice_email_to(invoice_id: int, recipient: str) -> dict:
    invoice = _load_invoice(invoice_id)
    if invoice is None:
        return {"status": "missing", "invoice_id": invoice_id}
    return _deliver_invoice_email(invoice, (recipient or "").strip())
