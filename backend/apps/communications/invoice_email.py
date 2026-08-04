"""Send guest-facing invoice emails."""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone

from apps.billing.models import Invoice
from apps.communications.guest_email import (
    _guest_recipient,
    _language_for_reservation,
    _sender_for_reservation,
    _smtp_connection_for_reservation,
)
from apps.communications.guest_email_quality import (
    is_ota_relay_email,
    is_usable_invoice_email,
)

logger = logging.getLogger(__name__)


def resolve_invoice_recipient(reservation) -> str | None:
    recipient = _guest_recipient(reservation)
    if not recipient:
        return None
    if not is_usable_invoice_email(recipient):
        if is_ota_relay_email(recipient):
            logger.info(
                "invoice_email_skipped_relay reservation_id=%s",
                reservation.pk,
                extra={
                    "event": "invoice_email_skipped_relay",
                    "reservation_id": reservation.pk,
                    "recipient": recipient,
                },
            )
        return None
    return recipient


def _public_invoice_urls(invoice: Invoice) -> tuple[str, str]:
    base = (settings.STAY_PUBLIC_API_URL or "https://api.stay.hr").rstrip("/")
    token = invoice.public_access_token
    html_url = f"{base}/api/v1/public/invoices/{token}/"
    pdf_url = f"{base}/api/v1/public/invoices/{token}/pdf/"
    return html_url, pdf_url


def send_invoice_email(invoice_id: int) -> dict:
    try:
        invoice = Invoice.objects.select_related(
            "reservation",
            "reservation__property",
            "reservation__tenant",
        ).get(pk=invoice_id)
    except Invoice.DoesNotExist:
        return {"status": "missing", "invoice_id": invoice_id}

    reservation = invoice.reservation
    recipient = resolve_invoice_recipient(reservation)
    if not recipient:
        return {"status": "skipped", "reason": "no_recipient", "invoice_id": invoice_id}

    connection = _smtp_connection_for_reservation(reservation)
    if connection is None:
        return {"status": "skipped", "reason": "no_smtp", "invoice_id": invoice_id}

    sender, _from_email = _sender_for_reservation(reservation)
    language = _language_for_reservation(reservation)
    invoice_url, invoice_pdf_url = _public_invoice_urls(invoice)
    context = {
        "booker_name": invoice.buyer_name,
        "booking_code": reservation.booking_code,
        "property_name": reservation.property.name,
        "invoice_number": invoice.invoice_number,
        "invoice_url": invoice_url,
        "invoice_pdf_url": invoice_pdf_url,
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
