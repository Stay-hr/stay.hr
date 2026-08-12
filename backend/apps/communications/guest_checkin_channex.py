"""Send guest web check-in link via Channex OTA inbox."""

from __future__ import annotations

import logging

from apps.communications.guest_compose import render_channex_guest_checkin_link_message
from apps.integrations.channex.ari_service import get_active_channex_integration
from apps.integrations.channex.exceptions import ChannexBookingIngestError
from apps.integrations.channex.message_service import send_message_for_reservation
from apps.integrations.channel_manager.resolver import get_channel_manager
from apps.reservations.guest_checkin_orchestrator import GuestCheckInOrchestrator
from apps.reservations.guest_checkin_session import mark_checkin_link_distributed
from apps.reservations.models import GuestCheckInSessionCreatedFrom, Reservation
from apps.tenants.models import ChannelManager

logger = logging.getLogger(__name__)


def send_guest_checkin_link_via_channex(reservation_id: int) -> dict:
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

    session_result = GuestCheckInOrchestrator.ensure_session_and_link(
        reservation,
        created_from=GuestCheckInSessionCreatedFrom.CHANNEX,
    )
    body = render_channex_guest_checkin_link_message(
        reservation,
        checkin_url=session_result.url,
    )

    try:
        integration = get_active_channex_integration(reservation.tenant.slug)
        send_message_for_reservation(integration, reservation, body)
    except ChannexBookingIngestError as exc:
        logger.warning(
            "channex guest check-in link send failed",
            extra={"reservation_id": reservation_id, "error": str(exc)},
        )
        return {"sent": False, "reason": str(exc)}

    mark_checkin_link_distributed(
        session_result.session,
        distributed_from=GuestCheckInSessionCreatedFrom.CHANNEX,
    )

    logger.info(
        "channex guest check-in link sent",
        extra={"reservation_id": reservation_id},
    )
    return {"sent": True, "url": session_result.url}
