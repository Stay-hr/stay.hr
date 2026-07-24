"""Shared helpers for messaging provider adapters (ADR 0010 Phase 4)."""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from apps.communications.messaging.context import TriggerContext
from apps.communications.messaging.intents import MessageDefinitionKey
from apps.communications.messaging.models import MessageDispatch, MessageErrorCategory
from apps.communications.models import (
    GuestMessageChannel,
    GuestMessageDraft,
    GuestMessageIntent,
    GuestOutboundMessage,
    GuestOutboundMessageStatus,
)
from apps.integrations.channex.exceptions import ChannexApiError, ChannexBookingIngestError
from apps.integrations.whatsapp.client import WhatsAppApiError
from apps.integrations.whatsapp.whatsapp_errors import (
    is_transient_whatsapp_error,
    parse_meta_api_error,
)
from apps.reservations.models import Reservation

logger = logging.getLogger(__name__)

PROVIDER_BOOKING = "booking"
PROVIDER_EMAIL = "email"
PROVIDER_WHATSAPP = "whatsapp"

_DEFINITION_INTENT: dict[str, str] = {
    MessageDefinitionKey.CHECKIN_INFO: GuestMessageIntent.CHECKIN,
    MessageDefinitionKey.CHECKIN_LINK: GuestMessageIntent.CHECKIN,
    MessageDefinitionKey.WELCOME: GuestMessageIntent.WELCOME_TEMPLATE,
}


def load_reservation(
    dispatch: MessageDispatch,
    ctx: TriggerContext,
) -> Reservation:
    """Resolve reservation for a dispatch; prefer the FK already on the row."""
    reservation = getattr(dispatch, "reservation", None)
    if reservation is not None and getattr(reservation, "pk", None):
        return reservation
    reservation_id = dispatch.reservation_id or ctx.reservation_id
    return Reservation.objects.select_related("property", "tenant").get(
        pk=reservation_id
    )


def snapshot_body(dispatch: MessageDispatch) -> str:
    """Frozen render body — adapters must not re-render."""
    return (dispatch.rendered_body or "").strip()


def snapshot_subject(dispatch: MessageDispatch) -> str:
    return (dispatch.rendered_subject or "").strip()


def draft_intent_for_definition(definition_key: str) -> str:
    return _DEFINITION_INTENT.get(definition_key, GuestMessageIntent.CUSTOM)


def draft_hint_for_dispatch(dispatch: MessageDispatch) -> str:
    return f"messaging:{dispatch.definition_key}:{dispatch.correlation_id}"


def create_timeline_draft(
    *,
    reservation: Reservation,
    dispatch: MessageDispatch,
    channel: str,
    body_text: str,
) -> GuestMessageDraft:
    """Create GuestMessageDraft for reception timeline compatibility."""
    return GuestMessageDraft.objects.create(
        tenant_id=reservation.tenant_id,
        reservation=reservation,
        intent=draft_intent_for_definition(dispatch.definition_key),
        hint=draft_hint_for_dispatch(dispatch),
        llm_body_text=body_text,
        final_body_text="",
        language=(dispatch.language or "")[:8],
        channel=channel,
    )


def outbound_succeeded(outbound: GuestOutboundMessage | Any) -> bool:
    """True when a send primitive left the channel row in a delivered state."""
    status = getattr(outbound, "status", "")
    if status == GuestOutboundMessageStatus.SENT:
        return True
    if status == GuestOutboundMessageStatus.HANDOFF_WHATSAPP:
        # Handoff is not an API delivery — treat as soft success for compose paths;
        # orchestration WELCOME uses Meta template and should not land here.
        return True
    delivery = getattr(outbound, "delivery_status", "") or ""
    if delivery in {"sent", "delivered", "read"}:
        return True
    return False


def categorize_send_exception(exc: BaseException) -> tuple[str, str, bool]:
    """Map channel exceptions → (error_category, error_code, retryable)."""
    if isinstance(exc, WhatsAppApiError):
        return _categorize_whatsapp(exc)
    if isinstance(exc, (ChannexApiError, ChannexBookingIngestError)):
        msg = str(exc).lower()
        if any(tok in msg for tok in ("401", "403", "unauthorized", "forbidden", "token")):
            return MessageErrorCategory.AUTH, "channex_auth", True
        if "429" in msg or "rate" in msg:
            return MessageErrorCategory.RATE_LIMIT, "channex_rate_limit", True
        if any(tok in msg for tok in ("timeout", "timed out", "502", "503", "504")):
            return MessageErrorCategory.NETWORK, "channex_network", True
        return MessageErrorCategory.PROVIDER, "channex_error", True
    if isinstance(exc, httpx.TimeoutException):
        return MessageErrorCategory.NETWORK, "http_timeout", True
    if isinstance(exc, httpx.HTTPError):
        return MessageErrorCategory.NETWORK, "http_error", True
    if isinstance(exc, ValueError):
        code = _slug_error_code(str(exc)) or "validation_error"
        return MessageErrorCategory.VALIDATION, code, False
    return MessageErrorCategory.UNKNOWN, "provider_exception", True


def _categorize_whatsapp(exc: WhatsAppApiError) -> tuple[str, str, bool]:
    parsed = parse_meta_api_error(exc)
    status = parsed.get("provider_status")
    meta_code = parsed.get("provider_error_code")
    if status in (401, 403):
        return MessageErrorCategory.AUTH, f"whatsapp_http_{status}", True
    if status == 429 or meta_code in (4, 80007, 130429):
        return MessageErrorCategory.RATE_LIMIT, "whatsapp_rate_limit", True
    if is_transient_whatsapp_error(exc):
        return MessageErrorCategory.NETWORK, "whatsapp_transient", True
    if status and int(status) >= 500:
        return MessageErrorCategory.NETWORK, f"whatsapp_http_{status}", True
    code = f"whatsapp_{meta_code}" if meta_code else "whatsapp_api_error"
    return MessageErrorCategory.PROVIDER, code, False


def categorize_reason_code(reason: str) -> tuple[str, str, bool]:
    """Map send-primitive reason strings (email dict / welcome status) to telemetry."""
    code = (reason or "unknown").strip() or "unknown"
    lowered = code.lower()
    validation = {
        "no_recipient",
        "no_email",
        "no_phone",
        "empty_body",
        "smtp_not_configured",
        "no_credentials",
        "whatsapp_not_configured",
        "booking_channel_unavailable",
        "channex_not_configured",
        "disabled",
        "maintenance",
        "not_expected",
        "web_checkin_completed",
        "autocheckin_waived",
        "guest_engaged",
        "no_whatsapp",
    }
    if lowered in validation:
        return MessageErrorCategory.VALIDATION, code, False
    if lowered in {"send_failed", "provider_error"}:
        return MessageErrorCategory.PROVIDER, code, True
    return MessageErrorCategory.UNKNOWN, code, False


def _slug_error_code(message: str, *, max_len: int = 64) -> str:
    text = (message or "").strip().lower()
    if not text:
        return ""
    # Prefer short machine codes like "booking_channel_unavailable".
    first = text.split(":", 1)[0].strip()
    slug = re.sub(r"[^a-z0-9_]+", "_", first).strip("_")
    return slug[:max_len]


def provider_message_id_from_outbound(outbound: GuestOutboundMessage | Any) -> str:
    mid = getattr(outbound, "provider_message_id", "") or ""
    if mid:
        return str(mid)
    # ChannexMessage
    channex_id = getattr(outbound, "channex_message_id", "") or ""
    return str(channex_id)
