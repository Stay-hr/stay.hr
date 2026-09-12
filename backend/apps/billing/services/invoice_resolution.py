from __future__ import annotations

from collections import Counter

from apps.billing.exceptions import InvoiceGraphError
from apps.billing.models import Invoice, InvoiceReplacement
from apps.reservations.models import Reservation


def has_open_post_storno_gap(reservation: Reservation) -> bool:
    """True when an OPEN replacement case has already issued its storno."""
    return InvoiceReplacement.objects.filter(
        reservation_id=reservation.pk,
        tenant_id=reservation.tenant_id,
        status=InvoiceReplacement.Status.OPEN,
        storno_invoice__isnull=False,
    ).exists()


def resolve_effective_invoice(reservation: Reservation) -> Invoice | None:
    """Return the current effective positive invoice, or None.

    Walks InvoiceReplacement links. Invoice cardinality is not an invariant.
    Never uses latest/last, newest, or highest sequence. Never returns a storno.
    OPEN + storno is the replacement gap (None). Graph corruption is fail-closed.
    """
    invoices = list(
        Invoice.objects.filter(
            reservation_id=reservation.pk,
            tenant_id=reservation.tenant_id,
        )
    )
    cases = list(
        InvoiceReplacement.objects.filter(
            reservation_id=reservation.pk,
            tenant_id=reservation.tenant_id,
        )
    )
    return _resolve_from_graph(invoices, cases)


def _resolve_from_graph(
    invoices: list[Invoice],
    cases: list[InvoiceReplacement],
) -> Invoice | None:
    if not cases:
        if not invoices:
            return None
        if len(invoices) == 1:
            return invoices[0]
        raise InvoiceGraphError(
            "Reservation has more than one invoice without replacement history."
        )

    invoices_by_id = {invoice.pk: invoice for invoice in invoices}
    _validate_replacement_graph(invoices_by_id, cases)

    storno_ids = {
        case.storno_invoice_id for case in cases if case.storno_invoice_id is not None
    }
    superseded_ids = {
        case.original_invoice_id
        for case in cases
        if case.status == InvoiceReplacement.Status.COMPLETED
    }
    open_with_storno = [
        case
        for case in cases
        if case.status == InvoiceReplacement.Status.OPEN and case.storno_invoice_id
    ]
    if open_with_storno:
        return None

    current = [
        invoice
        for invoice in invoices
        if invoice.pk not in storno_ids and invoice.pk not in superseded_ids
    ]
    if len(current) == 1:
        return current[0]
    if not current and not invoices:
        return None
    raise InvoiceGraphError(
        "Replacement graph has no unique current effective invoice."
    )


def _validate_replacement_graph(
    invoices_by_id: dict[int, Invoice],
    cases: list[InvoiceReplacement],
) -> None:
    open_cases = [case for case in cases if case.status == InvoiceReplacement.Status.OPEN]
    if len(open_cases) > 1:
        raise InvoiceGraphError("Reservation has more than one OPEN replacement case.")

    originals = [
        case.original_invoice_id
        for case in cases
        if case.status
        in (InvoiceReplacement.Status.OPEN, InvoiceReplacement.Status.COMPLETED)
    ]
    if any(count > 1 for count in Counter(originals).values()):
        raise InvoiceGraphError("The same original has more than one OPEN or COMPLETED case.")

    original_ids: set[int] = set()
    storno_ids: set[int] = set()
    replacement_ids: set[int] = set()
    successor: dict[int, int] = {}

    for case in cases:
        _validate_case_row(case, invoices_by_id)
        original_ids.add(case.original_invoice_id)
        if case.storno_invoice_id:
            storno_ids.add(case.storno_invoice_id)
        if case.replacement_invoice_id:
            replacement_ids.add(case.replacement_invoice_id)
        if (
            case.status == InvoiceReplacement.Status.COMPLETED
            and case.replacement_invoice_id
        ):
            successor[case.original_invoice_id] = case.replacement_invoice_id

    if storno_ids & original_ids or storno_ids & replacement_ids:
        raise InvoiceGraphError("A storno invoice cannot also be original or replacement.")

    explained = original_ids | storno_ids | replacement_ids
    unexplained = set(invoices_by_id) - explained
    if unexplained:
        raise InvoiceGraphError("Reservation has an invoice outside the replacement graph.")

    _assert_no_cycles(successor)


def _validate_case_row(
    case: InvoiceReplacement,
    invoices_by_id: dict[int, Invoice],
) -> None:
    for field_name in ("original_invoice_id", "storno_invoice_id", "replacement_invoice_id"):
        invoice_id = getattr(case, field_name)
        if invoice_id is None:
            continue
        if invoice_id not in invoices_by_id:
            raise InvoiceGraphError(
                f"{field_name} does not belong to the reservation invoice set."
            )

    if case.storno_invoice_id and case.storno_invoice_id == case.original_invoice_id:
        raise InvoiceGraphError("storno_invoice cannot equal original_invoice.")
    if (
        case.replacement_invoice_id
        and case.replacement_invoice_id == case.original_invoice_id
    ):
        raise InvoiceGraphError("replacement_invoice cannot equal original_invoice.")
    if (
        case.storno_invoice_id
        and case.replacement_invoice_id
        and case.storno_invoice_id == case.replacement_invoice_id
    ):
        raise InvoiceGraphError("storno_invoice cannot equal replacement_invoice.")

    if case.status == InvoiceReplacement.Status.COMPLETED:
        if not case.storno_invoice_id or not case.replacement_invoice_id:
            raise InvoiceGraphError("COMPLETED case must have storno and replacement.")
    if case.status == InvoiceReplacement.Status.CANCELLED:
        if case.storno_invoice_id or case.replacement_invoice_id:
            raise InvoiceGraphError("CANCELLED case must not have storno or replacement.")


def _assert_no_cycles(successor: dict[int, int]) -> None:
    for start in successor:
        seen: set[int] = set()
        node: int | None = start
        while node in successor:
            if node in seen:
                raise InvoiceGraphError("Replacement graph contains a cycle.")
            seen.add(node)
            node = successor[node]
