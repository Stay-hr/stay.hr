"""Create booking offer snapshot + PDF."""

from __future__ import annotations

from datetime import date

from django.db import transaction
from django.utils import timezone

from apps.billing.exceptions import FiscalConfigError, InvoiceBuildError
from apps.billing.models import BookingOffer, TenantFiscalSettings
from apps.billing.services.offer_builder import build_offer_snapshot
from apps.billing.services.offer_pdf import render_offer_pdf
from apps.reservations.models import Reservation


def _parse_valid_until(snapshot: dict) -> date | None:
    raw = snapshot.get("valid_until")
    if not raw:
        return None
    return date.fromisoformat(str(raw))


@transaction.atomic
def issue_booking_offer(reservation: Reservation) -> BookingOffer:
    """Create immutable offer for reservation (idempotent — returns existing)."""
    existing = getattr(reservation, "booking_offer", None)
    if existing is not None:
        return existing

    settings = TenantFiscalSettings.objects.filter(tenant_id=reservation.tenant_id).first()
    if settings is None:
        raise FiscalConfigError("Fiscal settings are not configured.")

    snapshot = build_offer_snapshot(reservation, settings)
    offer = BookingOffer.objects.create(
        tenant_id=reservation.tenant_id,
        reservation=reservation,
        offer_number=snapshot["offer_number"],
        issued_at=timezone.now(),
        valid_until=_parse_valid_until(snapshot),
        snapshot=snapshot,
    )
    render_offer_pdf(offer)
    return offer
