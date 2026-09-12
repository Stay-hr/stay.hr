"""Apply a READY BillingRecipient onto a newly created Invoice. No issue/checkout caller."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.billing.exceptions import BillingRecipientError
from apps.billing.models import BillingRecipient, Invoice
from apps.billing.services.billing_recipient import (
    RecipientRejectReason,
    RecipientStatus,
    can_mark_applied,
    snapshot_recipient_onto_invoice,
)

FORBIDDEN_INVOICE_BUYER_FIELDS: frozenset[str] = frozenset(
    {
        "buyer_name",
        "buyer_document_number",
        "buyer_address",
        "buyer_country",
    }
)


def _reservation_id(value: Any) -> int | None:
    if value is None:
        return None
    pk = getattr(value, "pk", value)
    try:
        return int(pk)
    except (TypeError, ValueError):
        return None


def apply_recipient_to_new_invoice(
    *,
    recipient: BillingRecipient,
    invoice_create_kwargs: dict[str, Any],
) -> Invoice:
    forbidden = sorted(FORBIDDEN_INVOICE_BUYER_FIELDS.intersection(invoice_create_kwargs))
    if forbidden:
        raise BillingRecipientError(
            "Invoice buyer fields come only from snapshot_recipient_onto_invoice.",
            reason=RecipientRejectReason.FORBIDDEN_BUYER_FIELDS,
        )
    reservation_id = _reservation_id(invoice_create_kwargs.get("reservation"))
    if reservation_id is None:
        raise BillingRecipientError(
            "invoice_create_kwargs['reservation'] must match the recipient reservation.",
            reason=RecipientRejectReason.RESERVATION_MISMATCH,
        )
    if not recipient.pk:
        raise BillingRecipientError(
            "Only a persisted READY billing recipient can be applied.",
            reason=RecipientRejectReason.NOT_READY,
        )

    with transaction.atomic():
        locked = (
            BillingRecipient.objects.select_for_update()
            .select_related("reservation")
            .get(pk=recipient.pk)
        )
        reject = can_mark_applied(
            current=RecipientStatus(locked.status),
            invoice_already_persisted=False,
        )
        if reject is not None:
            raise BillingRecipientError(
                "Billing recipient cannot be applied.",
                reason=reject,
            )
        if locked.reservation_id != reservation_id:
            raise BillingRecipientError(
                "Invoice reservation must match the billing recipient reservation.",
                reason=RecipientRejectReason.RESERVATION_MISMATCH,
            )
        if Invoice.objects.filter(reservation_id=locked.reservation_id).exists():
            raise BillingRecipientError(
                "Apply cannot target an already persisted invoice.",
                reason=RecipientRejectReason.INVOICE_ALREADY_PERSISTED,
            )

        snapshot = snapshot_recipient_onto_invoice(recipient=locked)
        create_kwargs = dict(invoice_create_kwargs)
        create_kwargs.update(asdict(snapshot))
        if "tenant" not in create_kwargs and "tenant_id" not in create_kwargs:
            create_kwargs["tenant_id"] = locked.tenant_id

        invoice = Invoice.objects.create(**create_kwargs)

        locked.status = BillingRecipient.Status.APPLIED
        locked.applied_invoice = invoice
        locked.applied_at = timezone.now()
        locked.save(allow_apply=True)
        return invoice
