"""WEB_GUEST document intake — slot-forced matching for web check-in OCR."""

from __future__ import annotations

from apps.reservations.document_expectations import expected_document_slots
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


def is_web_guest_slot_forced_job(ctx: DocumentIntakeContext) -> bool:
    job = ctx.job
    return (
        job.source == DocumentIntakeJobSource.WEB_GUEST
        and bool(job.guest_checkin_slot_position)
        and ctx.is_reservation_scoped
    )


def run_web_guest_matching_pipeline(
    *,
    ctx: DocumentIntakeContext,
    persons: list[dict],
) -> list[dict]:
    """Force each OCR person onto the guest at ``guest_checkin_slot_position``.

    Identity collision on another guest → duplicate_identity (no auto_apply).
    Same guest already holding identity → already_processed (no auto_apply / face write).
    """
    job = ctx.job
    reservation = ctx.reservation
    if reservation is None:
        return []

    position = int(job.guest_checkin_slot_position or 0)
    slots = expected_document_slots(reservation)
    if position < 1 or position > len(slots):
        return []

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

    matches: list[dict] = []
    for idx, person in enumerate(persons):
        collision = classify_identity_collision(
            reservation=reservation,
            person=person if isinstance(person, dict) else {},
            target_guest_id=target_guest.pk,
        )
        base = {
            "person_index": idx,
            "person_name": _person_full_name(person if isinstance(person, dict) else {}),
            "confidence": "high",
            "candidates": [candidate],
            "reservation_id": reservation.pk,
            "guest_id": target_guest.pk,
            "guest_name": guest_name,
            "reservation_label": reservation_label,
            "audit_status": "confirmed",
        }
        if collision.status == "none":
            matches.append({**base, "auto_apply": True})
            continue

        emit_identity_collision_audit(
            reservation_id=reservation.pk,
            job_id=job.pk,
            existing_guest_id=collision.existing_guest_id,
            target_guest_id=target_guest.pk,
            document_number=person_document_number(
                person if isinstance(person, dict) else {}
            ),
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
        matches.append(rejected)
    return matches
