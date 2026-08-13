"""WhatsApp auto check-in maintenance guard (deploy / migration window)."""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from apps.communications.canonical_store import create_with_canonical
from apps.communications.guest_compose import (
    HINT_WHATSAPP_AUTOCHECKIN_MAINTENANCE,
    render_whatsapp_autocheckin_maintenance_message,
)
from apps.communications.models import (
    GuestMessageChannel,
    GuestMessageDraft,
    GuestMessageIntent,
    GuestOutboundMessage,
    GuestOutboundMessageStatus,
)
from apps.integrations.models import IntegrationConfig, WhatsAppMessage
from apps.integrations.whatsapp.client import WhatsAppApiError, extract_outbound_wamid, send_text_message
from apps.integrations.whatsapp.runtime_config import WhatsAppRuntimeConfig
from apps.reservations.models import Reservation

logger = logging.getLogger(__name__)

_MAINTENANCE_REPLY_CACHE_PREFIX = "wa-autocheckin-maintenance-reply"
_MAINTENANCE_REPLY_CACHE_TTL = 3600


def whatsapp_autocheckin_maintenance_enabled() -> bool:
    return bool(settings.WHATSAPP_AUTOCHECKIN_MAINTENANCE)


def _maintenance_reply_cache_key(reservation_id: int) -> str:
    return f"{_MAINTENANCE_REPLY_CACHE_PREFIX}:{reservation_id}"


def send_autocheckin_maintenance_reply(
    *,
    row: WhatsAppMessage,
    integration_row: IntegrationConfig,
    runtime: WhatsAppRuntimeConfig,
    reservation: Reservation,
    dedupe: bool = True,
) -> dict:
    """Send controlled maintenance message to guest; optional once-per-hour dedupe."""
    if dedupe:
        cache_key = _maintenance_reply_cache_key(reservation.pk)
        if cache.get(cache_key):
            return {"status": "maintenance", "reason": "already_notified"}

    if not runtime.send_credentials_ok():
        return {"status": "maintenance", "reason": "missing_credentials"}

    body = render_whatsapp_autocheckin_maintenance_message(reservation)
    try:
        response = send_text_message(
            phone_number_id=runtime.phone_number_id,
            access_token=runtime.access_token,
            to_wa_id=row.wa_id,
            body=body,
        )
    except WhatsAppApiError as exc:
        logger.warning(
            "WhatsApp autocheckin maintenance reply failed message_id=%s: %s",
            row.pk,
            exc,
        )
        return {"status": "maintenance", "reason": "send_failed", "detail": str(exc)}

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
        intent=GuestMessageIntent.REPLY,
        hint=HINT_WHATSAPP_AUTOCHECKIN_MAINTENANCE,
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

    if dedupe:
        cache.set(_maintenance_reply_cache_key(reservation.pk), True, _MAINTENANCE_REPLY_CACHE_TTL)

    return {"status": "maintenance", "reason": "notified", "wamid": outbound_wamid}
