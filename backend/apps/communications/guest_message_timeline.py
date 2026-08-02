"""Unified guest message timeline for a reservation (all channels)."""

from __future__ import annotations

import re
from datetime import datetime

from django.utils.dateparse import parse_datetime

from apps.communications.guest_message_body_format import (
    format_timeline_body_text,
    timeline_body_quality_score,
)
from apps.communications.models import (
    GuestInboundMessage,
    GuestMessageChannel,
    GuestOutboundMessage,
    GuestOutboundMessageStatus,
)
from apps.integrations.channex.message_service import first_attachment_path
from apps.integrations.models import ChannexMessage, WhatsAppMessage
from apps.reservations.models import DocumentIntakeJob, Reservation

WA_ID_OFFSET = 2_000_000_000
CHANNEX_ID_OFFSET = 3_000_000_000
INBOUND_ID_OFFSET = 4_000_000_000

_MEDIA_PREVIEW = {
    "image": "📷 Dokument poslan",
    "document": "📎 Datoteka poslana",
}

_OUTBOUND_IMAGE_PREVIEW = "📷 Slika poslana"

MERGE_WINDOW_OUTBOUND_SECONDS = 180
MERGE_WINDOW_INBOUND_SECONDS = 900  # Booking.com mail relay often lags Channex by several minutes
MERGE_WINDOW_SECONDS = MERGE_WINDOW_OUTBOUND_SECONDS

_SOURCE_PRIORITY = {
    "booking": 0,
    "whatsapp": 1,
    "outbound": 2,
    "inbound": 3,
}

_STATUS_PRIORITY = {
    "sent": 0,
    "handoff_whatsapp": 1,
    "queued": 2,
    "failed": 3,
}


def document_intake_image_url(job_id: int, index: int = 0) -> str:
    return f"/api/v1/reception/document-intake/jobs/{job_id}/images/{index}/"


def whatsapp_message_media_url(message_id: int) -> str:
    return f"/api/v1/reception/whatsapp-messages/{message_id}/media/"


def outbound_message_media_url(outbound_id: int) -> str:
    return f"/api/v1/reception/guest-outbound-messages/{outbound_id}/media/"


def channex_message_media_url(message_id: int) -> str:
    return f"/api/v1/reception/channex-messages/{message_id}/media/"


def media_kind_for_message_type(message_type: str) -> str | None:
    mt = (message_type or "").strip().lower()
    if mt in {"image", "document"}:
        return mt
    return None


def whatsapp_display_body(msg: WhatsAppMessage) -> str:
    body = (msg.body or "").strip()
    if body:
        return body
    return _MEDIA_PREVIEW.get(msg.message_type, "Poruka (WhatsApp)")


def serialize_outbound(outbound: GuestOutboundMessage) -> dict:
    app = outbound.api_application
    message_type = "text"
    media_url = None
    media_kind = None
    if getattr(outbound, "media_file", None) and outbound.media_file:
        message_type = "image"
        media_url = outbound_message_media_url(outbound.pk)
        media_kind = "image"
    return {
        "id": outbound.pk,
        "source": "outbound",
        "direction": "outbound",
        "channel": outbound.channel,
        "body_text": format_timeline_body_text(outbound.body_text),
        "created_at": outbound.created_at.isoformat(),
        "status": outbound.status,
        "provider_message_id": outbound.provider_message_id or None,
        "delivery_status": outbound.delivery_status or None,
        "sent_by_name": app.name if app else None,
        "from_email": None,
        "wa_me_url": outbound.wa_me_url or None,
        "message_type": message_type,
        "document_intake_job_id": None,
        "media_url": media_url,
        "media_kind": media_kind,
    }


def _whatsapp_media_fields(msg: WhatsAppMessage, job_id: int | None) -> tuple[str | None, str | None]:
    message_type = msg.message_type or "text"
    media_kind = media_kind_for_message_type(message_type)
    if not media_kind:
        return None, None
    if getattr(msg, "media_file", None) and msg.media_file:
        return whatsapp_message_media_url(msg.pk), media_kind
    if job_id is not None:
        return document_intake_image_url(job_id), media_kind
    return None, media_kind


