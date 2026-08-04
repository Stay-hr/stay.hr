"""Capture usable invoice delivery email from guest inbound messages."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.communications.guest_email_quality import (
    extract_usable_invoice_emails,
    first_usable_invoice_email,
    is_usable_invoice_email,
)
from apps.reservations.models import Guest, Reservation

logger = logging.getLogger(__name__)


def invoice_email_waiting_timeout() -> timedelta:
    days = int(getattr(settings, "INVOICE_EMAIL_WAITING_TIMEOUT_DAYS", 14) or 14)
    return timedelta(days=max(1, days))


def is_waiting_for_invoice_email(reservation: Reservation) -> bool:
    return reservation.invoice_email_waiting_at is not None


def clear_invoice_email_waiting(reservation: Reservation, *, reason: str) -> None:
    if reservation.invoice_email_waiting_at is None:
        return
    reservation.invoice_email_waiting_at = None
    reservation.save(update_fields=["invoice_email_waiting_at", "updated_at"])
    if reason == "timeout":
        logger.info(
            "invoice_email_timeout reservation_id=%s",
            reservation.pk,
            extra={"event": "invoice_email_timeout", "reservation_id": reservation.pk},
        )


def maybe_timeout_invoice_email_waiting(reservation: Reservation) -> bool:
    """Clear waiting if past timeout. Returns True when timed out."""
    started = reservation.invoice_email_waiting_at
    if started is None:
        return False
    if timezone.now() - started < invoice_email_waiting_timeout():
        return False
    clear_invoice_email_waiting(reservation, reason="timeout")
    return True


def start_waiting_for_invoice_email(reservation: Reservation) -> None:
    if reservation.invoice_email_waiting_at is None:
        reservation.invoice_email_waiting_at = timezone.now()
        reservation.save(update_fields=["invoice_email_waiting_at", "updated_at"])
        logger.info(
            "invoice_email_requested reservation_id=%s",
            reservation.pk,
            extra={"event": "invoice_email_requested", "reservation_id": reservation.pk},
        )


def current_invoice_recipient(reservation: Reservation) -> str:
    """First usable invoice address (booker, then primary guest). Empty if none usable."""
    return first_usable_invoice_email(reservation) or ""


def has_usable_invoice_recipient(reservation: Reservation) -> bool:
    return bool(current_invoice_recipient(reservation))


@transaction.atomic
def update_invoice_email(reservation: Reservation, email: str) -> str:
    """Persist usable email on reservation booker + primary guest. Caller enforces guards."""
    cleaned = (email or "").strip()
    if not is_usable_invoice_email(cleaned):
        raise ValueError("email_not_usable")

    reservation = Reservation.objects.select_for_update().get(pk=reservation.pk)
    reservation.booker_email = cleaned
    reservation.invoice_email_waiting_at = None
    reservation.save(
        update_fields=["booker_email", "invoice_email_waiting_at", "updated_at"]
    )

    primary = (
        Guest.objects.select_for_update()
        .filter(reservation_id=reservation.pk, is_primary=True)
        .first()
    )
    if primary is None:
        primary = Guest.objects.select_for_update().filter(reservation_id=reservation.pk).first()
    if primary is not None:
        primary.email = cleaned
        primary.save(update_fields=["email", "updated_at"])

    logger.info(
        "invoice_email_updated reservation_id=%s",
        reservation.pk,
        extra={
            "event": "invoice_email_updated",
            "reservation_id": reservation.pk,
            "recipient": cleaned,
        },
    )
    return cleaned


class InvoiceEmailCaptureService:
    """Stateful email capture for invoice delivery (only while WAITING_FOR_EMAIL)."""

    @staticmethod
    def try_capture_while_waiting(reservation: Reservation, body: str) -> dict | None:
        """
        Extract email only when ``invoice_email_waiting_at`` is set.

        - exactly one usable → ``invoice_email_received`` then update
        - multiple usable → ``invoice_email_ambiguous`` only (no received)
        - none → None
        """
        if not is_waiting_for_invoice_email(reservation):
            return None

        usable = extract_usable_invoice_emails(body)
        if not usable:
            return None

        if len(usable) > 1:
            logger.info(
                "invoice_email_ambiguous reservation_id=%s count=%s",
                reservation.pk,
                len(usable),
                extra={
                    "event": "invoice_email_ambiguous",
                    "reservation_id": reservation.pk,
                    "email_count": len(usable),
                },
            )
            return {
                "status": "ambiguous",
                "event": "invoice_email_ambiguous",
                "emails": usable,
            }

        email = usable[0]
        logger.info(
            "invoice_email_received reservation_id=%s",
            reservation.pk,
            extra={
                "event": "invoice_email_received",
                "reservation_id": reservation.pk,
                "recipient": email,
            },
        )
        update_invoice_email(reservation, email)
        return {
            "status": "updated",
            "event": "invoice_email_updated",
            "email": email,
        }

    @staticmethod
    def log_email_not_requested(reservation: Reservation, body: str) -> None:
        usable = extract_usable_invoice_emails(body)
        if not usable:
            return
        if is_waiting_for_invoice_email(reservation):
            return
        logger.info(
            "invoice_email_not_requested reservation_id=%s count=%s",
            reservation.pk,
            len(usable),
            extra={
                "event": "invoice_email_not_requested",
                "reservation_id": reservation.pk,
                "email_count": len(usable),
            },
        )
