"""Invoice replacement / storno contract (ADR 0022).

Pure functions: no Django, no I/O, no persistence.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from apps.billing.services.billing_recipient import RecipientSource
from apps.billing.services.fiscal_routing import BuyerStatusConfidence


class ReplacementCaseStatus(StrEnum):
    OPEN = "open"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class InvoiceDocumentRole(StrEnum):
    STANDALONE = "standalone"
    ORIGINAL = "original"
    STORNO = "storno"
    REPLACEMENT = "replacement"


class ReplacementRejectReason(StrEnum):
    NOT_EFFECTIVE_ORIGINAL = "not_effective_original"
    CANDIDATE_IS_STORNO = "candidate_is_storno"
    SCOPE_MISMATCH = "scope_mismatch"
    OPEN_CASE_EXISTS = "open_case_exists"
    CASE_NOT_OPEN = "case_not_open"
    RECIPIENT_FROZEN = "recipient_frozen"
    RECIPIENT_NOT_READY = "recipient_not_ready"
    RECIPIENT_NOT_VERIFIED = "recipient_not_verified"
    STORNO_ALREADY_EXISTS = "storno_already_exists"
    STORNO_MISSING = "storno_missing"
    REPLACEMENT_ALREADY_EXISTS = "replacement_already_exists"
    CANCEL_AFTER_STORNO = "cancel_after_storno"
    CASE_REASON_REQUIRED = "case_reason_required"
    CANCEL_REASON_REQUIRED = "cancel_reason_required"
    ISSUER_OIB_EVIDENCE_REQUIRED = "issuer_oib_evidence_required"
    ISSUER_OIB_MISMATCH = "issuer_oib_mismatch"
    ISSUER_OIB_INVALID = "issuer_oib_invalid"
    INVALID_COUNTRY = "invalid_country"
    INVALID_RECIPIENT = "invalid_recipient"


READY_REQUIRED_FIELDS: tuple[str, ...] = (
    "company_name",
    "tax_id",
    "tax_id_country",
    "country",
    "address",
    "postal_code",
    "city",
    "email",
)

IDENTITY_FIELDS: tuple[str, ...] = READY_REQUIRED_FIELDS

ALLOWED_TRANSITIONS: frozenset[tuple[ReplacementCaseStatus, ReplacementCaseStatus]] = frozenset(
    {
        (ReplacementCaseStatus.OPEN, ReplacementCaseStatus.COMPLETED),
        (ReplacementCaseStatus.OPEN, ReplacementCaseStatus.CANCELLED),
    }
)


@dataclass(frozen=True)
class ReplacementRecipientDraft:
    company_name: str = ""
    tax_id: str = ""
    tax_id_country: str = ""
    country: str = ""
    address: str = ""
    postal_code: str = ""
    city: str = ""
    email: str = ""
    phone: str = ""
    identity_confidence: BuyerStatusConfidence = BuyerStatusConfidence.UNVERIFIED
    source: RecipientSource | None = None
    source_ref: str = ""
    source_excerpt: str = ""


@dataclass(frozen=True)
class InvoiceLineSnapshot:
    sort_order: int
    line_kind: str
    description: str
    quantity: Decimal
    unit_price: Decimal
    vat_rate: Decimal
    vat_amount: Decimal
    line_total: Decimal


@dataclass(frozen=True)
class InvoiceDocumentSnapshot:
    subtotal: Decimal
    vat_amount: Decimal
    total: Decimal
    currency: str
    payment_method: str
    payment_note: str
    buyer_name: str
    buyer_document_number: str
    buyer_address: str
    buyer_country: str
    lines: tuple[InvoiceLineSnapshot, ...]


def _strip(value: str) -> str:
    return (value or "").strip()


def _normalize_country(code: str) -> str | None:
    normalized = _strip(code).upper()
    if len(normalized) == 2 and normalized.isalpha():
        return normalized
    return None


def ready_missing_fields(draft: ReplacementRecipientDraft) -> list[str]:
    missing: list[str] = []
    for name in READY_REQUIRED_FIELDS:
        if not _strip(getattr(draft, name)):
            missing.append(name)
    tax_country = _normalize_country(draft.tax_id_country)
    seat = _normalize_country(draft.country)
    if _strip(draft.tax_id_country) and tax_country is None:
        missing.append("tax_id_country")
    if _strip(draft.country) and seat is None:
        missing.append("country")
    if tax_country == "HR":
        digits = "".join(ch for ch in _strip(draft.tax_id) if ch.isdigit())
        if len(digits) != 11:
            missing.append("tax_id")
    return list(dict.fromkeys(missing))


def is_structurally_ready(draft: ReplacementRecipientDraft) -> bool:
    return not ready_missing_fields(draft)


def identity_tuple(draft: ReplacementRecipientDraft) -> tuple[str, ...]:
    return tuple(_strip(getattr(draft, name)) for name in IDENTITY_FIELDS)


def identity_changed(
    before: ReplacementRecipientDraft,
    after: ReplacementRecipientDraft,
) -> bool:
    return identity_tuple(before) != identity_tuple(after)


def can_transition(
    current: ReplacementCaseStatus,
    target: ReplacementCaseStatus,
) -> bool:
    if current is target:
        return True
    return (current, target) in ALLOWED_TRANSITIONS


def negate_invoice_snapshot(snapshot: InvoiceDocumentSnapshot) -> InvoiceDocumentSnapshot:
    """Exact negative money snapshot. Buyer and payment stay identical."""
    return InvoiceDocumentSnapshot(
        subtotal=-snapshot.subtotal,
        vat_amount=-snapshot.vat_amount,
        total=-snapshot.total,
        currency=snapshot.currency,
        payment_method=snapshot.payment_method,
        payment_note=snapshot.payment_note,
        buyer_name=snapshot.buyer_name,
        buyer_document_number=snapshot.buyer_document_number,
        buyer_address=snapshot.buyer_address,
        buyer_country=snapshot.buyer_country,
        lines=tuple(
            InvoiceLineSnapshot(
                sort_order=line.sort_order,
                line_kind=line.line_kind,
                description=line.description,
                quantity=line.quantity,
                unit_price=-line.unit_price,
                vat_rate=line.vat_rate,
                vat_amount=-line.vat_amount,
                line_total=-line.line_total,
            )
            for line in snapshot.lines
        ),
    )


def mirror_invoice_snapshot(snapshot: InvoiceDocumentSnapshot) -> InvoiceDocumentSnapshot:
    """Exact positive economic snapshot. Buyer identity is applied separately."""
    return InvoiceDocumentSnapshot(
        subtotal=snapshot.subtotal,
        vat_amount=snapshot.vat_amount,
        total=snapshot.total,
        currency=snapshot.currency,
        payment_method=snapshot.payment_method,
        payment_note=snapshot.payment_note,
        buyer_name=snapshot.buyer_name,
        buyer_document_number=snapshot.buyer_document_number,
        buyer_address=snapshot.buyer_address,
        buyer_country=snapshot.buyer_country,
        lines=tuple(snapshot.lines),
    )


def derive_document_role(
    invoice_id: int,
    *,
    original_ids: frozenset[int],
    storno_ids: frozenset[int],
    replacement_ids: frozenset[int],
) -> InvoiceDocumentRole:
    """Role comes only from InvoiceReplacement links. Storno cannot share another role."""
    is_storno = invoice_id in storno_ids
    is_replacement = invoice_id in replacement_ids
    is_original = invoice_id in original_ids
    if is_storno and (is_replacement or is_original):
        raise ValueError("storno invoice cannot also be original or replacement")
    if is_storno:
        return InvoiceDocumentRole.STORNO
    if is_replacement:
        return InvoiceDocumentRole.REPLACEMENT
    if is_original:
        return InvoiceDocumentRole.ORIGINAL
    return InvoiceDocumentRole.STANDALONE


def can_open_case(
    *,
    candidate_is_effective: bool,
    candidate_is_storno: bool,
    same_scope: bool,
    open_case_exists: bool,
) -> ReplacementRejectReason | None:
    if not same_scope:
        return ReplacementRejectReason.SCOPE_MISMATCH
    if candidate_is_storno:
        return ReplacementRejectReason.CANDIDATE_IS_STORNO
    if not candidate_is_effective:
        return ReplacementRejectReason.NOT_EFFECTIVE_ORIGINAL
    if open_case_exists:
        return ReplacementRejectReason.OPEN_CASE_EXISTS
    return None


def can_edit_recipient(
    *,
    status: ReplacementCaseStatus,
    storno_invoice_id: int | None,
) -> ReplacementRejectReason | None:
    if status is not ReplacementCaseStatus.OPEN:
        return ReplacementRejectReason.CASE_NOT_OPEN
    if storno_invoice_id is not None:
        return ReplacementRejectReason.RECIPIENT_FROZEN
    return None


def expected_issuer_oib(*, original_issuer_oib: str, recorded_issuer_oib: str) -> str:
    """Frozen original OIB wins; legacy originals use write-once case evidence."""
    frozen = (original_issuer_oib or "").strip()
    if frozen:
        return frozen
    return (recorded_issuer_oib or "").strip()


def can_issue_storno(
    *,
    status: ReplacementCaseStatus,
    storno_invoice_id: int | None,
    recipient_ready: bool,
    recipient_verified: bool,
) -> ReplacementRejectReason | None:
    if status is not ReplacementCaseStatus.OPEN:
        return ReplacementRejectReason.CASE_NOT_OPEN
    if storno_invoice_id is not None:
        return ReplacementRejectReason.STORNO_ALREADY_EXISTS
    if not recipient_ready:
        return ReplacementRejectReason.RECIPIENT_NOT_READY
    if not recipient_verified:
        return ReplacementRejectReason.RECIPIENT_NOT_VERIFIED
    return None


def can_complete(
    *,
    status: ReplacementCaseStatus,
    storno_invoice_id: int | None,
    replacement_invoice_id: int | None,
) -> ReplacementRejectReason | None:
    if status is not ReplacementCaseStatus.OPEN:
        return ReplacementRejectReason.CASE_NOT_OPEN
    if storno_invoice_id is None:
        return ReplacementRejectReason.STORNO_MISSING
    if replacement_invoice_id is not None:
        return ReplacementRejectReason.REPLACEMENT_ALREADY_EXISTS
    return None


def can_cancel(
    *,
    status: ReplacementCaseStatus,
    storno_invoice_id: int | None,
    replacement_invoice_id: int | None,
) -> ReplacementRejectReason | None:
    if status is not ReplacementCaseStatus.OPEN:
        return ReplacementRejectReason.CASE_NOT_OPEN
    if storno_invoice_id is not None or replacement_invoice_id is not None:
        return ReplacementRejectReason.CANCEL_AFTER_STORNO
    return None