def serialize_whatsapp(msg: WhatsAppMessage) -> dict:
    job_id = (
        DocumentIntakeJob.objects.filter(whatsapp_message_id=msg.pk)
        .values_list("pk", flat=True)
        .first()
    )
    is_outbound = msg.direction == WhatsAppMessage.Direction.OUTBOUND
    media_url, media_kind = _whatsapp_media_fields(msg, job_id)
    body = whatsapp_display_body(msg)
    if is_outbound and (msg.message_type or "") == "image" and not (msg.body or "").strip():
        body = _OUTBOUND_IMAGE_PREVIEW
    return {
        "id": WA_ID_OFFSET + msg.pk,
        "source": "whatsapp",
        "whatsapp_source": msg.source or WhatsAppMessage.Source.CLOUD_API,
        "direction": msg.direction,
        "channel": "whatsapp",
        "body_text": format_timeline_body_text(body),
        "created_at": msg.created_at.isoformat(),
        "status": "sent" if is_outbound else None,
        "sent_by_name": None,
        "from_email": None,
        "wa_me_url": None,
        "message_type": msg.message_type or "text",
        "document_intake_job_id": job_id,
        "media_url": media_url,
        "media_kind": media_kind,
    }


def _whatsapp_outbound_mirrors_guest_outbound(
    outbound: GuestOutboundMessage,
    whatsapp_rows: list[WhatsAppMessage],
) -> bool:
    """True when a WhatsAppMessage row already represents this API/handoff send."""
    body = (outbound.body_text or "").strip()
    if not body:
        return False
    for msg in whatsapp_rows:
        if msg.direction != WhatsAppMessage.Direction.OUTBOUND:
            continue
        if (msg.body or "").strip() != body:
            continue
        delta = abs((msg.created_at - outbound.created_at).total_seconds())
        if delta <= 5:
            return True
    return False


def serialize_inbound(inbound: GuestInboundMessage) -> dict:
    ts = inbound.received_at or inbound.created_at
    return {
        "id": INBOUND_ID_OFFSET + inbound.pk,
        "source": "inbound",
        "direction": "inbound",
        "channel": inbound.channel,
        "body_text": format_timeline_body_text(inbound.body_text or ""),
        "created_at": ts.isoformat(),
        "status": None,
        "sent_by_name": None,
        "from_email": inbound.from_email or None,
        "wa_me_url": None,
        "message_type": "text",
        "document_intake_job_id": None,
        "media_url": None,
        "media_kind": None,
    }


def serialize_channex(msg: ChannexMessage) -> dict:
    direction = "inbound" if msg.sender == ChannexMessage.Sender.GUEST else "outbound"
    message_type = "text"
    media_url = None
    media_kind = None
    body = (msg.body or "").strip()
    if getattr(msg, "media_file", None) and msg.media_file:
        message_type = "image"
        media_url = channex_message_media_url(msg.pk)
        media_kind = "image"
        if not body:
            body = _OUTBOUND_IMAGE_PREVIEW
    elif msg.have_attachment and first_attachment_path(msg.raw_payload or {}):
        message_type = "image"
        media_url = channex_message_media_url(msg.pk)
        media_kind = "image"
        if not body:
            body = _MEDIA_PREVIEW.get("image", "📷 Dokument poslan")
    return {
        "id": CHANNEX_ID_OFFSET + msg.pk,
        "source": "booking",
        "direction": direction,
        "channel": "booking",
        "body_text": format_timeline_body_text(body),
        "created_at": msg.created_at.isoformat(),
        "status": "sent" if direction == "outbound" else None,
        "sent_by_name": None,
        "from_email": None,
        "wa_me_url": None,
        "message_type": message_type,
        "document_intake_job_id": None,
        "media_url": media_url,
        "media_kind": media_kind,
    }


def _normalize_body_text(text: str) -> str:
    normalized = (text or "").replace("\r\n", "\n").strip()
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def _parse_item_datetime(item: dict) -> datetime | None:
    parsed = parse_datetime(item.get("created_at") or "")
    return parsed


def _bodies_match(body_a: str, body_b: str) -> bool:
    a = _normalize_body_text(body_a)
    b = _normalize_body_text(body_b)
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) >= 10 and len(b) >= 10 and (a in b or b in a):
        return True
    return False


def _media_merge_compatible(a: dict, b: dict) -> bool:
    type_a = (a.get("message_type") or "text").lower()
    type_b = (b.get("message_type") or "text").lower()
    media_types = {"image", "document"}
    if type_a not in media_types and type_b not in media_types:
        return True
    if type_a in media_types and type_b in media_types:
        return (a.get("media_url") or "") == (b.get("media_url") or "")
    return False


