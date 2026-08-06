"""Cross-channel coordinator for guest web check-in."""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.db.models import F

from apps.integrations.evisitor.residence_address import validate_evisitor_residence_address
from apps.reservations.checkin_readiness import (
    CheckInReadinessDTO,
    build_checkin_readiness,
    effective_session_status,
    readiness_snapshot,
    slot_validation_results,
)
from apps.reservations.document_expectations import (
    booked_adults_ceiling,
    expected_document_slots,
)
from apps.reservations.guest_checkin_events import (
    emit_guest_checkin_link_regenerated,
    emit_guest_checkin_occupancy_changed,
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
from apps.reservations.guest_slots import remove_unfilled_secondary_guests
from apps.reservations.guest_validation import GuestValidator, SlotReadinessStatus
from apps.reservations.models import Guest, GuestCheckInSession, Reservation

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
    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        http_status: int = 400,
        payload: dict | None = None,
    ):
        super().__init__(message or code)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.payload = payload or {}


@dataclass(frozen=True)
class EnsureSessionResult:
    session: GuestCheckInSession
    url: str


@dataclass(frozen=True)
class PatchSlotResult:
    readiness: CheckInReadinessDTO
    access: SessionAccessResult


@dataclass(frozen=True)
class CommitSlotResult:
    readiness: CheckInReadinessDTO
    access: SessionAccessResult
    position: int


@dataclass(frozen=True)
class OccupancyResult:
    readiness: CheckInReadinessDTO
    access: SessionAccessResult
    session: GuestCheckInSession


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
    def commit_slot(
        session: GuestCheckInSession,
        reservation: Reservation,
        *,
        position: int,
        ops_version: int | None,
        require_ops_version: bool = True,
    ) -> CommitSlotResult:
        access = evaluate_session_access(session, reservation)
        if not access.allowed:
            raise GuestCheckInOrchestratorError(
                access.gate_status,
                http_status=access.http_status,
            )
        _require_ops_version(
            session, ops_version, required=require_ops_version
        )

        guest = _guest_at_position(reservation, position)
        validation = GuestValidator.validate(guest, position=position)
        if validation.status != SlotReadinessStatus.READY:
            raise GuestCheckInOrchestratorError(
                "not_ready",
                message="Guest slot is missing required fields.",
                http_status=409,
                payload={
                    "position": position,
                    "guest_id": guest.pk,
                    "status": validation.status.value,
                    "missing_fields": list(validation.missing_fields),
                    "field_errors": validation.field_errors_dict(),
                    "ops_version": int(session.ops_version or 0),
                },
            )

        before_all_ready, _ = readiness_snapshot(reservation)
        emit_guest_slot_ready(
            session=session,
            reservation=reservation,
            position=position,
            guest_id=guest.pk,
        )
        after_all_ready, _ = readiness_snapshot(reservation)
        if not before_all_ready and after_all_ready:
            emit_guest_session_ready(session=session, reservation=reservation)

        _bump_ops_version(session)
        touch_session_activity(session)
        readiness = build_checkin_readiness(session, reservation)
        return CommitSlotResult(readiness=readiness, access=access, position=position)

    @staticmethod
    @transaction.atomic
    def patch_occupancy(
        session: GuestCheckInSession,
        reservation: Reservation,
        *,
        expected_checkin_adults,
        ops_version: int | None,
        reason: str = "guest_self_service",
        require_ops_version: bool = True,
    ) -> OccupancyResult:
        access = evaluate_session_access(session, reservation)
        if not access.allowed:
            raise GuestCheckInOrchestratorError(
                access.gate_status,
                http_status=access.http_status,
            )
        _require_ops_version(
            session, ops_version, required=require_ops_version
        )

        old_value = reservation.expected_checkin_adults
        ceiling = booked_adults_ceiling(reservation)

        if expected_checkin_adults is None:
            new_value = None
            reason = "guest_reset_to_ota" if reason == "guest_self_service" else reason
        else:
            try:
                new_value = int(expected_checkin_adults)
            except (TypeError, ValueError) as exc:
                raise GuestCheckInOrchestratorError(
                    "invalid_occupancy",
                    message="expected_checkin_adults must be an integer or null.",
                    http_status=400,
                ) from exc
            if new_value < 1:
                raise GuestCheckInOrchestratorError(
                    "invalid_occupancy",
                    message="expected_checkin_adults must be >= 1.",
                    http_status=400,
                )
            if ceiling > 0 and new_value > ceiling:
                raise GuestCheckInOrchestratorError(
                    "invalid_occupancy",
                    message=(
                        f"expected_checkin_adults cannot exceed booked adults ({ceiling})."
                    ),
                    http_status=400,
                )

        if old_value == new_value:
            readiness = build_checkin_readiness(session, reservation)
            return OccupancyResult(readiness=readiness, access=access, session=session)

        reservation.expected_checkin_adults = new_value
        reservation.save(update_fields=["expected_checkin_adults", "updated_at"])
        remove_unfilled_secondary_guests(reservation)

        emit_guest_checkin_occupancy_changed(
            session=session,
            reservation=reservation,
            old_value=old_value,
            new_value=new_value,
            reason=reason,
        )
        _bump_ops_version(session)
        touch_session_activity(session)
        readiness = build_checkin_readiness(session, reservation)
        return OccupancyResult(readiness=readiness, access=access, session=session)

    @staticmethod
    @transaction.atomic
    def complete_session(
        session: GuestCheckInSession,
        reservation: Reservation,
        *,
        ops_version: int | None = None,
        require_ops_version: bool = False,
    ) -> CompleteSessionResult:
        access = evaluate_session_access(session, reservation)
        if not access.allowed:
            raise GuestCheckInOrchestratorError(
                access.gate_status,
                http_status=access.http_status,
            )
        _require_ops_version(
            session, ops_version, required=require_ops_version
        )

        if effective_session_status(session, reservation) != "ready":
            raise GuestCheckInOrchestratorError(
                "not_ready",
                message="All required guest slots must be ready before completing.",
                http_status=409,
            )

        mark_session_completed(session)
        _bump_ops_version(session)
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


