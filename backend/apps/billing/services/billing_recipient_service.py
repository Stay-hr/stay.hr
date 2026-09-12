"""Persist open BillingRecipient rows. Does not apply, issue, or touch invoices."""

from __future__ import annotations

from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.billing.exceptions import BillingRecipientError
from apps.billing.models import BillingRecipient
from apps.billing.services.billing_recipient import (
    BillingRecipientDraft,
    BuyerStatusConfidence,
    RecipientRejectReason,
    RecipientSource,
    RecipientStatus,
    has_request_anchor,
    next_status_for_fields,
    normalize_country,
)
from apps.reservations.models import Reservation

EDITABLE_FIELDS: frozenset[str] = frozenset(
    {
        "company_name",
        "tax_id",
        "tax_id_country",
        "country",
        "address",
        "postal_code",
        "city",
        "email",
        "phone",
        "source",
        "source_ref",
        "source_excerpt",
    }
)

OPEN_STATUSES: tuple[str, ...] = (
    BillingRecipient.Status.REQUESTED,
    BillingRecipient.Status.READY,
)


def _open_qs(reservation: Reservation):
    return BillingRecipient.objects.filter(
        reservation=reservation,
        status__in=OPEN_STATUSES,
    )


def _normalize_value(name: str, value: Any) -> str:
    text = "" if value is None else str(value).strip()
    if name in {"tax_id_country", "country"}:
        if not text:
            return ""
        iso = normalize_country(text)
        if iso is None:
            raise BillingRecipientError(
                f"{name} must be ISO 3166-1 alpha-2.",
                reason=RecipientRejectReason.INVALID_COUNTRY,
            )
        return iso
    if name == "source" and text:
        return RecipientSource(text).value
    return text


def _draft_from_values(values: dict[str, str]) -> BillingRecipientDraft:
    source_raw = values.get("source") or ""
    source = RecipientSource(source_raw) if source_raw else None
    return BillingRecipientDraft(
        company_name=values.get("company_name", ""),
        tax_id=values.get("tax_id", ""),
        tax_id_country=values.get("tax_id_country", ""),
        country=values.get("country", ""),
        address=values.get("address", ""),
        postal_code=values.get("postal_code", ""),
        city=values.get("city", ""),
        email=values.get("email", ""),
        phone=values.get("phone", ""),
        identity_confidence=BuyerStatusConfidence.UNVERIFIED,
        source=source,
        source_ref=values.get("source_ref", ""),
        source_excerpt=values.get("source_excerpt", ""),
    )


def _values_from_row(row: BillingRecipient) -> dict[str, str]:
    return {name: getattr(row, name) or "" for name in EDITABLE_FIELDS}


def _merge_fields(base: dict[str, str], fields: dict[str, Any]) -> dict[str, str]:
    merged = dict(base)
    for name, value in fields.items():
        if name not in EDITABLE_FIELDS:
            continue
        merged[name] = _normalize_value(name, value)
    return merged


def _apply_open_state(row: BillingRecipient, values: dict[str, str], status: RecipientStatus) -> None:
    previous_status = row.status
    for name, value in values.items():
        setattr(row, name, value)
    row.identity_confidence = BillingRecipient.IdentityConfidence.UNVERIFIED
    row.status = status.value
    row.applied_invoice = None
    if status is RecipientStatus.READY:
        if previous_status != BillingRecipient.Status.READY or row.ready_at is None:
            row.ready_at = timezone.now()
    else:
        row.ready_at = None


def create_open_recipient(
    reservation: Reservation,
    fields: dict[str, Any],
    *,
    source: str = "",
    source_ref: str = "",
    source_excerpt: str = "",
) -> BillingRecipient:
    extras = dict(fields)
    if source:
        extras["source"] = source
    if source_ref:
        extras["source_ref"] = source_ref
    if source_excerpt:
        extras["source_excerpt"] = source_excerpt
    values = _merge_fields({name: "" for name in EDITABLE_FIELDS}, extras)
    draft = _draft_from_values(values)
    if not has_request_anchor(draft):
        raise BillingRecipientError(
            "A billing recipient needs company_name, tax_id, or source_excerpt.",
            reason=RecipientRejectReason.EMPTY_REQUEST,
        )
    if _open_qs(reservation).exists():
        raise BillingRecipientError(
            "Reservation already has an open billing recipient.",
            reason=RecipientRejectReason.OPEN_ALREADY_EXISTS,
        )

    status = next_status_for_fields(RecipientStatus.REQUESTED, draft)
    if status is None or status is RecipientStatus.APPLIED:
        raise BillingRecipientError(
            "Open recipient cannot enter APPLIED.",
            reason=RecipientRejectReason.SKIP_TO_APPLIED,
        )

    row = BillingRecipient(
        tenant_id=reservation.tenant_id,
        reservation=reservation,
    )
    _apply_open_state(row, values, status)
    try:
        with transaction.atomic():
            row.save()
    except IntegrityError as exc:
        raise BillingRecipientError(
            "Reservation already has an open billing recipient.",
            reason=RecipientRejectReason.OPEN_ALREADY_EXISTS,
        ) from exc
    return row


def update_open_recipient(
    recipient: BillingRecipient,
    fields: dict[str, Any],
) -> BillingRecipient:
    if recipient.status == BillingRecipient.Status.APPLIED:
        raise BillingRecipientError(
            "An APPLIED billing recipient is frozen.",
            reason=RecipientRejectReason.ALREADY_APPLIED,
        )
    values = _merge_fields(_values_from_row(recipient), fields)
    draft = _draft_from_values(values)
    if not has_request_anchor(draft):
        raise BillingRecipientError(
            "A billing recipient needs company_name, tax_id, or source_excerpt.",
            reason=RecipientRejectReason.EMPTY_REQUEST,
        )
    current = RecipientStatus(recipient.status)
    status = next_status_for_fields(current, draft)
    if status is None or status is RecipientStatus.APPLIED:
        raise BillingRecipientError(
            "Open recipient cannot enter APPLIED.",
            reason=RecipientRejectReason.SKIP_TO_APPLIED,
        )
    requested_at = recipient.requested_at
    _apply_open_state(recipient, values, status)
    recipient.requested_at = requested_at
    recipient.save()
    return recipient
