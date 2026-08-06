"""Public readiness DTO for guest web check-in."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from apps.reservations.document_expectations import expected_document_count, expected_document_slots
from apps.reservations.guest_slots import ensure_guest_slots_for_intake
from apps.reservations.guest_validation import GuestValidator, SlotReadinessStatus, SlotValidationResult
from apps.reservations.models import GuestCheckInSession, GuestCheckInSessionStatus, Reservation


@dataclass(frozen=True)
class SlotReadinessDTO:
    position: int
    guest_id: int
    status: str
    missing_fields: tuple[str, ...]
    field_errors: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class CheckInReadinessDTO:
    status: str
    effective_status: str
    required_slots: int
    ready_slots: int
    can_complete: bool
    slots: tuple[SlotReadinessDTO, ...]
    waiting_positions: tuple[int, ...]
    ops_version: int = 0
    expected_checkin_adults: int | None = None
    adults_count: int | None = None


def effective_session_status(
    session: GuestCheckInSession,
    reservation: Reservation,
) -> str:
    """Derived business status; READY is never persisted on the session row."""
    if session.status != GuestCheckInSessionStatus.ACTIVE:
        return session.status
    if all_required_slots_ready(reservation):
        return "ready"
    return GuestCheckInSessionStatus.ACTIVE


def all_required_slots_ready(reservation: Reservation) -> bool:
    slots = slot_validation_results(reservation)
    if not slots:
        return False
    return all(slot.status == SlotReadinessStatus.READY for slot in slots)


def slot_validation_results(reservation: Reservation) -> list[SlotValidationResult]:
    ensure_guest_slots_for_intake(
        tenant=reservation.tenant,
        reservation=reservation,
        min_count=expected_document_count(reservation),
    )
    slots = expected_document_slots(reservation)
    return [
        GuestValidator.validate(guest, position=position)
        for position, guest in enumerate(slots, start=1)
    ]


def build_checkin_readiness(
    session: GuestCheckInSession,
    reservation: Reservation,
) -> CheckInReadinessDTO:
    validations = slot_validation_results(reservation)
    ready_count = sum(1 for slot in validations if slot.status == SlotReadinessStatus.READY)
    effective = effective_session_status(session, reservation)
    waiting = tuple(
        slot.position for slot in validations if slot.status != SlotReadinessStatus.READY
    )
    slot_dtos = tuple(
        SlotReadinessDTO(
            position=slot.position,
            guest_id=slot.guest_id,
            status=slot.status.value,
            missing_fields=slot.missing_fields,
            field_errors=slot.field_errors,
        )
        for slot in validations
    )
    return CheckInReadinessDTO(
        status=session.status,
        effective_status=effective,
        required_slots=len(validations),
        ready_slots=ready_count,
        can_complete=effective == "ready",
        slots=slot_dtos,
        waiting_positions=waiting,
        ops_version=int(session.ops_version or 0),
        expected_checkin_adults=reservation.expected_checkin_adults,
        adults_count=reservation.adults_count,
    )


def readiness_snapshot(reservation: Reservation) -> tuple[bool, tuple[int, ...]]:
    """Return (all_ready, waiting_positions) for event diffing."""
    validations = slot_validation_results(reservation)
    waiting = tuple(
        slot.position for slot in validations if slot.status != SlotReadinessStatus.READY
    )
    return not waiting and bool(validations), waiting
