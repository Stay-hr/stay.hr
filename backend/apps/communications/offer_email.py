"""Send booking offer PDF to invoice_email."""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone

from apps.billing.models import BookingOffer
from apps.communications.guest_email import (
    _language_for_reservation,
    _sender_for_reservation,
    _smtp_connection_for_reservation,
)

logger = logging.getLogger(__name__)


def resolve_offer_recipient(reservation) -> str | None:
    email = (getattr(reservation, "invoice_email", None) or "").strip()
    if email:
        return email
    email = (reservation.booker_email or "").strip()
    if email:
        return email
    primary = reservation.guests.filter(is_primary=True).first()
    if primary and (primary.email or "").strip():
        return primary.email.strip()
    return None


def _public_offer_pdf_url(offer: BookingOffer) -> str:
    base = (settings.STAY_PUBLIC_API_URL or "https://api.stay.hr").rstrip("/")
    return f"{base}/api/v1/public/offers/{offer.public_access_token}/pdf/"


def send_booking_offer_email(offer_id: int) -> dict:
    try:
        offer = BookingOffer.objects.select_related(
            "reservation",
            "reservation__property",
            "reservation__tenant",
        ).get(pk=offer_id)
    except BookingOffer.DoesNotExist:
        return {"status": "missing", "offer_id": offer_id}

    reservation = offer.reservation
    recipient = resolve_offer_recipient(reservation)
    if not recipient:
        return {"status": "skipped", "reason": "no_recipient", "offer_id": offer_id}

    connection = _smtp_connection_for_reservation(reservation)
    if connection is None:
        return {"status": "skipped", "reason": "no_smtp", "offer_id": offer_id}

    if not offer.pdf_file:
        return {"status": "skipped", "reason": "no_pdf", "offer_id": offer_id}

    sender, _from_email = _sender_for_reservation(reservation)
    language = _language_for_reservation(reservation)
    snapshot = offer.snapshot or {}
    buyer = snapshot.get("buyer") or {}
    context = {
        "buyer_name": buyer.get("name") or reservation.booker_name,
        "booking_code": reservation.booking_code,
        "property_name": reservation.property.name,
        "offer_number": offer.offer_number,
        "offer_url": _public_offer_pdf_url(offer),
        "total": snapshot.get("total") or "",
        "currency": snapshot.get("currency") or "EUR",
    }
    subject = f"Ponuda — {reservation.property.name}"
    text_body = render_to_string(f"communications/offer_email_{language}.txt", context)
    html_body = render_to_string(f"communications/offer_email_{language}.html", context)

    message = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=sender,
        to=[recipient],
        connection=connection,
    )
    message.attach_alternative(html_body, "text/html")
    offer.pdf_file.open("rb")
    try:
        message.attach(
            offer.pdf_file.name.rsplit("/", 1)[-1],
            offer.pdf_file.read(),
            "application/pdf",
        )
    finally:
        offer.pdf_file.close()

    message.send(fail_silently=False)

    offer.email_recipient = recipient
    offer.email_sent_at = timezone.now()
    offer.save(update_fields=["email_recipient", "email_sent_at", "updated_at"])
    logger.info(
        "offer_sent offer_id=%s reservation_id=%s recipient=%s",
        offer_id,
        reservation.pk,
        recipient,
    )
    return {"status": "sent", "offer_id": offer_id, "recipient": recipient}
