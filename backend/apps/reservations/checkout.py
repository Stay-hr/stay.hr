from __future__ import annotations

import logging

from apps.integrations.evisitor.exceptions import (
    EvisitorApiError,
    EvisitorConfigError,
    EvisitorValidationError,
)
from apps.integrations.evisitor.service import checkout_reservation_guests_in_evisitor
from apps.integrations.evisitor.summary import evisitor_summary_for_reservation
from apps.reservations.guest_slots import remove_unfilled_secondary_guests
from apps.reservations.models import Reservation

logger = logging.getLogger(__name__)


class CheckoutBlockedError(Exception):
    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        failed_guests: list[dict] | None = None,
    ):
        self.code = code
        self.failed_guests = list(failed_guests or [])
        super().__init__(message or code)


def perform_reservation_checkout(
    reservation: Reservation,
    *,
    source: str = "manual",
) -> None:
    del source  # reserved for audit/logging

    if reservation.status != Reservation.Status.CHECKED_IN:
        raise CheckoutBlockedError(
            "invalid_status",
            f"Reservation status is {reservation.status}, expected checked_in.",
        )

    remove_unfilled_secondary_guests(reservation)

    summary = evisitor_summary_for_reservation(reservation)
    if summary == "none":
        raise CheckoutBlockedError(
            "evisitor_none",
            "Checkout blocked: reservation has no guests.",
        )
    if summary != "complete":
        raise CheckoutBlockedError(
            "evisitor_incomplete",
            "Checkout blocked: eVisitor registration incomplete.",
        )

    try:
        checkout_reservation_guests_in_evisitor(reservation)
    except EvisitorApiError as exc:
        raise CheckoutBlockedError(
            "evisitor_checkout_failed",
            getattr(exc, "user_message", "") or str(exc),
            failed_guests=getattr(exc, "failed_guests", None),
        ) from exc
    except (EvisitorValidationError, EvisitorConfigError) as exc:
        raise CheckoutBlockedError(
            "evisitor_checkout_failed",
            str(exc),
        ) from exc

    from apps.billing.exceptions import FiscalConfigError, InvoiceBuildError
    from apps.billing.services.issue import issue_guest_invoice, should_issue_invoice_on_checkout
    from apps.billing.tasks import fiscalize_invoice, send_invoice_email_task
    from apps.communications.invoice_email import resolve_invoice_recipient

    if should_issue_invoice_on_checkout(reservation):
        try:
            invoice = issue_guest_invoice(reservation)
            fiscalize_invoice.delay(invoice.pk)
            if resolve_invoice_recipient(reservation):
                send_invoice_email_task.delay(invoice.pk)
        except FiscalConfigError as exc:
            raise CheckoutBlockedError("fiscal_config_incomplete", str(exc)) from exc
        except InvoiceBuildError as exc:
            logger.warning(
                "Checkout invoice build failed reservation_id=%s: %s",
                reservation.pk,
                exc,
            )

    reservation.status = Reservation.Status.CHECKED_OUT
    reservation.save(update_fields=["status", "updated_at"])
