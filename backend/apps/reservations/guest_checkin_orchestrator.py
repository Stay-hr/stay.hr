"""Cross-channel coordinator for guest web check-in."""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction

from apps.reservations.checkin_readiness import (
    CheckInReadinessDTO,
    build_checkin_readiness,
    effective_session_status,
    readiness_snapshot,
    slot_validation_results,
)
from apps.reservations.document_expectations import expected_document_slots
from apps.reservations.guest_checkin_events import (
    emit_guest_checkin_link_regenerated,
    emit_guest_session_completed,
    emit_guest_session_ready,
    emit_guest_slot_ready,
)
from apps.reservations.guest_checkin_session import (
    SessionAccessResult,
    build_guest_checkin_url,
    ensure_active_session,
    evaluate_session_access,
    mark_session_completed,
    regenerate_session,
    touch_session_activity,
)
from apps.reservations.guest_validation import SlotReadinessStatus
from apps.reservations.models import Guest, GuestCheckInSession, GuestCheckInSessionStatus, Reservation

_GUEST_PATCHABLE_FIELDS = frozenset(
    {
        "first_name",
        "last_name",
        "email",
        "phone",
        "date_of_birth",
        "document_number",
        "nationality",
        "sex",
        "address",
        "date_of_issue",
        "date_of_expiry",
        "issuing_authority",
        "personal_id_number",
        "document_additional_number",
        "additional_personal_id_number",
        "document_code",
        "document_type",
        "document_country",
        "document_country_iso2",
        "document_country_iso3",
        "document_country_numeric",
    }
)


class GuestCheckInOrchestratorError(Exception):
    def __init__(self, code: str, message: str = "", *, http_status: int = 400):
        super().__init__(message or code)
        self.code = code
        self.http_status = http_status


@dataclass(frozen=True)
class EnsureSessionResult:
    session: GuestCheckInSession
    url: str


@dataclass(frozen=True)
class PatchSlotResult:
    readiness: CheckInReadinessDTO
    access: SessionAccessResult


@dataclass(frozen=True)
class CompleteSessionResult:
    session: GuestCheckInSession
    readiness: CheckInReadinessDTO


