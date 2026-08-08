"""Domain events for guest web check-in (orchestrator emits, handlers react)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.utils import timezone

from apps.reservations.models import GuestCheckInSession, Reservation, ReservationVersionScope
from apps.reservations.reservation_version import touch_reservation_version

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GuestSlotReadyEvent:
    session: GuestCheckInSession
    reservation: Reservation
    position: int
    guest_id: int


@dataclass(frozen=True)
class GuestSessionReadyEvent:
    session: GuestCheckInSession
    reservation: Reservation


@dataclass(frozen=True)
class GuestSessionCompletedEvent:
    session: GuestCheckInSession
    reservation: Reservation


@dataclass(frozen=True)
class GuestCheckInLinkRegeneratedEvent:
    old_session: GuestCheckInSession | None
    new_session: GuestCheckInSession
    reservation: Reservation


@dataclass(frozen=True)
class GuestCheckInOccupancyChangedEvent:
    session: GuestCheckInSession
    reservation: Reservation
    old_value: int | None
    new_value: int | None
    reason: str


def emit_guest_slot_ready(
    *,
    session: GuestCheckInSession,
    reservation: Reservation,
    position: int,
    guest_id: int,
) -> None:
    event = GuestSlotReadyEvent(
        session=session,
        reservation=reservation,
        position=position,
        guest_id=guest_id,
    )
    _dispatch_guest_slot_ready(event)


def emit_guest_session_ready(
    *,
    session: GuestCheckInSession,
    reservation: Reservation,
) -> None:
    event = GuestSessionReadyEvent(session=session, reservation=reservation)
    _dispatch_guest_session_ready(event)


def emit_guest_session_completed(
    *,
    session: GuestCheckInSession,
    reservation: Reservation,
) -> None:
    event = GuestSessionCompletedEvent(session=session, reservation=reservation)
    _dispatch_guest_session_completed(event)


def emit_guest_checkin_link_regenerated(
    *,
    old_session: GuestCheckInSession | None,
    new_session: GuestCheckInSession,
    reservation: Reservation,
) -> None:
    event = GuestCheckInLinkRegeneratedEvent(
        old_session=old_session,
        new_session=new_session,
        reservation=reservation,
    )
    _dispatch_guest_checkin_link_regenerated(event)


def emit_guest_checkin_occupancy_changed(
    *,
    session: GuestCheckInSession,
    reservation: Reservation,
    old_value: int | None,
    new_value: int | None,
    reason: str,
) -> None:
    event = GuestCheckInOccupancyChangedEvent(
        session=session,
        reservation=reservation,
        old_value=old_value,
        new_value=new_value,
        reason=reason,
    )
    _dispatch_guest_checkin_occupancy_changed(event)


def _dispatch_guest_slot_ready(event: GuestSlotReadyEvent) -> None:
    logger.info(
        "guest_checkin slot_ready reservation=%s session=%s position=%s guest=%s",
        event.reservation.pk,
        event.session.pk,
        event.position,
        event.guest_id,
    )
    for handler in _GUEST_SLOT_READY_HANDLERS:
        handler(event)


def _dispatch_guest_session_ready(event: GuestSessionReadyEvent) -> None:
    logger.info(
        "guest_checkin session_ready reservation=%s session=%s",
        event.reservation.pk,
        event.session.pk,
    )
    for handler in _GUEST_SESSION_READY_HANDLERS:
        handler(event)


def _dispatch_guest_session_completed(event: GuestSessionCompletedEvent) -> None:
    logger.info(
        "guest_checkin session_completed reservation=%s session=%s",
        event.reservation.pk,
        event.session.pk,
    )
    for handler in _GUEST_SESSION_COMPLETED_HANDLERS:
        handler(event)


def _dispatch_guest_checkin_link_regenerated(event: GuestCheckInLinkRegeneratedEvent) -> None:
    logger.info(
        "guest_checkin link_regenerated reservation=%s old_session=%s new_session=%s",
        event.reservation.pk,
        event.old_session.pk if event.old_session else None,
        event.new_session.pk,
    )
    for handler in _GUEST_CHECKIN_LINK_REGENERATED_HANDLERS:
        handler(event)


def _dispatch_guest_checkin_occupancy_changed(event: GuestCheckInOccupancyChangedEvent) -> None:
    logger.info(
        "guest_checkin occupancy_changed reservation=%s session=%s from=%s to=%s reason=%s",
        event.reservation.pk,
        event.session.pk,
        event.old_value,
        event.new_value,
        event.reason,
    )
    for handler in _GUEST_CHECKIN_OCCUPANCY_CHANGED_HANDLERS:
        handler(event)


def on_guest_slot_ready(handler):
    _GUEST_SLOT_READY_HANDLERS.append(handler)
    return handler


def on_guest_session_ready(handler):
    _GUEST_SESSION_READY_HANDLERS.append(handler)
    return handler


def on_guest_session_completed(handler):
    _GUEST_SESSION_COMPLETED_HANDLERS.append(handler)
    return handler


def on_guest_checkin_link_regenerated(handler):
    _GUEST_CHECKIN_LINK_REGENERATED_HANDLERS.append(handler)
    return handler


def on_guest_checkin_occupancy_changed(handler):
    _GUEST_CHECKIN_OCCUPANCY_CHANGED_HANDLERS.append(handler)
    return handler


_GUEST_SLOT_READY_HANDLERS: list = []
_GUEST_SESSION_READY_HANDLERS: list = []
_GUEST_SESSION_COMPLETED_HANDLERS: list = []
_GUEST_CHECKIN_LINK_REGENERATED_HANDLERS: list = []
_GUEST_CHECKIN_OCCUPANCY_CHANGED_HANDLERS: list = []


@on_guest_session_ready
def set_ready_at_on_session_ready(event: GuestSessionReadyEvent) -> None:
    """Stamp ready_at once when the session first becomes ready (ADR 0004)."""
    GuestCheckInSession.objects.filter(
        pk=event.session.pk,
        ready_at__isnull=True,
    ).update(ready_at=timezone.now())


@on_guest_session_ready
def bump_checkin_version_on_session_ready(event: GuestSessionReadyEvent) -> None:
    touch_reservation_version(
        event.reservation.pk,
        ReservationVersionScope.CHECKIN,
        reason="guest_session_ready",
    )


@on_guest_session_completed
def bump_checkin_version_on_session_completed(event: GuestSessionCompletedEvent) -> None:
    touch_reservation_version(
        event.reservation.pk,
        ReservationVersionScope.CHECKIN,
        reason="guest_session_completed",
    )


@on_guest_checkin_link_regenerated
def log_checkin_link_regenerated(event: GuestCheckInLinkRegeneratedEvent) -> None:
    logger.info(
        "guest_checkin audit link_regenerated reservation=%s token=%s",
        event.reservation.pk,
        event.new_session.token,
    )


@on_guest_checkin_occupancy_changed
def bump_checkin_version_on_occupancy_changed(
    event: GuestCheckInOccupancyChangedEvent,
) -> None:
    touch_reservation_version(
        event.reservation.pk,
        ReservationVersionScope.CHECKIN,
        reason="guest_checkin_occupancy_changed",
    )
