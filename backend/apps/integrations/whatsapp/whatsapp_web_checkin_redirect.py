"""WhatsApp replies that redirect guests to web check-in (no WA document OCR)."""

from __future__ import annotations

import logging

from django.utils import timezone

from apps.communications.canonical_store import create_with_canonical
from apps.communications.guest_compose import render_autocheckin_web_checkin_message
from apps.communications.models import (
    GuestMessageChannel,
    GuestMessageDraft,
    GuestMessageIntent,
    GuestOutboundMessage,
    GuestOutboundMessageStatus,
)
from apps.integrations.models import IntegrationConfig, WhatsAppMessage
from apps.integrations.whatsapp.client import (
    WhatsAppApiError,
    extract_outbound_wamid,
    send_text_message,
)
from apps.integrations.whatsapp.runtime_config import WhatsAppRuntimeConfig
from apps.reservations.guest_checkin_orchestrator import GuestCheckInOrchestrator
from apps.reservations.guest_checkin_session import mark_checkin_link_distributed
from apps.reservations.models import GuestCheckInSessionCreatedFrom, Reservation

logger = logging.getLogger(__name__)


def send_guest_web_checkin_link_reply(
    *,
    row: WhatsAppMessage,
    integration_row: IntegrationConfig,
    runtime: WhatsAppRuntimeConfig,
    reservation: Reservation,
    hint: str = "autocheckin web check-in",
) -> dict:
    """Ensure web session exists and send check-in URL (no WA document upload)."""
    if not runtime.send_credentials_ok():
        return {"status": "missing_credentials"}

    session_result = GuestCheckInOrchestrator.ensure_session_and_link(
        reservation,
        created_from=GuestCheckInSessionCreatedFrom.WHATSAPP_AUTOCHECKIN,
        wa_id=row.wa_id,
    )
    body = render_autocheckin_web_checkin_message(
        reservation,
        checkin_url=session_result.url,
    )

    try:
        response = send_text_message(
            phone_number_id=runtime.phone_number_id,
            access_token=runtime.access_token,
            to_wa_id=row.wa_id,
            body=body,
        )
    except WhatsAppApiError as exc:
        logger.warning(
            "WhatsApp web check-in reply failed message_id=%s: %s",
            row.pk,
            exc,
        )
        return {"status": "send_failed", "detail": str(exc)}

    outbound_wamid = extract_outbound_wamid(response)
    if outbound_wamid:
        create_with_canonical(WhatsAppMessage,
            tenant_id=row.tenant_id,
            integration=integration_row,
            reservation=reservation,
            wamid=outbound_wamid,
            wa_id=row.wa_id,
            phone_number_id=runtime.phone_number_id,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            message_type="text",
            body=body,
            raw_payload=response,
        )

    draft = GuestMessageDraft.objects.create(
        tenant_id=row.tenant_id,
        reservation=reservation,
        intent=GuestMessageIntent.CHECKIN,
        hint=hint,
        language="",
        llm_body_text=body,
        final_body_text=body,
        channel=GuestMessageChannel.WHATSAPP,
        sent_at=timezone.now(),
    )
    create_with_canonical(GuestOutboundMessage,
        tenant_id=row.tenant_id,
        reservation=reservation,
        draft=draft,
        channel=GuestMessageChannel.WHATSAPP,
        body_text=body,
        status=GuestOutboundMessageStatus.SENT,
        to_phone=reservation.booker_phone or row.wa_id,
    )

    mark_checkin_link_distributed(
        session_result.session,
        distributed_from=GuestCheckInSessionCreatedFrom.WHATSAPP_AUTOCHECKIN,
        wa_id=row.wa_id,
    )

    return {
        "status": "web_checkin_sent",
        "outbound_wamid": outbound_wamid,
        "checkin_url": session_result.url,
    }