def _require_ops_version(
    session: GuestCheckInSession,
    ops_version: int | None,
    *,
    required: bool = True,
) -> None:
    current = int(session.ops_version or 0)
    if ops_version is None:
        if required:
            raise GuestCheckInOrchestratorError(
                "session_conflict",
                message="ops_version is required.",
                http_status=409,
                payload={"ops_version": current},
            )
        return
    try:
        provided = int(ops_version)
    except (TypeError, ValueError) as exc:
        raise GuestCheckInOrchestratorError(
            "session_conflict",
            message="ops_version must be an integer.",
            http_status=409,
            payload={"ops_version": current},
        ) from exc
    if provided != current:
        raise GuestCheckInOrchestratorError(
            "session_conflict",
            message="Session was updated elsewhere. Reload and retry.",
            http_status=409,
            payload={"ops_version": current},
        )


def _bump_ops_version(session: GuestCheckInSession) -> None:
    GuestCheckInSession.objects.filter(pk=session.pk).update(
        ops_version=F("ops_version") + 1
    )
    session.refresh_from_db(fields=["ops_version", "updated_at", "last_activity_at"])


def _apply_guest_fields(guest: Guest, fields: dict) -> None:
    if not isinstance(fields, dict):
        raise GuestCheckInOrchestratorError("invalid_payload", http_status=400)

    update_fields: list[str] = []
    for key, value in fields.items():
        if key not in _GUEST_PATCHABLE_FIELDS:
            continue
        if key == "address":
            raw = (value or "").strip() if isinstance(value, str) else ""
            if raw:
                result = validate_evisitor_residence_address(raw)
                if not result.valid:
                    raise GuestCheckInOrchestratorError(
                        "invalid_address",
                        result.errors[0]
                        if result.errors
                        else "Nije moguće odrediti grad prebivališta (CityOfResidence).",
                        http_status=400,
                    )
                value = result.normalized_address
            else:
                value = ""
        setattr(guest, key, value)
        update_fields.append(key)

    if not update_fields:
        return

    update_fields.append("updated_at")
    guest.save(update_fields=update_fields)
