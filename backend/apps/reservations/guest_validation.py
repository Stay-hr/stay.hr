"""Guest field validation for web check-in wizard (separate from eVisitor mapper)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from apps.integrations.evisitor.residence_address import validate_evisitor_residence_address
from apps.reservations.models import Guest


class SlotReadinessStatus(str, Enum):
    PARTIAL = "partial"
    READY = "ready"


@dataclass(frozen=True)
class SlotValidationResult:
    position: int
    guest_id: int
    status: SlotReadinessStatus
    missing_fields: tuple[str, ...]


def _has_text(value: str | None) -> bool:
    return bool((value or "").strip())


def _has_gender(sex: str | None) -> bool:
    raw = (sex or "").strip().lower()
    return raw in {
        "m",
        "male",
        "muški",
        "muski",
        "muskarac",
        "muškarac",
        "f",
        "female",
        "ženski",
        "zenski",
        "zena",
        "žena",
    }


def _has_nationality(guest: Guest) -> bool:
    return _has_text(guest.nationality) or _has_text(guest.document_country_iso2)


def _has_document_type(guest: Guest) -> bool:
    return _has_text(guest.document_type) or _has_text(guest.document_code)


class GuestValidator:
    """Validate guest identity fields required for web check-in slot readiness."""

    REQUIRED_FIELDS = (
        "first_name",
        "last_name",
        "date_of_birth",
        "nationality",
        "sex",
        "document_number",
        "document_type",
        "address",
    )

    @classmethod
    def validate(cls, guest: Guest, *, position: int) -> SlotValidationResult:
        missing: list[str] = []

        if not _has_text(guest.first_name):
            missing.append("first_name")
        if not _has_text(guest.last_name):
            missing.append("last_name")
        if guest.date_of_birth is None:
            missing.append("date_of_birth")
        if not _has_nationality(guest):
            missing.append("nationality")
        if not _has_gender(guest.sex):
            missing.append("sex")
        if not _has_text(guest.document_number):
            missing.append("document_number")
        if not _has_document_type(guest):
            missing.append("document_type")
        if not _has_text(guest.address):
            missing.append("address")
        elif not validate_evisitor_residence_address(guest.address).valid:
            missing.append("address")

        status = SlotReadinessStatus.READY if not missing else SlotReadinessStatus.PARTIAL
        return SlotValidationResult(
            position=position,
            guest_id=guest.pk,
            status=status,
            missing_fields=tuple(missing),
        )
