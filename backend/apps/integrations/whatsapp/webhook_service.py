from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.communications.conversation_ingest_status import mark_conversation_ingest
from apps.communications.models import GuestOutboundDeliveryStatus, GuestOutboundMessage
from apps.integrations.models import IntegrationConfig, WhatsAppMessage
from apps.integrations.whatsapp.media_download import extract_media_from_message
from apps.integrations.whatsapp.platform_inbound_router import (
    resolve_business_app_echo_reservation,
    route_inbound_message,
)
from apps.integrations.whatsapp.tasks import process_inbound_message
from apps.reservations.models import ReservationVersionScope
from apps.reservations.reservation_version import touch_reservation_version

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParsedInboundMessage:
    phone_number_id: str
    wa_id: str
    wamid: str
    message_type: str
    body: str
    profile_name: str
    raw_message: dict[str, Any]


@dataclass(frozen=True)
class ParsedMessageEcho:
    phone_number_id: str
    wa_id: str
    wamid: str
    message_type: str
    body: str
    received_at: datetime
    raw_echo: dict[str, Any]


def _extract_display_body(message: dict[str, Any], message_type: str) -> str:
    if message_type == "text":
        return str((message.get("text") or {}).get("body") or "").strip()
    if message_type == "interactive":
        interactive = message.get("interactive") or {}
        interactive_type = str(interactive.get("type") or "").strip()
        if interactive_type == "button_reply":
            return str(
                (interactive.get("button_reply") or {}).get("title") or ""
            ).strip()
        if interactive_type == "list_reply":
            return str(
                (interactive.get("list_reply") or {}).get("title") or ""
            ).strip()
        return ""
    if message_type == "button":
        button = message.get("button") or {}
        return str(button.get("text") or button.get("payload") or "").strip()
    try:
        _, _, caption = extract_media_from_message(message)
        return caption or ""
    except Exception:
        return ""


