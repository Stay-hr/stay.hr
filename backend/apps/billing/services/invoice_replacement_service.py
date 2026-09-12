"""Replacement case commands. No Invoice writes (ADR 0022).

Lock order is Reservation → InvoiceReplacement → recipient.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.billing.exceptions import InvoiceReplacementError
from apps.billing.models import Invoice, InvoiceReplacement, InvoiceReplacementRecipient
from apps.billing.services.billing_recipient import RecipientSource
from apps.billing.services.fiscal_routing import BuyerStatusConfidence
from apps.billing.services.invoice_replacement import (
    ReplacementCaseStatus,
    ReplacementRecipientDraft,
    ReplacementRejectReason,
    can_cancel,
    can_edit_recipient,
    can_open_case,
    identity_changed,
    is_structurally_ready,
)
from apps.billing.services.invoice_resolution import resolve_effective_invoice
from apps.reservations.models import Reservation

EDITABLE_RECIPIENT_FIELDS: frozenset[str] = frozenset(
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
PROVENANCE_FIELDS: frozenset[str] = frozenset({"source", "source_ref", "source_excerpt"})
COUNTRY_FIELDS: frozenset[str] = frozenset({"tax_id_country", "country"})


def _raise(reason: ReplacementRejectReason, message: str) -> None:
    raise InvoiceReplacementError(message, reason=reason.value)


def _valid_oib(value: str) -> bool:
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    return len(digits) == 11 and digits == (value or "").strip()


def _normalize_country(code: str) -> str:
    text = (code or "").strip()
    if not text:
        return ""
    normalized = text.upper()
    if len(normalized) == 2 and normalized.isalpha():
        return normalized
    _raise(ReplacementRejectReason.INVALID_COUNTRY, f"{code!r} is not ISO 3166-1 alpha-2.")
    raise AssertionError("unreachable")


def _normalize_recipient_value(name: str, value: Any) -> str:
    text = "" if value is None else str(value).strip()
    if name in COUNTRY_FIELDS:
        return _normalize_country(text)
    if name == "source" and text:
        try:
            return InvoiceReplacementRecipient.Source(text).value
        except ValueError:
            _raise(ReplacementRejectReason.INVALID_RECIPIENT, "Invalid recipient source.")
    return text


def _values_from_recipient(row: InvoiceReplacementRecipient) -> dict[str, str]:
    return {name: getattr(row, name) or "" for name in EDITABLE_RECIPIENT_FIELDS}


def _merge_recipient_fields(
    base: dict[str, str],
    fields: dict[str, Any],
) -> dict[str, str]:
    merged = dict(base)
    for name, value in fields.items():
        if name not in EDITABLE_RECIPIENT_FIELDS:
            continue
        merged[name] = _normalize_recipient_value(name, value)
    return merged


def _draft_from_values(values: dict[str, str]) -> ReplacementRecipientDraft:
    source_raw = values.get("source") or ""
    source = RecipientSource(source_raw) if source_raw else None
    return ReplacementRecipientDraft(
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


def _same_scope(original: Invoice, reservation: Reservation) -> bool:
    return (
        original.reservation_id == reservation.pk
        and original.tenant_id == reservation.tenant_id
    )


def _is_storno(original: Invoice, reservation: Reservation) -> bool:
    return InvoiceReplacement.objects.filter(
        reservation_id=reservation.pk,
        tenant_id=reservation.tenant_id,
        storno_invoice_id=original.pk,
    ).exists()


def _lock_open_case(reservation: Reservation) -> InvoiceReplacement | None:
    return (
        InvoiceReplacement.objects.select_for_update()
        .select_related("original_invoice")
        .filter(
            reservation_id=reservation.pk,
            tenant_id=reservation.tenant_id,
            status=InvoiceReplacement.Status.OPEN,
        )
        .first()
    )


def _lock_case(case: InvoiceReplacement) -> InvoiceReplacement:
    return (
        InvoiceReplacement.objects.select_for_update()
        .select_related("original_invoice", "reservation")
        .get(pk=case.pk)
    )


def _lock_recipient(case: InvoiceReplacement) -> InvoiceReplacementRecipient | None:
    return (
        InvoiceReplacementRecipient.objects.select_for_update()
        .filter(case_id=case.pk)
        .first()
    )


def _resolve_issuer_evidence(
    *,
    original: Invoice,
    original_issuer_oib: str,
    original_issuer_oib_source: str,
) -> tuple[str, str]:
    frozen_oib = (original.issuer_oib or "").strip()
    provided_oib = (original_issuer_oib or "").strip()
    provided_source = (original_issuer_oib_source or "").strip()
    if frozen_oib:
        if provided_oib and provided_oib != frozen_oib:
            _raise(
                ReplacementRejectReason.ISSUER_OIB_MISMATCH,
                "Provided issuer OIB does not match the frozen original issuer OIB.",
            )
        oib = frozen_oib
        source = provided_source or "invoice.issuer_oib"
    else:
        if not provided_oib or not provided_source:
            _raise(
                ReplacementRejectReason.ISSUER_OIB_EVIDENCE_REQUIRED,
                "Legacy original requires write-once issuer OIB evidence.",
            )
        oib = provided_oib
        source = provided_source
    if not _valid_oib(oib):
        _raise(ReplacementRejectReason.ISSUER_OIB_INVALID, "Issuer OIB must be 11 digits.")
    return oib, source


def _create_empty_recipient(case: InvoiceReplacement) -> InvoiceReplacementRecipient:
    recipient = InvoiceReplacementRecipient(
        tenant_id=case.tenant_id,
        case=case,
    )
    recipient.save()
    return recipient


def open_replacement_case(
    *,
    original: Invoice,
    actor,
    reason: str,
    original_issuer_oib: str = "",
    original_issuer_oib_source: str = "",
) -> InvoiceReplacement:
    reason_text = (reason or "").strip()
    if not reason_text:
        _raise(ReplacementRejectReason.CASE_REASON_REQUIRED, "Opening a case requires a reason.")
    if actor is None or getattr(actor, "pk", None) is None:
        _raise(ReplacementRejectReason.INVALID_RECIPIENT, "Opening a case requires an actor.")

    try:
        with transaction.atomic():
            reservation = Reservation.objects.select_for_update().get(pk=original.reservation_id)
            original = Invoice.objects.get(pk=original.pk)
            existing = _lock_open_case(reservation)
            if existing is not None:
                if existing.original_invoice_id == original.pk:
                    return existing
                _raise(
                    ReplacementRejectReason.OPEN_CASE_EXISTS,
                    "Reservation already has an OPEN replacement case.",
                )

            effective = resolve_effective_invoice(reservation)
            reject = can_open_case(
                candidate_is_effective=effective is not None and effective.pk == original.pk,
                candidate_is_storno=_is_storno(original, reservation),
                same_scope=_same_scope(original, reservation),
                open_case_exists=False,
            )
            if reject is not None:
                _raise(reject, "Cannot open a replacement case for this invoice.")

            oib, source = _resolve_issuer_evidence(
                original=original,
                original_issuer_oib=original_issuer_oib,
                original_issuer_oib_source=original_issuer_oib_source,
            )
            now = timezone.now()
            case = InvoiceReplacement(
                tenant_id=reservation.tenant_id,
                reservation=reservation,
                original_invoice=original,
                reason=reason_text,
                opened_by=actor,
                opened_at=now,
                original_issuer_oib=oib,
                original_issuer_oib_source=source,
                original_issuer_oib_recorded_by=actor,
                original_issuer_oib_recorded_at=now,
            )
            case.save()
            _create_empty_recipient(case)
            return case
    except IntegrityError as exc:
        existing = InvoiceReplacement.objects.filter(
            reservation_id=original.reservation_id,
            status=InvoiceReplacement.Status.OPEN,
        ).first()
        if existing is not None and existing.original_invoice_id == original.pk:
            return existing
        raise InvoiceReplacementError(
            "Reservation already has an OPEN replacement case.",
            reason=ReplacementRejectReason.OPEN_CASE_EXISTS.value,
        ) from exc


def update_replacement_recipient(
    *,
    case: InvoiceReplacement,
    fields: dict[str, Any],
) -> InvoiceReplacementRecipient:
    with transaction.atomic():
        reservation = Reservation.objects.select_for_update().get(pk=case.reservation_id)
        locked_case = _lock_case(case)
        if locked_case.reservation_id != reservation.pk:
            _raise(ReplacementRejectReason.SCOPE_MISMATCH, "Case reservation mismatch.")
        reject = can_edit_recipient(
            status=ReplacementCaseStatus(locked_case.status),
            storno_invoice_id=locked_case.storno_invoice_id,
        )
        if reject is not None:
            _raise(reject, "Replacement recipient cannot be edited.")

        locked = _lock_recipient(locked_case)
        if locked is None:
            locked = _create_empty_recipient(locked_case)

        before = locked.as_draft()
        values = _merge_recipient_fields(_values_from_recipient(locked), fields)
        for name in PROVENANCE_FIELDS:
            old = getattr(locked, name) or ""
            new = values[name]
            if old and new != old:
                _raise(
                    ReplacementRejectReason.INVALID_RECIPIENT,
                    f"{name} is write-once.",
                )
        after = _draft_from_values(values)
        for name, value in values.items():
            setattr(locked, name, value)
        if identity_changed(before, after):
            locked.identity_confidence = InvoiceReplacementRecipient.IdentityConfidence.UNVERIFIED
            locked.verified_by = None
            locked.verified_at = None
        try:
            locked.save()
        except ValidationError as exc:
            raise InvoiceReplacementError(
                "Replacement recipient update is invalid.",
                reason=ReplacementRejectReason.INVALID_RECIPIENT.value,
            ) from exc
        return locked


def verify_replacement_recipient(
    *,
    case: InvoiceReplacement,
    actor,
) -> InvoiceReplacementRecipient:
    if actor is None or getattr(actor, "pk", None) is None:
        _raise(ReplacementRejectReason.INVALID_RECIPIENT, "Verify requires an actor.")
    with transaction.atomic():
        reservation = Reservation.objects.select_for_update().get(pk=case.reservation_id)
        locked_case = _lock_case(case)
        if locked_case.reservation_id != reservation.pk:
            _raise(ReplacementRejectReason.SCOPE_MISMATCH, "Case reservation mismatch.")
        reject = can_edit_recipient(
            status=ReplacementCaseStatus(locked_case.status),
            storno_invoice_id=locked_case.storno_invoice_id,
        )
        if reject is not None:
            _raise(reject, "Replacement recipient cannot be verified.")
        locked = _lock_recipient(locked_case)
        if locked is None or not is_structurally_ready(locked.as_draft()):
            _raise(
                ReplacementRejectReason.RECIPIENT_NOT_READY,
                "Recipient must be structurally READY before verify.",
            )
        if locked.identity_confidence == InvoiceReplacementRecipient.IdentityConfidence.VERIFIED:
            return locked
        locked.identity_confidence = InvoiceReplacementRecipient.IdentityConfidence.VERIFIED
        locked.verified_by = actor
        locked.verified_at = timezone.now()
        try:
            locked.save()
        except ValidationError as exc:
            raise InvoiceReplacementError(
                "Replacement recipient verify is invalid.",
                reason=ReplacementRejectReason.INVALID_RECIPIENT.value,
            ) from exc
        return locked


def cancel_replacement_case(
    *,
    case: InvoiceReplacement,
    actor,
    cancel_reason: str,
) -> InvoiceReplacement:
    reason_text = (cancel_reason or "").strip()
    if not reason_text:
        _raise(
            ReplacementRejectReason.CANCEL_REASON_REQUIRED,
            "Cancel requires a reason.",
        )
    if actor is None or getattr(actor, "pk", None) is None:
        _raise(ReplacementRejectReason.INVALID_RECIPIENT, "Cancel requires an actor.")
    with transaction.atomic():
        reservation = Reservation.objects.select_for_update().get(pk=case.reservation_id)
        locked_case = _lock_case(case)
        if locked_case.reservation_id != reservation.pk:
            _raise(ReplacementRejectReason.SCOPE_MISMATCH, "Case reservation mismatch.")
        reject = can_cancel(
            status=ReplacementCaseStatus(locked_case.status),
            storno_invoice_id=locked_case.storno_invoice_id,
            replacement_invoice_id=locked_case.replacement_invoice_id,
        )
        if reject is not None:
            _raise(reject, "Replacement case cannot be cancelled.")
        locked_case.status = InvoiceReplacement.Status.CANCELLED
        locked_case.cancelled_by = actor
        locked_case.cancelled_at = timezone.now()
        locked_case.cancel_reason = reason_text
        try:
            locked_case.save()
        except ValidationError as exc:
            raise InvoiceReplacementError(
                "Replacement case cancel is invalid.",
                reason=ReplacementRejectReason.INVALID_RECIPIENT.value,
            ) from exc
        return locked_case
