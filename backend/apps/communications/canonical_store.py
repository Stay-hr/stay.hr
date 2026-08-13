"""ADR 0019 Phase D2 — dual-write raw ingest rows onto Conversation / GuestMessage.

GET still reads ``timeline_for_reservation``. This module only records canonical
identity. Call after every raw upsert (create and existing), inside the same
``transaction.atomic()`` as the raw write. Provider I/O stays outside.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import Prefetch
from django.utils.dateparse import parse_datetime

from apps.communications.guest_message_timeline import (
    MERGE_WINDOW_INBOUND_SECONDS,
    MERGE_WINDOW_OUTBOUND_SECONDS,
    _should_merge_items,
    serialize_channex,
    serialize_inbound,
    serialize_outbound,
    serialize_whatsapp,
)
from apps.communications.models import (
    Conversation,
    GuestInboundMessage,
    GuestMessage,
    GuestMessageChannel,
    GuestMessageDirection,
    GuestMessageSource,
    GuestMessageSourceProvider,
    GuestOutboundDeliveryStatus,
    GuestOutboundMessage,
    GuestOutboundMessageStatus,
)
from apps.integrations.models import ChannexMessage, WhatsAppMessage
from apps.reservations.models import Reservation

logger = logging.getLogger(__name__)

_SYNTHETIC_ID_PREFIXES = (
    "local-outbound:",
    "local-outbound-image:",
    "local.outbound.image.",
    "local-inbound:",
)


class CanonicalSourceConversationMismatch(Exception):
    """Source already belongs to another Conversation; D2 does not move it."""


def record_canonical_source(raw) -> GuestMessage | None:
    """Upsert GuestMessageSource for ``raw``. Skip if unrouted. Never touches/FCM."""
    reservation_id = getattr(raw, "reservation_id", None)
    if not reservation_id:
        return None

    reservation = _reservation(raw)
    tenant_id = reservation.tenant_id
    provider = _provider(raw)
    provider_message_id = _canonical_provider_message_id(raw)
    incoming = _timeline_item_for_raw(raw)

    with transaction.atomic():
        conversation = _lock_conversation(tenant_id=tenant_id, reservation_id=reservation.pk)
        source = _lookup_source(
            tenant_id=tenant_id,
            provider=provider,
            provider_message_id=provider_message_id,
            raw=raw,
        )
        if source is not None:
            _assert_same_conversation(source, conversation, raw)
            return source.message

        candidates = _lock_heuristic_candidates(
            conversation,
            direction=incoming.get("direction") or "",
            occurred_at=_occurred_at_from_item(incoming),
        )
        matched = _heuristic_match(incoming, candidates)
        try:
            with transaction.atomic():
                if matched is not None:
                    _insert_source(
                        message=matched,
                        raw=raw,
                        tenant_id=tenant_id,
                        provider=provider,
                        provider_message_id=provider_message_id,
                    )
                    _enrich_message(matched, raw, incoming)
                    _reconcile_channel(matched)
                    return matched

                message = _insert_message(
                    conversation=conversation,
                    tenant_id=tenant_id,
                    raw=raw,
                    incoming=incoming,
                )
                _insert_source(
                    message=message,
                    raw=raw,
                    tenant_id=tenant_id,
                    provider=provider,
                    provider_message_id=provider_message_id,
                )
                _reconcile_channel(message)
                return message
        except IntegrityError:
            source = _lookup_source(
                tenant_id=tenant_id,
                provider=provider,
                provider_message_id=provider_message_id,
                raw=raw,
            )
            if source is None:
                raise
            _assert_same_conversation(source, conversation, raw)
            return source.message


def link_raw_reservation(raw, reservation: Reservation) -> None:
    """Set ``raw.reservation`` and dual-write in one atomic unit."""
    with transaction.atomic():
        raw.reservation = reservation
        raw.save(update_fields=["reservation"])
        record_canonical_source(raw)


def create_with_canonical(model, **kwargs):
    """Create a raw row and dual-write in one atomic unit."""
    with transaction.atomic():
        row = model.objects.create(**kwargs)
        record_canonical_source(row)
        return row


def _reservation(raw) -> Reservation:
    reservation = getattr(raw, "reservation", None)
    if reservation is not None and getattr(reservation, "tenant_id", None):
        return reservation
    return Reservation.objects.select_related("tenant").get(pk=raw.reservation_id)


def _lock_conversation(*, tenant_id: int, reservation_id: int) -> Conversation:
    conversation = (
        Conversation.objects.select_for_update()
        .filter(tenant_id=tenant_id, reservation_id=reservation_id)
        .first()
    )
    if conversation is not None:
        return conversation
    try:
        with transaction.atomic():
            return Conversation.objects.create(
                tenant_id=tenant_id,
                reservation_id=reservation_id,
            )
    except IntegrityError:
        return (
            Conversation.objects.select_for_update()
            .get(tenant_id=tenant_id, reservation_id=reservation_id)
        )


def _lookup_source(
    *,
    tenant_id: int,
    provider: str,
    provider_message_id: str | None,
    raw,
) -> GuestMessageSource | None:
    qs = GuestMessageSource.objects.select_related("message", "message__conversation")
    field = _raw_fk_field(raw)
    found = qs.filter(**{field: raw}).first()
    if found is not None:
        return found
    if provider_message_id:
        return qs.filter(
            tenant_id=tenant_id,
            provider=provider,
            provider_message_id=provider_message_id,
        ).first()
    return None


def _assert_same_conversation(
    source: GuestMessageSource,
    conversation: Conversation,
    raw,
) -> None:
    source_conversation_id = source.message.conversation_id
    if source_conversation_id == conversation.pk:
        return
    logger.error(
        "canonical_source_conversation_mismatch",
        extra={
            "event": "canonical_source_conversation_mismatch",
            "source_id": source.pk,
            "source_conversation_id": source_conversation_id,
            "expected_conversation_id": conversation.pk,
            "raw_model": type(raw).__name__,
            "raw_pk": raw.pk,
            "reservation_id": getattr(raw, "reservation_id", None),
        },
    )
    raise CanonicalSourceConversationMismatch(
        "GuestMessageSource belongs to a different Conversation"
    )


def _lock_heuristic_candidates(
    conversation: Conversation,
    *,
    direction: str,
    occurred_at,
) -> list[GuestMessage]:
    if occurred_at is None or not direction:
        return []
    window = (
        MERGE_WINDOW_INBOUND_SECONDS
        if direction == GuestMessageDirection.INBOUND
        else MERGE_WINDOW_OUTBOUND_SECONDS
    )
    qs = (
        GuestMessage.objects.select_for_update()
        .filter(
            conversation=conversation,
            direction=direction,
            is_visible=True,
            occurred_at__gte=occurred_at - timedelta(seconds=window),
            occurred_at__lte=occurred_at + timedelta(seconds=window),
        )
        .prefetch_related(
            Prefetch(
                "sources",
                queryset=GuestMessageSource.objects.select_related(
                    "channex_message",
                    "whatsapp_message",
                    "inbound_message",
                    "outbound_message",
                ),
            )
        )
        .order_by("occurred_at", "id")
    )
    return list(qs)


def _heuristic_match(incoming: dict[str, Any], candidates: list[GuestMessage]) -> GuestMessage | None:
    for message in candidates:
        for source in message.sources.all():
            existing_raw = _raw_from_source(source)
            if existing_raw is None:
                continue
            if _should_merge_items(incoming, _timeline_item_for_raw(existing_raw)):
                return message
        fallback = {
            "direction": message.direction,
            "body_text": message.body,
            "created_at": message.occurred_at.isoformat(),
            "message_type": "image" if message.media_file else "text",
            "media_url": None,
        }
        if _should_merge_items(incoming, fallback):
            return message
    return None


def _insert_message(
    *,
    conversation: Conversation,
    tenant_id: int,
    raw,
    incoming: dict[str, Any],
) -> GuestMessage:
    occurred_at = _occurred_at_from_item(incoming)
    if occurred_at is None:
        occurred_at = getattr(raw, "created_at", None)
    message = GuestMessage(
        tenant_id=tenant_id,
        conversation=conversation,
        direction=incoming.get("direction") or GuestMessageDirection.INBOUND,
        channel=_product_channel(raw),
        body=(incoming.get("body_text") or "")[:],
        occurred_at=occurred_at,
        delivery_status=_delivery_status(raw),
        is_visible=True,
    )
    media = _raw_media_file(raw)
    if media:
        message.media_file = media
    message.save()
    return message


def _insert_source(
    *,
    message: GuestMessage,
    raw,
    tenant_id: int,
    provider: str,
    provider_message_id: str | None,
) -> GuestMessageSource:
    kwargs: dict[str, Any] = {
        "tenant_id": tenant_id,
        "message": message,
        "provider": provider,
        "provider_message_id": provider_message_id,
        _raw_fk_field(raw): raw,
    }
    return GuestMessageSource.objects.create(**kwargs)


def _enrich_message(message: GuestMessage, raw, incoming: dict[str, Any]) -> None:
    update_fields: list[str] = []
    incoming_body = (incoming.get("body_text") or "").strip()
    if incoming_body and (
        not (message.body or "").strip() or len(incoming_body) > len((message.body or "").strip())
    ):
        message.body = incoming.get("body_text") or ""
        update_fields.append("body")
    occurred_at = _occurred_at_from_item(incoming)
    if occurred_at is not None and occurred_at < message.occurred_at:
        message.occurred_at = occurred_at
        update_fields.append("occurred_at")
    if not message.media_file:
        media = _raw_media_file(raw)
        if media:
            message.media_file = media
            update_fields.append("media_file")
    if not (message.delivery_status or "").strip():
        status = _delivery_status(raw)
        if status:
            message.delivery_status = status
            update_fields.append("delivery_status")
    if update_fields:
        message.save(update_fields=update_fields)


def _reconcile_channel(message: GuestMessage) -> None:
    has_channex = message.sources.filter(channex_message__isnull=False).exists()
    if has_channex and message.channel != GuestMessageChannel.BOOKING:
        message.channel = GuestMessageChannel.BOOKING
        message.save(update_fields=["channel"])


def _timeline_item_for_raw(raw) -> dict[str, Any]:
    if isinstance(raw, ChannexMessage):
        return serialize_channex(raw)
    if isinstance(raw, WhatsAppMessage):
        return serialize_whatsapp(raw)
    if isinstance(raw, GuestInboundMessage):
        return serialize_inbound(raw)
    if isinstance(raw, GuestOutboundMessage):
        return serialize_outbound(raw)
    raise TypeError(f"Unsupported raw type: {type(raw)!r}")


def _provider(raw) -> str:
    if isinstance(raw, ChannexMessage):
        return GuestMessageSourceProvider.CHANNEX
    if isinstance(raw, WhatsAppMessage):
        return GuestMessageSourceProvider.WABA
    if isinstance(raw, GuestInboundMessage):
        return GuestMessageSourceProvider.IMAP
    if isinstance(raw, GuestOutboundMessage):
        if raw.channel == GuestMessageChannel.WHATSAPP:
            return GuestMessageSourceProvider.STAY_OUTBOUND
        return GuestMessageSourceProvider.SMTP
    raise TypeError(f"Unsupported raw type: {type(raw)!r}")


def _product_channel(raw) -> str:
    if isinstance(raw, ChannexMessage):
        return GuestMessageChannel.BOOKING
    if isinstance(raw, WhatsAppMessage):
        return GuestMessageChannel.WHATSAPP
    if isinstance(raw, GuestInboundMessage):
        return raw.channel or GuestMessageChannel.EMAIL
    if isinstance(raw, GuestOutboundMessage):
        return raw.channel
    return GuestMessageChannel.EMAIL


def _raw_provider_id(raw) -> str:
    if isinstance(raw, ChannexMessage):
        return raw.channex_message_id or ""
    if isinstance(raw, WhatsAppMessage):
        return raw.wamid or ""
    if isinstance(raw, GuestInboundMessage):
        return raw.message_id or ""
    if isinstance(raw, GuestOutboundMessage):
        return raw.provider_message_id or ""
    return ""


def _canonical_provider_message_id(raw) -> str | None:
    value = (_raw_provider_id(raw) or "").strip()
    if not value:
        return None
    lowered = value.lower()
    if any(value.startswith(prefix) or lowered.startswith(prefix) for prefix in _SYNTHETIC_ID_PREFIXES):
        return None
    return value


def _raw_fk_field(raw) -> str:
    if isinstance(raw, ChannexMessage):
        return "channex_message"
    if isinstance(raw, WhatsAppMessage):
        return "whatsapp_message"
    if isinstance(raw, GuestInboundMessage):
        return "inbound_message"
    if isinstance(raw, GuestOutboundMessage):
        return "outbound_message"
    raise TypeError(f"Unsupported raw type: {type(raw)!r}")


def _raw_from_source(source: GuestMessageSource):
    return (
        source.channex_message
        or source.whatsapp_message
        or source.inbound_message
        or source.outbound_message
    )


def _raw_media_file(raw):
    media = getattr(raw, "media_file", None)
    if media:
        return media
    return None


def _delivery_status(raw) -> str:
    if isinstance(raw, GuestOutboundMessage):
        delivery = (raw.delivery_status or "").strip()
        if delivery:
            return delivery
        if raw.status == GuestOutboundMessageStatus.SENT:
            return GuestOutboundDeliveryStatus.SENT
        if raw.status == GuestOutboundMessageStatus.FAILED:
            return GuestOutboundDeliveryStatus.FAILED
        return ""
    if isinstance(raw, ChannexMessage) and raw.sender == ChannexMessage.Sender.PROPERTY:
        return GuestOutboundDeliveryStatus.SENT
    if isinstance(raw, WhatsAppMessage) and raw.direction == WhatsAppMessage.Direction.OUTBOUND:
        return GuestOutboundDeliveryStatus.SENT
    return ""


def _occurred_at_from_item(item: dict[str, Any]):
    parsed = parse_datetime(item.get("created_at") or "")
    return parsed
