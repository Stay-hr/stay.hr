"""ADR 0019 Phase D4 — serialize visible GuestMessage onto the existing timeline JSON."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from django.db.models import Prefetch

from apps.communications.canonical_store import _raw_from_source
from apps.communications.guest_message_timeline import (
    _channex_visible_in_timeline,
    _merge_item_group,
    _whatsapp_outbound_mirrors_guest_outbound,
    serialize_channex,
    serialize_inbound,
    serialize_outbound,
    serialize_whatsapp,
)
from apps.communications.models import (
    GuestInboundMessage,
    GuestMessage,
    GuestMessageChannel,
    GuestMessageSource,
    GuestOutboundMessage,
    GuestOutboundMessageStatus,
)
from apps.integrations.models import ChannexMessage, WhatsAppMessage
from apps.reservations.models import DocumentIntakeJob, Reservation

_SOURCE_PREFETCH = Prefetch(
    "sources",
    queryset=GuestMessageSource.objects.select_related(
        "channex_message",
        "whatsapp_message",
        "inbound_message",
        "outbound_message",
        "outbound_message__api_application",
    ),
)


def visible_guest_messages_qs(*, reservation_ids: Iterable[int]):
    return (
        GuestMessage.objects.filter(
            conversation__reservation_id__in=list(reservation_ids),
            is_visible=True,
        )
        .select_related("conversation")
        .prefetch_related(_SOURCE_PREFETCH)
        .order_by("occurred_at", "pk")
    )


def _display_items_for_message(
    message: GuestMessage,
    *,
    whatsapp_rows: list[WhatsAppMessage],
    intake_jobs: dict[int, int],
) -> list[dict]:
    display: list[dict] = []
    for source in message.sources.all():
        raw = _raw_from_source(source)
        if raw is None:
            continue
        if isinstance(raw, GuestOutboundMessage):
            if (
                raw.channel == GuestMessageChannel.WHATSAPP
                and raw.status == GuestOutboundMessageStatus.SENT
                and _whatsapp_outbound_mirrors_guest_outbound(raw, whatsapp_rows)
            ):
                continue
            display.append(serialize_outbound(raw))
            continue
        if isinstance(raw, WhatsAppMessage):
            display.append(
                serialize_whatsapp(
                    raw,
                    document_intake_job_id=intake_jobs.get(raw.pk),
                    resolve_intake_job=False,
                )
            )
            continue
        if isinstance(raw, ChannexMessage):
            if _channex_visible_in_timeline(raw):
                display.append(serialize_channex(raw))
            continue
        if isinstance(raw, GuestInboundMessage):
            if (raw.body_text or "").strip():
                display.append(serialize_inbound(raw))
    return display


def serialize_guest_message(
    message: GuestMessage,
    *,
    whatsapp_rows: list[WhatsAppMessage],
    intake_jobs: dict[int, int] | None = None,
) -> dict[str, Any] | None:
    items = _display_items_for_message(
        message,
        whatsapp_rows=whatsapp_rows,
        intake_jobs=intake_jobs or {},
    )
    if not items:
        return None
    merged = _merge_item_group(items)
    merged["canonical_id"] = message.pk
    return merged


def _intake_jobs_for_whatsapp(whatsapp_ids: Iterable[int]) -> dict[int, int]:
    ids = [pk for pk in whatsapp_ids if pk]
    if not ids:
        return {}
    return {
        job.whatsapp_message_id: job.pk
        for job in DocumentIntakeJob.objects.filter(whatsapp_message_id__in=ids)
        if job.whatsapp_message_id
    }


def _whatsapp_rows_from_messages(messages: list[GuestMessage]) -> list[WhatsAppMessage]:
    rows: list[WhatsAppMessage] = []
    seen: set[int] = set()
    for message in messages:
        for source in message.sources.all():
            raw = source.whatsapp_message
            if raw is None or raw.pk in seen:
                continue
            seen.add(raw.pk)
            rows.append(raw)
    return rows


def canonical_timeline_for_messages(
    messages: list[GuestMessage],
    *,
    whatsapp_rows: list[WhatsAppMessage] | None = None,
    intake_jobs: dict[int, int] | None = None,
) -> list[dict]:
    wa_rows = whatsapp_rows if whatsapp_rows is not None else _whatsapp_rows_from_messages(messages)
    jobs = intake_jobs if intake_jobs is not None else _intake_jobs_for_whatsapp(
        raw.pk for raw in wa_rows
    )
    timeline: list[dict] = []
    for message in messages:
        item = serialize_guest_message(message, whatsapp_rows=wa_rows, intake_jobs=jobs)
        if item is not None:
            timeline.append(item)
    return timeline


def canonical_timeline_for_reservation(reservation: Reservation) -> list[dict]:
    messages = list(visible_guest_messages_qs(reservation_ids=[reservation.pk]))
    return canonical_timeline_for_messages(messages)


def canonical_timelines_by_reservation(
    reservations: list[Reservation],
) -> dict[int, list[dict]]:
    if not reservations:
        return {}
    reservation_ids = [reservation.pk for reservation in reservations]
    messages = list(visible_guest_messages_qs(reservation_ids=reservation_ids))
    by_reservation: dict[int, list[GuestMessage]] = defaultdict(list)
    for message in messages:
        by_reservation[message.conversation.reservation_id].append(message)
    wa_rows = _whatsapp_rows_from_messages(messages)
    jobs = _intake_jobs_for_whatsapp(raw.pk for raw in wa_rows)
    wa_by_reservation: dict[int, list[WhatsAppMessage]] = defaultdict(list)
    for message in messages:
        rid = message.conversation.reservation_id
        for source in message.sources.all():
            if source.whatsapp_message_id and source.whatsapp_message:
                wa_by_reservation[rid].append(source.whatsapp_message)
    result: dict[int, list[dict]] = {}
    for reservation in reservations:
        result[reservation.pk] = canonical_timeline_for_messages(
            by_reservation.get(reservation.pk, []),
            whatsapp_rows=wa_by_reservation.get(reservation.pk, []),
            intake_jobs=jobs,
        )
    return result