def _parse_meta_timestamp(raw: Any) -> datetime:
    try:
        ts = int(str(raw).strip())
        return datetime.fromtimestamp(ts, tz=dt_timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return timezone.now()


def extract_inbound_messages(body: dict[str, Any]) -> list[ParsedInboundMessage]:
    if body.get("object") != "whatsapp_business_account":
        return []

    messages: list[ParsedInboundMessage] = []
    for entry in body.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            metadata = value.get("metadata") or {}
            phone_number_id = str(metadata.get("phone_number_id") or "").strip()
            contacts = {
                str(contact.get("wa_id") or "").strip(): str(
                    (contact.get("profile") or {}).get("name") or ""
                ).strip()
                for contact in value.get("contacts") or []
            }
            for message in value.get("messages") or []:
                wa_id = str(message.get("from") or "").strip()
                wamid = str(message.get("id") or "").strip()
                message_type = str(message.get("type") or "").strip() or "unknown"
                text_body = _extract_display_body(message, message_type)
                messages.append(
                    ParsedInboundMessage(
                        phone_number_id=phone_number_id,
                        wa_id=wa_id,
                        wamid=wamid,
                        message_type=message_type,
                        body=text_body,
                        profile_name=contacts.get(wa_id, ""),
                        raw_message=message,
                    )
                )
    return messages


def extract_message_echoes(body: dict[str, Any]) -> list[ParsedMessageEcho]:
    if body.get("object") != "whatsapp_business_account":
        return []

    echoes: list[ParsedMessageEcho] = []
    for entry in body.get("entry") or []:
        for change in entry.get("changes") or []:
            if str(change.get("field") or "").strip() != "smb_message_echoes":
                continue
            value = change.get("value") or {}
            metadata = value.get("metadata") or {}
            phone_number_id = str(metadata.get("phone_number_id") or "").strip()
            for echo in value.get("message_echoes") or []:
                if not isinstance(echo, dict):
                    continue
                # Guest wa_id is `to` (Business App → guest); `from` is the WABA number.
                wa_id = str(echo.get("to") or "").strip()
                wamid = str(echo.get("id") or "").strip()
                message_type = str(echo.get("type") or "").strip() or "unknown"
                body_text = _extract_display_body(echo, message_type)
                echoes.append(
                    ParsedMessageEcho(
                        phone_number_id=phone_number_id,
                        wa_id=wa_id,
                        wamid=wamid,
                        message_type=message_type,
                        body=body_text,
                        received_at=_parse_meta_timestamp(echo.get("timestamp")),
                        raw_echo=echo,
                    )
                )
    return echoes


def record_inbound_whatsapp_message(
    *,
    integration_row: IntegrationConfig,
    parsed: ParsedInboundMessage,
) -> dict[str, Any]:
    if not parsed.wamid:
        return {"status": "ignored", "reason": "missing_wamid"}

    try:
        row, created = WhatsAppMessage.objects.get_or_create(
            wamid=parsed.wamid,
            defaults={
                "tenant_id": integration_row.tenant_id,
                "integration": integration_row,
                "wa_id": parsed.wa_id,
                "phone_number_id": parsed.phone_number_id,
                "direction": WhatsAppMessage.Direction.INBOUND,
                "source": WhatsAppMessage.Source.CLOUD_API,
                "message_type": parsed.message_type,
                "body": parsed.body,
                "raw_payload": parsed.raw_message,
            },
        )
    except IntegrityError:
        mark_conversation_ingest("whatsapp", "webhook")
        return {"status": "duplicate", "wamid": parsed.wamid}

    mark_conversation_ingest("whatsapp", "webhook")
    if not created:
        return {"status": "duplicate", "wamid": parsed.wamid}

    routing = route_inbound_message(message=row, integration=integration_row)
    process_inbound_message.delay(row.pk, profile_name=parsed.profile_name)
    return {
        "status": "queued",
        "message_id": row.pk,
        "wamid": parsed.wamid,
        "routing_status": routing.status,
    }


def record_business_app_echo(
    *,
    integration_row: IntegrationConfig,
    parsed: ParsedMessageEcho,
) -> dict[str, Any]:
    """Store Business App outbound echo. No inbound automation."""
    if not parsed.wamid:
        return {"status": "ignored", "reason": "missing_wamid"}

    if WhatsAppMessage.objects.filter(wamid=parsed.wamid).exists():
        logger.debug(
            "Business app echo duplicate wamid=%s (message-level idempotency)",
            parsed.wamid,
        )
        return {"status": "duplicate", "wamid": parsed.wamid}

    reservation, matched_by = resolve_business_app_echo_reservation(
        wa_id=parsed.wa_id,
        integration=integration_row,
    )

    try:
        with transaction.atomic():
            row, created = WhatsAppMessage.objects.get_or_create(
                wamid=parsed.wamid,
                defaults={
                    "tenant_id": integration_row.tenant_id,
                    "integration": integration_row,
                    "reservation": reservation,
                    "wa_id": parsed.wa_id,
                    "phone_number_id": parsed.phone_number_id,
                    "direction": WhatsAppMessage.Direction.OUTBOUND,
                    "source": WhatsAppMessage.Source.BUSINESS_APP,
                    "message_type": parsed.message_type,
                    "body": parsed.body,
                    "raw_payload": parsed.raw_echo,
                    "received_at": parsed.received_at,
                },
            )
            if not created:
                return {"status": "duplicate", "wamid": parsed.wamid}

            if reservation is not None:
                touch_reservation_version(
                    reservation.pk,
                    ReservationVersionScope.MESSAGES,
                    reason="whatsapp_business_app_echo",
                )
    except IntegrityError:
        return {"status": "duplicate", "wamid": parsed.wamid}

    logger.info(
        "Business app echo stored integration_id=%s wamid=%s reservation_id=%s matched_by=%s",
        integration_row.pk,
        parsed.wamid,
        reservation.pk if reservation else None,
        matched_by,
    )
    return {
        "status": "stored",
        "message_id": row.pk,
        "wamid": parsed.wamid,
        "reservation_id": reservation.pk if reservation else None,
        "matched_by": matched_by,
    }


def extract_status_updates(body: dict[str, Any]) -> list[dict[str, str]]:
    if body.get("object") != "whatsapp_business_account":
        return []
    updates: list[dict[str, str]] = []
    for entry in body.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            for item in value.get("statuses") or []:
                wamid = str(item.get("id") or "").strip()
                status = str(item.get("status") or "").strip().lower()
                if wamid and status:
                    updates.append({"wamid": wamid, "status": status})
    return updates


def apply_outbound_status_update(*, wamid: str, status: str) -> dict[str, Any]:
    mapping = {
        "sent": GuestOutboundDeliveryStatus.SENT,
        "delivered": GuestOutboundDeliveryStatus.DELIVERED,
        "read": GuestOutboundDeliveryStatus.READ,
        "failed": GuestOutboundDeliveryStatus.FAILED,
    }
    delivery_status = mapping.get(status)
    if not delivery_status:
        return {"status": "ignored", "wamid": wamid}

    updated = GuestOutboundMessage.objects.filter(provider_message_id=wamid).update(
        delivery_status=delivery_status,
    )
    if updated:
        return {"status": "updated", "wamid": wamid, "delivery_status": delivery_status}
    return {"status": "not_found", "wamid": wamid}


def process_whatsapp_webhook(body: dict[str, Any]) -> dict[str, Any]:
    from apps.integrations.whatsapp.resolver import find_whatsapp_integration

    status_updates = extract_status_updates(body)
    status_results = [apply_outbound_status_update(**item) for item in status_updates]

    results: list[dict[str, Any]] = list(status_results)

    for parsed in extract_inbound_messages(body):
        integration_row = None
        if parsed.phone_number_id:
            integration_row = find_whatsapp_integration(parsed.phone_number_id)
        if integration_row is None:
            logger.warning(
                "whatsapp webhook: no integration for phone_number_id=%s",
                parsed.phone_number_id,
            )
            results.append(
                {
                    "status": "unrouted",
                    "phone_number_id": parsed.phone_number_id,
                    "wamid": parsed.wamid,
                }
            )
            continue

        results.append(
            record_inbound_whatsapp_message(
                integration_row=integration_row,
                parsed=parsed,
            )
        )

    for parsed in extract_message_echoes(body):
        integration_row = None
        if parsed.phone_number_id:
            integration_row = find_whatsapp_integration(parsed.phone_number_id)
        if integration_row is None:
            logger.warning(
                "whatsapp webhook: no integration for smb_message_echoes phone_number_id=%s",
                parsed.phone_number_id,
            )
            results.append(
                {
                    "status": "unrouted",
                    "phone_number_id": parsed.phone_number_id,
                    "wamid": parsed.wamid,
                }
            )
            continue

        results.append(
            record_business_app_echo(
                integration_row=integration_row,
                parsed=parsed,
            )
        )

    return {"status": "ok", "processed": len(results), "results": results}
