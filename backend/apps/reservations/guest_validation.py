"""Guest field validation for web check-in wizard (separate from eVisitor mapper).

Date of birth plausibility follows eVisitor API rules: DOB must be before today
and not older than 120 calendar years. Minors remain valid here; whether a guest
must be submitted to eVisitor is decided elsewhere (guest_requires_evisitor; always True).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum

from django.utils import timezone

from apps.integrations.evisitor.residence_address import validate_evisitor_residence_address
from apps.reservations.models import Guest

DOB_MAX_AGE_YEARS = 120
DOB_OUT_OF_RANGE_MESSAGE = "Datum rođenja nije unutar dozvoljenog raspona."


class SlotReadinessStatus(str, Enum):
    PARTIAL = "partial"
    READY = "ready"


@dataclass(frozen=True)
class SlotValidationResult:
    position: int
    guest_id: int
    status: SlotReadinessStatus
    missing_fields: tuple[str, ...]
    field_errors: tuple[tuple[str, str], ...] = ()

    def field_errors_dict(self) -> dict[str, str]:
        return dict(self.field_errors)


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


def _dob_earliest_allowed(today: date) -> date:
    """Earliest acceptable DOB: today minus 120 calendar years.

    Falls back to 28 Feb only when the target year has no 29 Feb.
    """
    if not isinstance(today, date):
        raise TypeError("today must be a datetime.date")
    try:
        return today.replace(year=today.year - DOB_MAX_AGE_YEARS)
    except ValueError:
        # today is 29 Feb and target year is not a leap year
        return date(today.year - DOB_MAX_AGE_YEARS, 2, 28)


def _is_plausible_dob(dob: date, *, today: date) -> bool:
    """True when dob is a date strictly before today and within 120 years."""
    if not isinstance(dob, date) or not isinstance(today, date):
        return False
    if dob >= today:
        return False
    return dob >= _dob_earliest_allowed(today)


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
        field_errors: list[tuple[str, str]] = []

        if not _has_text(guest.first_name):
            missing.append("first_name")
        if not _has_text(guest.last_name):
            missing.append("last_name")
        if guest.date_of_birth is None:
            missing.append("date_of_birth")
        elif not _is_plausible_dob(guest.date_of_birth, today=timezone.localdate()):
            missing.append("date_of_birth")
            field_errors.append(("date_of_birth", DOB_OUT_OF_RANGE_MESSAGE))
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
        else:
            address_result = validate_evisitor_residence_address(guest.address)
            if not address_result.valid:
                missing.append("address")
                msg = (
                    address_result.errors[0]
                    if address_result.errors
                    else "Nije moguće odrediti grad prebivališta (CityOfResidence)."
                )
                field_errors.append(("address", msg))

        status = SlotReadinessStatus.READY if not missing else SlotReadinessStatus.PARTIAL
        return SlotValidationResult(
            position=position,
            guest_id=guest.pk,
            status=status,
            missing_fields=tuple(missing),
            field_errors=tuple(field_errors),
        )
