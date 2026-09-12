"""BillingRecipient lifecycle contract (ADR 0021).

Pure functions: no Django, no I/O, no persistence. The ORM model is a later slice.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from apps.billing.services.fiscal_routing import BuyerStatusConfidence


class RecipientStatus(StrEnum):
    REQUESTED = "requested"
    READY = "ready"
    APPLIED = "applied"


class RecipientSource(StrEnum):
    BOOKING_MESSAGE = "booking_message"
    WHATSAPP = "whatsapp"
    GUEST_FORM = "guest_form"
    STAFF = "staff"


class RecipientRejectReason(StrEnum):
    EMPTY_REQUEST = "empty_request"
    OPEN_ALREADY_EXISTS = "open_already_exists"
    ALREADY_APPLIED = "already_applied"
    SKIP_TO_APPLIED = "skip_to_applied"
    NOT_READY = "not_ready"
    INVOICE_ALREADY_PERSISTED = "invoice_already_persisted"
    STRUCTURALLY_INCOMPLETE = "structurally_incomplete"
    INVALID_COUNTRY = "invalid_country"
    INVALID_HR_TAX_ID = "invalid_hr_tax_id"


ALLOWED_TRANSITIONS: frozenset[tuple[RecipientStatus, RecipientStatus]] = frozenset(
    {
        (RecipientStatus.REQUESTED, RecipientStatus.READY),
        (RecipientStatus.READY, RecipientStatus.REQUESTED),
        (RecipientStatus.READY, RecipientStatus.APPLIED),
    }
)

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


@dataclass(frozen=True)
class BillingRecipientDraft:
    """Field contract for a future BillingRecipient row."""

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


def _strip(value: str) -> str:
    return (value or "").strip()


def normalize_country(code: str) -> str | None:
    normalized = _strip(code).upper()
    if len(normalized) == 2 and normalized.isalpha():
        return normalized
    return None


def has_request_anchor(draft: BillingRecipientDraft) -> bool:
    return bool(
        _strip(draft.company_name)
        or _strip(draft.tax_id)
        or _strip(draft.source_excerpt)
    )


def ready_missing_fields(draft: BillingRecipientDraft) -> list[str]:
    missing: list[str] = []
    for name in READY_REQUIRED_FIELDS:
        if not _strip(getattr(draft, name)):
            missing.append(name)
    tax_country = normalize_country(draft.tax_id_country)
    seat = normalize_country(draft.country)
    if _strip(draft.tax_id_country) and tax_country is None:
        missing.append("tax_id_country")
    if _strip(draft.country) and seat is None:
        missing.append("country")
    if tax_country == "HR":
        digits = "".join(ch for ch in _strip(draft.tax_id) if ch.isdigit())
        if len(digits) != 11:
            missing.append("tax_id")
    return list(dict.fromkeys(missing))


def is_structurally_ready(draft: BillingRecipientDraft) -> bool:
    return not ready_missing_fields(draft)


def can_transition(current: RecipientStatus, target: RecipientStatus) -> bool:
    if current is target:
        return True
    return (current, target) in ALLOWED_TRANSITIONS


def next_status_for_fields(
    current: RecipientStatus,
    draft: BillingRecipientDraft,
) -> RecipientStatus | None:
    """Status after an edit. APPLIED is frozen. None = reject the edit."""
    if current is RecipientStatus.APPLIED:
        return None
    if is_structurally_ready(draft):
        return RecipientStatus.READY
    return RecipientStatus.REQUESTED


def can_mark_applied(
    *,
    current: RecipientStatus,
    invoice_already_persisted: bool,
) -> RecipientRejectReason | None:
    """None means the transition is allowed. APPLY never targets an existing invoice."""
    if current is RecipientStatus.APPLIED:
        return RecipientRejectReason.ALREADY_APPLIED
    if current is RecipientStatus.REQUESTED:
        return RecipientRejectReason.SKIP_TO_APPLIED
    if current is not RecipientStatus.READY:
        return RecipientRejectReason.NOT_READY
    if invoice_already_persisted:
        return RecipientRejectReason.INVOICE_ALREADY_PERSISTED
    return None