class GuestCheckInOrchestrator:
    """Single entry point for guest web check-in cross-channel coordination."""

    @staticmethod
    def ensure_session_and_link(
        reservation: Reservation,
        *,
        created_from: str,
        wa_id: str = "",
    ) -> EnsureSessionResult:
        session = ensure_active_session(
            reservation,
            created_from=created_from,
            wa_id=wa_id,
        )
        url = build_guest_checkin_url(session, reservation)
        return EnsureSessionResult(session=session, url=url)

    @staticmethod
    def regenerate_link(
        reservation: Reservation,
        *,
        created_from: str,
        wa_id: str = "",
    ) -> EnsureSessionResult:
        old, new = regenerate_session(
            reservation,
            created_from=created_from,
            wa_id=wa_id,
        )
        emit_guest_checkin_link_regenerated(
            old_session=old,
            new_session=new,
            reservation=reservation,
        )
        return EnsureSessionResult(session=new, url=build_guest_checkin_url(new, reservation))

    @staticmethod
    @transaction.atomic
    def patch_slot(
        session: GuestCheckInSession,
        reservation: Reservation,
        *,
        position: int,
        fields: dict,
    ) -> PatchSlotResult:
        access = evaluate_session_access(session, reservation)
        if not access.allowed:
            raise GuestCheckInOrchestratorError(
                access.gate_status,
                http_status=access.http_status,
            )

        before_all_ready, _ = readiness_snapshot(reservation)
        before_slots = {
            slot.position: slot.status for slot in slot_validation_results(reservation)
        }

        guest = _guest_at_position(reservation, position)

        # Identity collision on manual document_number PATCH (same normalizer as OCR).
        if "document_number" in fields and fields.get("document_number"):
            from apps.reservations.document_intake_identity import (
                REASON_DUPLICATE_IDENTITY,
                classify_identity_collision,
                emit_identity_collision_audit,
                person_document_number,
            )
            from apps.reservations.document_intake_ocr_fixup import (
                normalize_document_number,
            )

            normalized = normalize_document_number(str(fields.get("document_number") or ""))
            fields = {**fields, "document_number": normalized}
            collision = classify_identity_collision(
                reservation=reservation,
                person={"document_number": normalized},
                target_guest_id=guest.pk,
            )
            if collision.status == "duplicate_identity":
                emit_identity_collision_audit(
                    reservation_id=reservation.pk,
                    job_id=None,
                    existing_guest_id=collision.existing_guest_id,
                    target_guest_id=guest.pk,
                    document_number=person_document_number(
                        {"document_number": normalized}
                    ),
                    reason=REASON_DUPLICATE_IDENTITY,
                )
                raise GuestCheckInOrchestratorError(
                    "duplicate_identity",
                    message=(
                        f"Document already belongs to guest #{collision.existing_guest_id}"
                    ),
                    http_status=409,
                )

        _apply_guest_fields(guest, fields)
        touch_session_activity(session)

        after_slots = slot_validation_results(reservation)
        for slot in after_slots:
            prev = before_slots.get(slot.position)
            if (
                prev != SlotReadinessStatus.READY
                and slot.status == SlotReadinessStatus.READY
            ):
                emit_guest_slot_ready(
                    session=session,
                    reservation=reservation,
                    position=slot.position,
                    guest_id=slot.guest_id,
                )

        after_all_ready, _ = readiness_snapshot(reservation)
        if not before_all_ready and after_all_ready:
            emit_guest_session_ready(session=session, reservation=reservation)

        readiness = build_checkin_readiness(session, reservation)
        return PatchSlotResult(readiness=readiness, access=access)

    @staticmethod
    @transaction.atomic
    def complete_session(
        session: GuestCheckInSession,
        reservation: Reservation,
    ) -> CompleteSessionResult:
        access = evaluate_session_access(session, reservation)
        if not access.allowed:
            raise GuestCheckInOrchestratorError(
                access.gate_status,
                http_status=access.http_status,
            )

        if effective_session_status(session, reservation) != "ready":
            raise GuestCheckInOrchestratorError(
                "not_ready",
                message="All required guest slots must be ready before completing.",
                http_status=409,
            )

        mark_session_completed(session)
        emit_guest_session_completed(session=session, reservation=reservation)
        readiness = build_checkin_readiness(session, reservation)

        reservation_id = reservation.pk
        session_id = session.pk

        def _enqueue_portal_link() -> None:
            from apps.reservations.guest_checkin_tasks import (
                send_guest_portal_link_after_checkin,
            )

            send_guest_portal_link_after_checkin.delay(reservation_id, session_id)

        transaction.on_commit(_enqueue_portal_link)
        return CompleteSessionResult(session=session, readiness=readiness)

    @staticmethod
    def get_readiness(
        session: GuestCheckInSession,
        reservation: Reservation,
    ) -> tuple[CheckInReadinessDTO, SessionAccessResult]:
        access = evaluate_session_access(session, reservation)
        readiness = build_checkin_readiness(session, reservation)
        return readiness, access


def _guest_at_position(reservation: Reservation, position: int) -> Guest:
    if position < 1:
        raise GuestCheckInOrchestratorError("invalid_position", http_status=400)
    slots = expected_document_slots(reservation)
    if position > len(slots):
        raise GuestCheckInOrchestratorError("invalid_position", http_status=404)
    return slots[position - 1]


def _apply_guest_fields(guest: Guest, fields: dict) -> None:
    if not isinstance(fields, dict):
        raise GuestCheckInOrchestratorError("invalid_payload", http_status=400)

    update_fields: list[str] = []
    for key, value in fields.items():
        if key not in _GUEST_PATCHABLE_FIELDS:
            continue
        setattr(guest, key, value)
        update_fields.append(key)

    if not update_fields:
        return

    update_fields.append("updated_at")
    guest.save(update_fields=update_fields)
