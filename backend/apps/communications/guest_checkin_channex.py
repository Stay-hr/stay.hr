"""Send guest web check-in link via Channex OTA inbox."""

from __future__ import annotations

import logging

from celery import shared_task
from django.db import transaction

from apps.communications.guest_compose import render_channex_guest_checkin_link_message
from apps.core.timezone import property_local_now
from apps.integrations.channex.ari_service import get_active_channex_integration
from apps.integrations.channex.exceptions import ChannexBookingIngestError
from apps.integrations.channex.message_service import send_message_for_reservation
from apps.integrations.channel_manager.resolver import get_channel_manager
from apps.reservations.guest_checkin_orchestrator import GuestCheckInOrchestrator
from apps.reservations.guest_checkin_session import (
    evaluate_session_access,
    get_active_session,
    mark_checkin_link_distributed,
)
from apps.reservations.models import (
    GuestCheckInSession,
    GuestCheckInSessionCreatedFrom,
    Reservation,
)
from apps.tenants.models import ChannelManager

logger = logging.getLogger(__name__)


def _orchestration_owns_checkin_outbound(reservation: Reservation) -> bool:
    from apps.communications.messaging.flags import suppress_legacy_automated_outbound

    return suppress_legacy_automated_outbound(reservation=reservation)


def is_immediate_channex_checkin_link_eligible(reservation: Reservation) -> bool:
    """True when check-in is within the property open window (property-local calendar).

    Inclusive of D-7 (or property window) and D-0. Past check-in (days < 0) is not eligible.
    """
    if reservation.status != Reservation.Status.EXPECTED:
        return False
    if reservation.import_source != "channex":
        return False
    if not reservation.check_in:
        return False
    prop = reservation.property
    if prop is None:
        return False
    today = property_local_now(prop).date()
    days = (reservation.check_in - today).days
    if days < 0:
        return False
    window = max(int(prop.guest_checkin_opens_days_before or 7), 0)
    return days <= window


def send_guest_checkin_link_via_channex(reservation_id: int) -> dict:
    """Send OTA check-in link; claim under session row lock through provider + stamp.

    ``select_for_update`` is held inside ``transaction.atomic()`` across the Channex
    HTTP call and ``mark_checkin_link_distributed`` so concurrent callers cannot
    double-send.
    """
    reservation = (
        Reservation.objects.filter(pk=reservation_id)
        .select_related("property", "tenant")
        .first()
    )
    if reservation is None:
        return {"sent": False, "reason": "reservation_not_found"}

    if reservation.import_source != "channex":
        return {"sent": False, "reason": "not_channex_reservation"}

    if reservation.status != Reservation.Status.EXPECTED:
        return {"sent": False, "reason": "wrong_status"}

    if get_channel_manager(reservation.tenant) != ChannelManager.CHANNEX:
        return {"sent": False, "reason": "channel_manager_not_channex"}

    # Create-if-missing outside the claim lock (ACTIVE reuse is idempotent).
    session_result = GuestCheckInOrchestrator.ensure_session_and_link(
        reservation,
        created_from=GuestCheckInSessionCreatedFrom.CHANNEX,
    )
    checkin_url = session_result.url

    try:
        with transaction.atomic():
            session = (
                GuestCheckInSession.objects.select_for_update()
                .filter(pk=session_result.session.pk)
                .first()
            )
            if session is None:
                return {"sent": False, "reason": "session_missing"}

            if session.last_distributed_from:
                return {
                    "sent": False,
                    "reason": "already_distributed",
                    "url": checkin_url,
                }

            body = render_channex_guest_checkin_link_message(
                reservation,
                checkin_url=checkin_url,
            )
            integration = get_active_channex_integration(reservation.tenant.slug)
            # Lock held through provider call (intentional race guard).
            send_message_for_reservation(integration, reservation, body)
            mark_checkin_link_distributed(
                session,
                distributed_from=GuestCheckInSessionCreatedFrom.CHANNEX,
            )
    except ChannexBookingIngestError as exc:
        logger.warning(
            "channex guest check-in link send failed",
            extra={"reservation_id": reservation_id, "error": str(exc)},
        )
        return {"sent": False, "reason": str(exc)}

    logger.info(
        "channex guest check-in link sent",
        extra={"reservation_id": reservation_id},
    )
    return {"sent": True, "url": checkin_url}


@shared_task(name="communications.maybe_send_immediate_channex_checkin_link")
def maybe_send_immediate_channex_checkin_link(reservation_id: int) -> dict:
    """Authoritative late-booking send after create enqueue (re-validates all guards)."""
    reservation = (
        Reservation.objects.select_related("property", "tenant")
        .filter(pk=reservation_id)
        .first()
    )
    if reservation is None:
        return {"status": "missing", "reservation_id": reservation_id}

    if _orchestration_owns_checkin_outbound(reservation):
        logger.info(
            "immediate channex checkin link suppressed_by_orchestration "
            "reservation_id=%s",
            reservation_id,
        )
        return {
            "status": "skipped",
            "reason": "orchestration_owns_outbound",
            "reservation_id": reservation_id,
        }

    from apps.reservations.booking_lifecycle import is_web_pending_booking

    if is_web_pending_booking(reservation):
        return {
            "status": "skipped",
            "reason": "web_pending",
            "reservation_id": reservation_id,
        }

    if not is_immediate_channex_checkin_link_eligible(reservation):
        return {
            "status": "skipped",
            "reason": "not_eligible",
            "reservation_id": reservation_id,
        }

    existing = get_active_session(reservation)
    if existing is not None and existing.last_distributed_from:
        return {
            "status": "skipped",
            "reason": "already_distributed",
            "reservation_id": reservation_id,
        }

    # Create session early so access gate can skip before provider call.
    ensured = GuestCheckInOrchestrator.ensure_session_and_link(
        reservation,
        created_from=GuestCheckInSessionCreatedFrom.CHANNEX,
    )
    access = evaluate_session_access(ensured.session, reservation)
    if not access.allowed:
        return {
            "status": "skipped",
            "reason": f"session_{access.gate_status}",
            "reservation_id": reservation_id,
        }

    result = send_guest_checkin_link_via_channex(reservation_id)
    if not result.get("sent"):
        reason = str(result.get("reason") or "send_failed")
        status = "skipped" if reason == "already_distributed" else "failed"
        return {
            "status": status,
            "reason": reason,
            "reservation_id": reservation_id,
        }

    return {
        "status": "sent",
        "reservation_id": reservation_id,
        "url": result.get("url"),
    }
