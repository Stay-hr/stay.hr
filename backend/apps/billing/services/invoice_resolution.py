from __future__ import annotations

from apps.billing.exceptions import InvoiceGraphError
from apps.billing.models import Invoice
from apps.reservations.models import Reservation


def resolve_effective_invoice(reservation: Reservation) -> Invoice | None:
    """Return the reservation's effective invoice, or None if none exists.

    Bootstrap (OneToOne still in place): cardinality 0/1 is the only legal
    shape. More than one row is fail-closed. This count heuristic is not the
    domain algorithm and must be replaced by an InvoiceReplacement walk
    before a second invoice can be created.
    """
    invoices = list(
        Invoice.objects.filter(
            reservation_id=reservation.pk,
            tenant_id=reservation.tenant_id,
        )
    )
    if not invoices:
        return None
    if len(invoices) == 1:
        return invoices[0]
    raise InvoiceGraphError(
        "Reservation has more than one invoice; effective invoice is ambiguous."
    )
