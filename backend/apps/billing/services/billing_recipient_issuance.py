"""Decide how a reservation invoice may be issued. Read-only; no create/apply."""

from __future__ import annotations

from apps.billing.models import BillingRecipient, Invoice
from apps.billing.services.billing_recipient import BillingRecipientIssuanceDecision
from apps.reservations.models import Reservation

OPEN_STATUSES: tuple[str, ...] = (
    BillingRecipient.Status.REQUESTED,
    BillingRecipient.Status.READY,
)


def resolve_billing_recipient_issuance(
    reservation: Reservation,
) -> BillingRecipientIssuanceDecision:
    """Return the issuance path. Existing invoices are never blocked or re-applied."""
    tenant_id = reservation.tenant_id
    if Invoice.objects.filter(reservation=reservation, tenant_id=tenant_id).exists():
        return BillingRecipientIssuanceDecision.GUEST

    open_row = (
        BillingRecipient.objects.filter(
            reservation=reservation,
            tenant_id=tenant_id,
            status__in=OPEN_STATUSES,
        )
        .only("status")
        .first()
    )
    if open_row is None:
        return BillingRecipientIssuanceDecision.GUEST
    if open_row.status == BillingRecipient.Status.REQUESTED:
        return BillingRecipientIssuanceDecision.BLOCK_REQUESTED
    return BillingRecipientIssuanceDecision.APPLY_READY