def _should_merge_items(a: dict, b: dict) -> bool:
    if a.get("direction") != b.get("direction"):
        return False
    if not _bodies_match(a.get("body_text") or "", b.get("body_text") or ""):
        return False
    if not _media_merge_compatible(a, b):
        return False
    dt_a = _parse_item_datetime(a)
    dt_b = _parse_item_datetime(b)
    if dt_a is None or dt_b is None:
        return False
    window = (
        MERGE_WINDOW_INBOUND_SECONDS
        if a.get("direction") == "inbound"
        else MERGE_WINDOW_OUTBOUND_SECONDS
    )
    return abs((dt_a - dt_b).total_seconds()) <= window


def _source_priority(item: dict) -> int:
    return _SOURCE_PRIORITY.get(item.get("source") or "", 99)


def _status_priority(status: str | None) -> int:
    if status is None:
        return 99
    return _STATUS_PRIORITY.get(status, 50)


def _pick_richer_field(items: list[dict], key: str):
    best = None
    best_score = -1
    for item in items:
        value = item.get(key)
        if value in (None, ""):
            continue
        score = 1
        if key == "status":
            score = 100 - _status_priority(value)
        if score > best_score:
            best_score = score
            best = value
    return best


def _merge_item_group(group: list[dict]) -> dict:
    if len(group) == 1:
        merged = dict(group[0])
        merged["channels"] = [merged["channel"]]
        return merged

    ordered = sorted(group, key=lambda item: item["created_at"])
    channels: list[str] = []
    seen_channels: set[str] = set()
    for item in ordered:
        channel = item.get("channel") or ""
        if channel and channel not in seen_channels:
            channels.append(channel)
            seen_channels.add(channel)

    primary = min(
        ordered,
        key=lambda item: (_source_priority(item), item["created_at"]),
    )
    merged = dict(primary)
    merged["body_text"] = max(
        ordered,
        key=lambda item: timeline_body_quality_score(item.get("body_text") or ""),
    )["body_text"]
    merged["body_text"] = format_timeline_body_text(merged["body_text"])
    merged["created_at"] = ordered[0]["created_at"]
    merged["channels"] = channels
    merged["channel"] = channels[0] if channels else primary.get("channel")

    for key in (
        "sent_by_name",
        "status",
        "media_url",
        "media_kind",
        "document_intake_job_id",
        "from_email",
        "wa_me_url",
    ):
        value = _pick_richer_field(ordered, key)
        if value not in (None, ""):
            merged[key] = value

    return merged


def merge_timeline_duplicates(items: list[dict]) -> list[dict]:
    """Collapse cross-channel duplicates into one entry with channels[]."""
    if not items:
        return []

    n = len(items)
    parent = list(range(n))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left = find(left)
        root_right = find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    for i in range(n):
        for j in range(i + 1, n):
            if _should_merge_items(items[i], items[j]):
                union(i, j)

    groups: dict[int, list[dict]] = {}
    for index, item in enumerate(items):
        root = find(index)
        groups.setdefault(root, []).append(item)

    merged = [_merge_item_group(group) for group in groups.values()]
    merged.sort(key=lambda item: item["created_at"])
    return merged


def timeline_for_reservation(reservation: Reservation) -> list[dict]:
    rows: list[tuple[str, dict]] = []

    whatsapp_rows = list(
        WhatsAppMessage.objects.filter(reservation=reservation).order_by("created_at", "pk")
    )

    for outbound in GuestOutboundMessage.objects.filter(reservation=reservation).select_related(
        "api_application"
    ):
        if (
            outbound.channel == GuestMessageChannel.WHATSAPP
            and outbound.status == GuestOutboundMessageStatus.SENT
            and _whatsapp_outbound_mirrors_guest_outbound(outbound, whatsapp_rows)
        ):
            continue
        rows.append((outbound.created_at.isoformat(), serialize_outbound(outbound)))

    for msg in whatsapp_rows:
        rows.append((msg.created_at.isoformat(), serialize_whatsapp(msg)))

    for msg in ChannexMessage.objects.filter(reservation=reservation):
        if (msg.body or "").strip() or msg.have_attachment or (
            getattr(msg, "media_file", None) and msg.media_file
        ):
            rows.append((msg.created_at.isoformat(), serialize_channex(msg)))

    for msg in GuestInboundMessage.objects.filter(reservation=reservation):
        if (msg.body_text or "").strip():
            ts = msg.received_at or msg.created_at
            rows.append((ts.isoformat(), serialize_inbound(msg)))

    rows.sort(key=lambda r: r[0])
    return merge_timeline_duplicates([item for _, item in rows])


def last_timeline_entry(reservation: Reservation) -> dict | None:
    timeline = timeline_for_reservation(reservation)
    return timeline[-1] if timeline else None
