"""Identity consistency for document intake (cross-job / cross-slot).

Core invariants (ADR 0017):
- one identity → one Guest per reservation
- document_number hard match is terminal
- duplicate / already_processed never create Guest or mutate face / IdDocument
- apply never writes MRZ-inconsistent fields
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

from django.utils.dateparse import parse_date

from apps.reservations.document_intake_identity_metrics import incr as identity_incr
from apps.reservations.document_intake_match import normalize_mrz_lines
from apps.reservations.document_intake_ocr_fixup import normalize_document_number
from apps.reservations.mrz_parse import (
    parse_date_of_birth_from_mrz,
    parse_document_number_from_mrz,
    parse_sex_from_mrz,
)
from apps.reservations.models import Guest, Reservation

logger = logging.getLogger(__name__)

IdentityStatus = Literal["none", "already_processed", "duplicate_identity"]

REASON_ALREADY_PROCESSED = "already_processed"
REASON_DUPLICATE_IDENTITY = "duplicate_identity"
REASON_MRZ_INCONSISTENT = "mrz_inconsistent"


@dataclass(frozen=True)
class IdentityCollision:
    status: IdentityStatus
    existing_guest: Guest | None = None
    reason: str = ""

    @property
    def existing_guest_id(self) -> int | None:
        return self.existing_guest.pk if self.existing_guest is not None else None


def _normalize_mrz_text(value: str) -> str:
    lines = [
        re.sub(r"\s+", "", str(line).upper())
        for line in (value or "").splitlines()
        if str(line).strip()
    ]
    return "\n".join(lines)


def person_document_number(person: dict) -> str:
    return normalize_document_number(str(person.get("document_number") or ""))


def person_mrz_key(person: dict) -> str:
    return _normalize_mrz_text(normalize_mrz_lines(person))


def guest_mrz_key(guest: Guest) -> str:
    return _normalize_mrz_text(guest.mrz_raw_text or "")


def find_guest_by_document_number(
    reservation: Reservation,
    person: dict,
    *,
    exclude: set[int] | None = None,
) -> Guest | None:
    doc_no = person_document_number(person)
    if not doc_no:
        return None
    blocked = exclude or set()
    for guest in reservation.guests.all():
        if guest.pk in blocked:
            continue
        if normalize_document_number(guest.document_number) == doc_no:
            return guest
    return None


def find_guest_by_mrz(
    reservation: Reservation,
    person: dict,
    *,
    exclude: set[int] | None = None,
) -> Guest | None:
    mrz_key = person_mrz_key(person)
    if not mrz_key:
        return None
    blocked = exclude or set()
    for guest in reservation.guests.all():
        if guest.pk in blocked:
            continue
        if guest_mrz_key(guest) == mrz_key:
            return guest
    return None


def find_guest_by_identity(
    reservation: Reservation,
    person: dict,
    *,
    exclude: set[int] | None = None,
) -> tuple[Guest | None, str]:
    """Return (guest, match_type) for document_number then MRZ. Empty match_type if none."""
    guest = find_guest_by_document_number(reservation, person, exclude=exclude)
    if guest is not None:
        return guest, "document_number"
    guest = find_guest_by_mrz(reservation, person, exclude=exclude)
    if guest is not None:
        return guest, "mrz"
    return None, ""


def classify_identity_collision(
    *,
    reservation: Reservation,
    person: dict,
    target_guest_id: int | None,
) -> IdentityCollision:
    existing, _match_type = find_guest_by_identity(reservation, person)
    if existing is None:
        return IdentityCollision(status="none")
    if target_guest_id is not None and int(existing.pk) == int(target_guest_id):
        return IdentityCollision(
            status="already_processed",
            existing_guest=existing,
            reason=REASON_ALREADY_PROCESSED,
        )
    return IdentityCollision(
        status="duplicate_identity",
        existing_guest=existing,
        reason=REASON_DUPLICATE_IDENTITY,
    )


def emit_identity_collision_audit(
    *,
    reservation_id: int,
    job_id: int | None,
    existing_guest_id: int | None,
    target_guest_id: int | None,
    document_number: str,
    reason: str,
) -> None:
    logger.info(
        "DOCUMENT_DUPLICATE_DETECTED reservation_id=%s job_id=%s "
        "existing_guest_id=%s target_guest_id=%s document_number=%s reason=%s",
        reservation_id,
        job_id,
        existing_guest_id,
        target_guest_id,
        document_number,
        reason,
    )
    if reason == REASON_ALREADY_PROCESSED:
        identity_incr("identity.already_processed", reservation_id=reservation_id)
    elif reason == REASON_DUPLICATE_IDENTITY:
        identity_incr("identity.duplicate", reservation_id=reservation_id)
    elif reason == REASON_MRZ_INCONSISTENT:
        identity_incr("identity.mrz_inconsistent", reservation_id=reservation_id)


def record_hard_match_metric(match_type: str, *, reservation_id: int | None = None) -> None:
    if match_type == "document_number":
        identity_incr("identity.hard_match.document", reservation_id=reservation_id)
    elif match_type == "mrz":
        identity_incr("identity.hard_match.mrz", reservation_id=reservation_id)

def _normalize_sex(value: str) -> str:
    raw = str(value or "").strip().upper()
    if raw in {"M", "MALE", "MUŠKI", "MUSKI"}:
        return "M"
    if raw in {"F", "FEMALE", "ŽENSKI", "ZENSKI"}:
        return "F"
    return raw[:1] if raw else ""


def _person_dob(person: dict) -> date | None:
    raw = str(person.get("date_of_birth") or "").strip()
    if not raw:
        return None
    return parse_date(raw)


def validate_person_against_mrz(person: dict) -> list[str]:
    """Return mismatch reason codes when OCR fields disagree with parseable MRZ."""
    mrz = normalize_mrz_lines(person)
    if not mrz:
        return []

    mismatches: list[str] = []

    mrz_sex = parse_sex_from_mrz(mrz)
    ocr_sex = _normalize_sex(str(person.get("sex") or ""))
    if mrz_sex and ocr_sex and mrz_sex != ocr_sex:
        mismatches.append("sex_mismatch")

    mrz_dob = parse_date_of_birth_from_mrz(mrz)
    ocr_dob = _person_dob(person)
    if mrz_dob is not None and ocr_dob is not None and mrz_dob != ocr_dob:
        mismatches.append("dob_mismatch")

    mrz_doc = normalize_document_number(parse_document_number_from_mrz(mrz))
    ocr_doc = person_document_number(person)
    if mrz_doc and ocr_doc and mrz_doc != ocr_doc:
        mismatches.append("document_number_mismatch")

    return mismatches
