from __future__ import annotations

import logging
import os

from django.utils import timezone

from apps.communications.canonical_store import create_with_canonical
from apps.communications.guest_compose import (
    HINT_EVISITOR_REGISTERED,
    render_evisitor_registered_message,
)
from apps.communications.models import (
    GuestMessageChannel,
    GuestMessageDraft,
    GuestMessageIntent,
    GuestOutboundMessage,
    GuestOutboundMessageStatus,
)
from apps.integrations.evisitor.summary import evisitor_summary_for_reservation
from apps.integrations.models import WhatsAppMessage
from apps.integrations.whatsapp.client import WhatsAppApiError, extract_outbound_wamid, send_text_message
from apps.integrations.whatsapp.integration_lookup import resolve_whatsapp_integration
from apps.reservations.models import Reservation

logger = logging.getLogger(__name__)


def evisitor_registered_reply_enabled() -> bool:
    raw = os.getenv("WHATSAPP_EVISITOR_REGISTERED_REPLY", "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _wa_id_for_reservation(reservation: Reservation) -> str:
    wa_id = (
        WhatsAppMessage.objects.filter(
            reservation_id=reservation.pk,
            direction=WhatsAppMessage.Direction.INBOUND,
        )
        .order_by("-id")
        .values_list("wa_id", flat=True)
        .first()
    )
    return (wa_id or "").strip()


def _evisitor_registered_whatsapp_already_sent(reservation_id: int) -> bool:
    return GuestMessageDraft.objects.filter(
        reservation_id=reservation_id,
        hint=HINT_EVISITOR_REGISTERED,
        channel=GuestMessageChannel.WHATSAPP,
    ).exists()


def _send_reservation_whatsapp_text(
    *,
    reservation: Reservation,
    body: str,
    hint: str,
) -> dict:
    wa_id = _wa_id_for_reservation(reservation)
    if not wa_id:
        return {"status": "skipped", "reason": "no_wa_id"}

    integration_row, runtime = resolve_whatsapp_integration(reservation.tenant)
    if integration_row is None or runtime is None or not runtime.send_credentials_ok():
        return {"status": "skipped", "reason": "no_credentials"}

    try:
        response = send_text_message(
            phone_number_id=runtime.phone_number_id,
            access_token=runtime.access_token,
            to_wa_id=wa_id,
            body=body,
        )
    except WhatsAppApiError as exc:
        logger.warning(
            "WhatsApp eVisitor registered reply failed reservation_id=%s: %s",
            reservation.pk,
            exc,
        )
        return {"status": "send_failed", "detail": str(exc)}

    outbound_wamid = extract_outbound_wamid(response)
    if outbound_wamid:
        create_with_canonical(WhatsAppMessage,
            tenant_id=reservation.tenant_id,
            integration=integration_row,
            reservation=reservation,
            wamid=outbound_wamid,
            wa_id=wa_id,
            phone_number_id=runtime.phone_number_id,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            message_type="text",
            body=body,
            raw_payload=response,
        )

    draft = GuestMessageDraft.objects.create(
        tenant_id=reservation.tenant_id,
        reservation=reservation,
        intent=GuestMessageIntent.REPLY,
        hint=hint,
        language="",
        llm_body_text=body,
        final_body_text=body,
        channel=GuestMessageChannel.WHATSAPP,
        sent_at=timezone.now(),
    )
    create_with_canonical(GuestOutboundMessage,
        tenant_id=reservation.tenant_id,
        reservation=reservation,
        draft=draft,
        channel=GuestMessageChannel.WHATSAPP,
        body_text=body,
        status=GuestOutboundMessageStatus.SENT,
        to_phone=reservation.booker_phone or wa_id,
    )

    return {"status": "sent", "wamid": outbound_wamid}


def maybe_send_evisitor_registered_whatsapp_reply(reservation: Reservation) -> dict:
    """Send one WhatsApp message when all eVisitor-required guests are submitted."""
    if not evisitor_registered_reply_enabled():
        return {"status": "disabled"}

    if evisitor_summary_for_reservation(reservation) != "complete":
        return {"status": "skipped", "reason": "evisitor_incomplete"}

    if _evisitor_registered_whatsapp_already_sent(reservation.pk):
        return {"status": "already_sent"}

    body = render_evisitor_registered_message(reservation)
    return _send_reservation_whatsapp_text(
        reservation=reservation,
        body=body,
        hint=HINT_EVISITOR_REGISTERED,
    )
