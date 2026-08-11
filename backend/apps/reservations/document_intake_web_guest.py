"""WEB_GUEST document intake — slot-forced matching for web check-in OCR."""

from __future__ import annotations

from apps.reservations.document_expectations import (
    expected_document_count,
    expected_document_slots,
)
from apps.reservations.document_intake_context import DocumentIntakeContext
from apps.reservations.document_intake_identity import (
    classify_identity_collision,
    emit_identity_collision_audit,
    person_document_number,
)
from apps.reservations.document_intake_match import (
    _guest_display_name,
    _person_full_name,
    _reservation_label,
)
from apps.reservations.models import DocumentIntakeJobSource

OCCUPANCY_STATUS_MISMATCH = "occupancy_mismatch"


def is_web_guest_slot_forced_job(ctx: DocumentIntakeContext) -> bool:
    job = ctx.job
    return (
        job.source == DocumentIntakeJobSource.WEB_GUEST
        and bool(job.guest_checkin_slot_position)
        and ctx.is_reservation_scoped
    )


def occupancy_persons_mismatch(
    *,
    reservation,
    persons: list,
) -> tuple[bool, int, int]:
    """Return (is_mismatch, persons_detected, expected_persons)."""
    expected = int(expected_document_count(reservation) or 0)
    detected = len(persons) if isinstance(persons, list) else 0
    if expected <= 0:
        return False, detected, expected
    return detected > expected, detected, expected


def run_web_guest_matching_pipeline(
    *,
    ctx: DocumentIntakeContext,
    persons: list[dict],
) -> list[dict]:
    """Force OCR ``person_index=0`` onto the guest at ``guest_checkin_slot_position``.

    Identity collision on another guest → duplicate_identity (no auto_apply).
    Same guest already holding identity → already_processed (no auto_apply / face write).

    If OCR detects more persons than ``expected_document_count`` (occupancy /
    OTA adults), return a single ``occupancy_mismatch`` match with no auto_apply
    and no guest writes — UI may offer PATCH occupancy, then rematch on poll.
    Extra persons never auto_apply onto this slot (companion fills their own slot).
    """
    job = ctx.job
    reservation = ctx.reservation
    if reservation is None:
        return []

    mismatch, detected, expected = occupancy_persons_mismatch(
        reservation=reservation,
        persons=persons,
    )
    if mismatch:
        return [
            {
                "person_index": 0,
                "person_name": "",
                "confidence": "high",
                "candidates": [],
                "reservation_id": reservation.pk,
                "auto_apply": False,
                "occupancy_status": OCCUPANCY_STATUS_MISMATCH,
                "persons_detected": detected,
                "expected_persons": expected,
            }
        ]

    position = int(job.guest_checkin_slot_position or 0)
    slots = expected_document_slots(reservation)
    if position < 1 or position > len(slots):
        return []

    # WEB_GUEST jobs are one upload → one slot. Only person_index=0 is forced
    # onto that guest (see ocr-multi-guest-rules). Extra OCR persons may trigger
    # occupancy_mismatch above; they must not overwrite this slot on apply.
    if not persons:
        return []
    person = persons[0] if isinstance(persons[0], dict) else {}
    idx = 0

    target_guest = slots[position - 1]
    reservation_label = _reservation_label(reservation)
    guest_name = _guest_display_name(target_guest)
    candidate = {
        "reservation_id": reservation.pk,
        "guest_id": target_guest.pk,
        "guest_name": guest_name,
        "reservation_label": reservation_label,
        "match_type": "web_guest_slot",
        "check_in_date": reservation.check_in.isoformat(),
    }

    collision = classify_identity_collision(
        reservation=reservation,
        person=person,
        target_guest_id=target_guest.pk,
    )
    base = {
        "person_index": idx,
        "person_name": _person_full_name(person),
        "confidence": "high",
        "candidates": [candidate],
        "reservation_id": reservation.pk,
        "guest_id": target_guest.pk,
        "guest_name": guest_name,
        "reservation_label": reservation_label,
        "audit_status": "confirmed",
    }
    if collision.status == "none":
        return [{**base, "auto_apply": True}]

    emit_identity_collision_audit(
        reservation_id=reservation.pk,
        job_id=job.pk,
        existing_guest_id=collision.existing_guest_id,
        target_guest_id=target_guest.pk,
        document_number=person_document_number(person),
        reason=collision.reason,
    )
    rejected = {
        **base,
        "auto_apply": False,
        "reject_reason": collision.reason,
        "identity_status": collision.status,
        "existing_guest_id": collision.existing_guest_id,
    }
    if collision.existing_guest is not None:
        rejected["existing_guest_name"] = _guest_display_name(collision.existing_guest)
        if collision.status == "duplicate_identity":
            rejected["guest_id"] = collision.existing_guest.pk
            rejected["guest_name"] = rejected["existing_guest_name"]
    return [rejected]
