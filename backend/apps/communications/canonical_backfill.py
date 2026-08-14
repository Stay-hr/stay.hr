"""ADR 0019 Phase D3 — backfill raw timeline groups onto canonical store.

GET still reads ``timeline_for_reservation``. This module does not send, poll,
touch, or flip ``is_visible``.
"""

from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from apps.communications.canonical_store import (
    _canonical_provider_message_id,
    _delivery_status,
    _enrich_message,
    _insert_source,
    _lock_conversation,
    _provider,
    _raw_fk_field,
    _raw_media_file,
    _reconcile_channel,
    _timeline_item_for_raw,
)
from apps.communications.guest_message_timeline import (
    TimelineMergeGroup,
    raw_type_for,
    timeline_merge_groups_for_reservation,
)
from apps.communications.models import (
    CanonicalConversationBackfill,
    Conversation,
    GuestInboundMessage,
    GuestMessage,
    GuestMessageChannel,
    GuestMessageDirection,
    GuestMessageSource,
    GuestOutboundMessage,
)
from apps.integrations.models import ChannexMessage, WhatsAppMessage
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant

BLOCKING_CODES = (
    "group_member_missing_source",
    "source_without_raw",
    "source_without_message",
    "source_conversation_mismatch",
    "tenant_mismatch",
    "split_canonical",
    "cross_group_canonical",
    "group_message_invisible",
    "provider_identity_conflict",
    "duplicate_group_source_member",
)

INFO_CODES = (
    "unrouted_whatsapp_skipped",
    "raw_outside_timeline",
    "visible_outside_timeline",
    "raw_after_cutoff",
)


@dataclass(frozen=True)
class BackfillCutoff:
    at: datetime
    channex_id: int
    whatsapp_id: int
    inbound_id: int
    outbound_id: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "cutoff_at": self.at.isoformat(),
            "channex_id": self.channex_id,
            "whatsapp_id": self.whatsapp_id,
            "inbound_id": self.inbound_id,
            "outbound_id": self.outbound_id,
        }


@dataclass
class BackfillReport:
    reservations_scanned: int = 0
    groups: int = 0
    guest_messages_visible_in_groups: int = 0
    sources_in_groups: int = 0
    would_create_messages: int = 0
    would_create_sources: int = 0
    created_messages: int = 0
    created_sources: int = 0
    healed_sources: int = 0
    skipped_unrouted_whatsapp: int = 0
    raw_with_reservation_outside_timeline: int = 0
    raw_after_cutoff: int = 0
    cutoff: dict[str, Any] = field(default_factory=dict)
    anomalies: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {"blocking": [], "info": []}
    )

    def add_blocking(self, code: str, **detail: Any) -> None:
        self.anomalies["blocking"].append({"code": code, **detail})

    def add_info(self, code: str, **detail: Any) -> None:
        self.anomalies["info"].append({"code": code, **detail})

    @property
    def blocking_count(self) -> int:
        return len(self.anomalies["blocking"])

    def as_dict(self) -> dict[str, Any]:
        return {
            "reservations_scanned": self.reservations_scanned,
            "cutoff": self.cutoff,
            "groups": self.groups,
            "guest_messages_visible_in_groups": self.guest_messages_visible_in_groups,
            "sources_in_groups": self.sources_in_groups,
            "would_create_messages": self.would_create_messages,
            "would_create_sources": self.would_create_sources,
            "created_messages": self.created_messages,
            "created_sources": self.created_sources,
            "healed_sources": self.healed_sources,
            "skipped_unrouted_whatsapp": self.skipped_unrouted_whatsapp,
            "raw_with_reservation_outside_timeline": self.raw_with_reservation_outside_timeline,
            "raw_after_cutoff": self.raw_after_cutoff,
            "anomalies": {
                "blocking": list(self.anomalies["blocking"]),
                "info": list(self.anomalies["info"]),
            },
        }


def compute_cutoff(tenant: Tenant) -> BackfillCutoff:
    def _max_pk(qs) -> int:
        return int(qs.aggregate(v=Max("pk"))["v"] or 0)

    whatsapp = max(
        _max_pk(WhatsAppMessage.objects.filter(tenant=tenant)),
        _max_pk(WhatsAppMessage.objects.filter(reservation__tenant=tenant)),
    )
    return BackfillCutoff(
        at=timezone.now(),
        channex_id=_max_pk(ChannexMessage.objects.filter(tenant=tenant)),
        whatsapp_id=whatsapp,
        inbound_id=_max_pk(GuestInboundMessage.objects.filter(tenant=tenant)),
        outbound_id=_max_pk(GuestOutboundMessage.objects.filter(tenant=tenant)),
    )


def cutoff_from_status(row: CanonicalConversationBackfill) -> BackfillCutoff | None:
    if (
        row.cutoff_channex_id is None
        or row.cutoff_whatsapp_id is None
        or row.cutoff_inbound_id is None
        or row.cutoff_outbound_id is None
    ):
        return None
    return BackfillCutoff(
        at=row.cutoff_at or timezone.now(),
        channex_id=int(row.cutoff_channex_id),
        whatsapp_id=int(row.cutoff_whatsapp_id),
        inbound_id=int(row.cutoff_inbound_id),
        outbound_id=int(row.cutoff_outbound_id),
    )


def persist_cutoff(tenant: Tenant, cutoff: BackfillCutoff) -> CanonicalConversationBackfill:
    row, _created = CanonicalConversationBackfill.objects.get_or_create(
        tenant=tenant,
        defaults={
            "cutoff_at": cutoff.at,
            "cutoff_channex_id": cutoff.channex_id,
            "cutoff_whatsapp_id": cutoff.whatsapp_id,
            "cutoff_inbound_id": cutoff.inbound_id,
            "cutoff_outbound_id": cutoff.outbound_id,
        },
    )
    if cutoff_from_status(row) is None:
        row.cutoff_at = cutoff.at
        row.cutoff_channex_id = cutoff.channex_id
        row.cutoff_whatsapp_id = cutoff.whatsapp_id
        row.cutoff_inbound_id = cutoff.inbound_id
        row.cutoff_outbound_id = cutoff.outbound_id
        row.save(
            update_fields=[
                "cutoff_at",
                "cutoff_channex_id",
                "cutoff_whatsapp_id",
                "cutoff_inbound_id",
                "cutoff_outbound_id",
                "updated_at",
            ]
        )
    return row


def resolve_cutoff(
    tenant: Tenant, *, persist: bool, stored: CanonicalConversationBackfill | None = None
) -> tuple[BackfillCutoff, CanonicalConversationBackfill | None]:
    row = stored
    if row is None:
        row = CanonicalConversationBackfill.objects.filter(tenant=tenant).first()
    if row is not None:
        existing = cutoff_from_status(row)
        if existing is not None:
            return existing, row
    computed = compute_cutoff(tenant)
    if persist:
        return cutoff_from_status(persist_cutoff(tenant, computed)) or computed, row
    return computed, row


def run_canonical_backfill(
    tenant: Tenant,
    *,
    dry_run: bool = False,
    validate_only: bool = False,
    mark_complete: bool = False,
    reservation_id: int | None = None,
    resume_after_reservation_id: int | None = None,
) -> BackfillReport:
    persist = not dry_run and not validate_only and not mark_complete
    if mark_complete:
        row = CanonicalConversationBackfill.objects.filter(tenant=tenant).first()
        cutoff = cutoff_from_status(row) if row is not None else None
        if cutoff is None:
            raise ValueError("mark-complete requires a stored full-tenant cutoff")
        report = _scan_tenant(
            tenant,
            cutoff=cutoff,
            dry_run=False,
            validate_only=True,
            reservation_id=None,
            resume_after_reservation_id=None,
        )
        if report.blocking_count == 0:
            row.completed_at = timezone.now()
            row.snapshot = report.as_dict()
            row.completed_by = _completed_by()
            row.save(update_fields=["completed_at", "snapshot", "completed_by", "updated_at"])
        return report

    cutoff, _row = resolve_cutoff(tenant, persist=persist)
    return _scan_tenant(
        tenant,
        cutoff=cutoff,
        dry_run=dry_run,
        validate_only=validate_only,
        reservation_id=reservation_id,
        resume_after_reservation_id=resume_after_reservation_id,
    )


def _completed_by() -> str:
    sha = (os.environ.get("GIT_SHA") or os.environ.get("SOURCE_VERSION") or "").strip()
    if sha:
        return f"backfill_canonical_guest_messages {sha[:12]}"
    return "backfill_canonical_guest_messages"


def _scan_tenant(
    tenant: Tenant,
    *,
    cutoff: BackfillCutoff,
    dry_run: bool,
    validate_only: bool,
    reservation_id: int | None,
    resume_after_reservation_id: int | None,
) -> BackfillReport:
    report = BackfillReport(cutoff=cutoff.as_dict())
    qs = Reservation.objects.filter(tenant=tenant).order_by("pk")
    if reservation_id is not None:
        qs = qs.filter(pk=reservation_id)
    if resume_after_reservation_id is not None:
        qs = qs.filter(pk__gt=resume_after_reservation_id)

    apply = not dry_run and not validate_only
    visible_message_ids: set[int] = set()
    group_source_keys: set[tuple[str, int]] = set()

    for reservation in qs.iterator():
        report.reservations_scanned += 1
        if apply:
            with transaction.atomic():
                _process_reservation(
                    reservation,
                    cutoff=cutoff,
                    report=report,
                    dry_run=False,
                    validate_only=False,
                    visible_message_ids=visible_message_ids,
                    group_source_keys=group_source_keys,
                )
        else:
            _process_reservation(
                reservation,
                cutoff=cutoff,
                report=report,
                dry_run=dry_run,
                validate_only=validate_only,
                visible_message_ids=visible_message_ids,
                group_source_keys=group_source_keys,
            )

    _collect_info_counts(
        tenant,
        cutoff=cutoff,
        report=report,
        group_source_keys=group_source_keys,
        visible_message_ids=visible_message_ids,
        reservation_id=reservation_id,
    )
    return report


def _process_reservation(
    reservation: Reservation,
    *,
    cutoff: BackfillCutoff,
    report: BackfillReport,
    dry_run: bool,
    validate_only: bool,
    visible_message_ids: set[int],
    group_source_keys: set[tuple[str, int]],
) -> None:
    groups = timeline_merge_groups_for_reservation(reservation, cutoff=cutoff)
    report.groups += len(groups)
    seen_raw: dict[tuple[str, int], int] = {}
    resolved: list[tuple[TimelineMergeGroup, _GroupResolution]] = []

    for index, group in enumerate(groups):
        for member in group.source_members:
            key = (member.raw_type, member.raw_pk)
            group_source_keys.add(key)
            if key in seen_raw:
                report.add_blocking(
                    "duplicate_group_source_member",
                    reservation_id=reservation.pk,
                    raw_type=member.raw_type,
                    raw_pk=member.raw_pk,
                )
            else:
                seen_raw[key] = index
        resolved.append((group, _resolve_group(reservation, group, report)))

    message_groups: dict[int, list[int]] = defaultdict(list)
    for index, (_group, resolution) in enumerate(resolved):
        for message_id in resolution.message_ids:
            message_groups[message_id].append(index)
    cross = {mid for mid, idxs in message_groups.items() if len(set(idxs)) > 1}
    blocked_indexes: set[int] = set()
    for message_id in cross:
        report.add_blocking(
            "cross_group_canonical",
            reservation_id=reservation.pk,
            message_id=message_id,
        )
        blocked_indexes.update(message_groups[message_id])

    for index, (group, resolution) in enumerate(resolved):
        if resolution.blocking or index in blocked_indexes:
            continue
        if resolution.visible_message_id:
            visible_message_ids.add(resolution.visible_message_id)
        if validate_only:
            _validate_group(reservation, group, resolution, report)
            if resolution.visible_message_id:
                report.guest_messages_visible_in_groups += 1
                report.sources_in_groups += len(group.source_members)
            continue
        if dry_run:
            _simulate_group(group, resolution, report)
            continue
        created_message, created_sources, healed = _apply_group(reservation, group, resolution)
        report.created_messages += created_message
        report.created_sources += created_sources
        report.healed_sources += healed
        message = resolution.visible_message
        if message is None and created_message:
            # applied path sets resolution after create via return; count below
            pass
        if created_message or resolution.visible_message_id:
            report.guest_messages_visible_in_groups += 1
            report.sources_in_groups += len(group.source_members)
            if resolution.visible_message_id:
                visible_message_ids.add(resolution.visible_message_id)


@dataclass
class _GroupResolution:
    blocking: bool = False
    visible_message: GuestMessage | None = None
    message_ids: set[int] = field(default_factory=set)
    missing_raws: list[Any] = field(default_factory=list)
    existing_by_raw: dict[int, GuestMessageSource] = field(default_factory=dict)

    @property
    def visible_message_id(self) -> int | None:
        if self.visible_message is not None and self.visible_message.is_visible:
            return self.visible_message.pk
        return None


def _resolve_group(
    reservation: Reservation,
    group: TimelineMergeGroup,
    report: BackfillReport,
) -> _GroupResolution:
    resolution = _GroupResolution()
    messages: dict[int, GuestMessage] = {}
    tenant_id = reservation.tenant_id

    for member in group.source_members:
        raw = member.raw
        by_fk, by_id, conflict = _lookup_source_pair(raw, tenant_id=tenant_id)
        if conflict:
            report.add_blocking(
                "provider_identity_conflict",
                reservation_id=reservation.pk,
                raw_type=member.raw_type,
                raw_pk=member.raw_pk,
            )
            resolution.blocking = True
            continue
        source = by_fk or by_id
        if source is None:
            resolution.missing_raws.append(raw)
            continue
        if _raw_from_source_id(source) is None:
            report.add_blocking(
                "source_without_raw",
                reservation_id=reservation.pk,
                source_id=source.pk,
            )
            resolution.blocking = True
        if not source.message_id:
            report.add_blocking(
                "source_without_message",
                reservation_id=reservation.pk,
                source_id=source.pk,
            )
            resolution.blocking = True
            continue
        message = source.message
        if message.tenant_id != tenant_id or source.tenant_id != tenant_id:
            report.add_blocking(
                "tenant_mismatch",
                reservation_id=reservation.pk,
                source_id=source.pk,
            )
            resolution.blocking = True
        if message.conversation.reservation_id != reservation.pk:
            report.add_blocking(
                "source_conversation_mismatch",
                reservation_id=reservation.pk,
                source_id=source.pk,
                source_conversation_id=message.conversation_id,
            )
            resolution.blocking = True
        messages[message.pk] = message
        resolution.message_ids.add(message.pk)
        resolution.existing_by_raw[id(raw)] = source

    visible = [msg for msg in messages.values() if msg.is_visible]
    hidden = [msg for msg in messages.values() if not msg.is_visible]
    if hidden and not visible:
        report.add_blocking(
            "group_message_invisible",
            reservation_id=reservation.pk,
            message_id=hidden[0].pk,
        )
        resolution.blocking = True
    elif hidden and visible:
        report.add_blocking(
            "split_canonical",
            reservation_id=reservation.pk,
            message_ids=sorted(messages),
        )
        resolution.blocking = True
    if len(visible) > 1:
        report.add_blocking(
            "split_canonical",
            reservation_id=reservation.pk,
            message_ids=sorted(m.pk for m in visible),
        )
        resolution.blocking = True
    if len(visible) == 1:
        resolution.visible_message = visible[0]
    return resolution


def _validate_group(
    reservation: Reservation,
    group: TimelineMergeGroup,
    resolution: _GroupResolution,
    report: BackfillReport,
) -> None:
    if resolution.missing_raws:
        report.add_blocking(
            "group_member_missing_source",
            reservation_id=reservation.pk,
            missing=len(resolution.missing_raws),
        )


def _simulate_group(
    group: TimelineMergeGroup,
    resolution: _GroupResolution,
    report: BackfillReport,
) -> None:
    if resolution.visible_message is None:
        report.would_create_messages += 1
        report.would_create_sources += len(group.source_members)
        return
    report.would_create_sources += len(resolution.missing_raws)


def _apply_group(
    reservation: Reservation,
    group: TimelineMergeGroup,
    resolution: _GroupResolution,
) -> tuple[int, int, int]:
    tenant_id = reservation.tenant_id
    conversation = _lock_conversation(tenant_id=tenant_id, reservation_id=reservation.pk)
    message = resolution.visible_message
    created_messages = 0
    if message is None:
        message = _insert_group_message(
            conversation=conversation,
            tenant_id=tenant_id,
            group=group,
        )
        created_messages = 1
        resolution.visible_message = message
    created_sources = 0
    healed = 0
    for member in group.source_members:
        raw = member.raw
        if id(raw) in resolution.existing_by_raw:
            continue
        incoming = _timeline_item_for_raw(raw)
        try:
            with transaction.atomic():
                _insert_source(
                    message=message,
                    raw=raw,
                    tenant_id=tenant_id,
                    provider=_provider(raw),
                    provider_message_id=_canonical_provider_message_id(raw),
                )
        except IntegrityError:
            healed += 1
            continue
        _enrich_message(message, raw, incoming)
        if created_messages:
            created_sources += 1
        else:
            healed += 1
    _reconcile_channel(message)
    return created_messages, created_sources, healed


def _insert_group_message(
    *,
    conversation: Conversation,
    tenant_id: int,
    group: TimelineMergeGroup,
) -> GuestMessage:
    display = group.display
    occurred_at = group.occurred_at
    if occurred_at is None:
        occurred_at = timezone.now()
    has_channex = any(raw_type_for(member.raw) == "channex" for member in group.source_members)
    channel = (
        GuestMessageChannel.BOOKING
        if has_channex
        else (display.get("channel") or GuestMessageChannel.EMAIL)
    )
    delivery = ""
    media = None
    for member in group.source_members:
        if not delivery:
            delivery = _delivery_status(member.raw)
        if media is None:
            media = _raw_media_file(member.raw)
    message = GuestMessage(
        tenant_id=tenant_id,
        conversation=conversation,
        direction=display.get("direction") or GuestMessageDirection.INBOUND,
        channel=channel,
        body=display.get("body_text") or "",
        occurred_at=occurred_at,
        delivery_status=delivery,
        is_visible=True,
    )
    if media:
        message.media_file = media
    message.save()
    return message


def _lookup_source_pair(raw, *, tenant_id: int):
    qs = GuestMessageSource.objects.select_related("message", "message__conversation")
    by_fk = qs.filter(**{_raw_fk_field(raw): raw}).first()
    provider_message_id = _canonical_provider_message_id(raw)
    by_id = None
    if provider_message_id:
        by_id = qs.filter(
            tenant_id=tenant_id,
            provider=_provider(raw),
            provider_message_id=provider_message_id,
        ).first()
    if by_fk is not None and by_id is not None and by_fk.pk != by_id.pk:
        return by_fk, by_id, True
    return by_fk, by_id, False


def _raw_from_source_id(source: GuestMessageSource) -> int | None:
    for name in GuestMessageSource.RAW_FK_FIELDS:
        value = getattr(source, f"{name}_id", None)
        if value is not None:
            return value
    return None


def _collect_info_counts(
    tenant: Tenant,
    *,
    cutoff: BackfillCutoff,
    report: BackfillReport,
    group_source_keys: set[tuple[str, int]],
    visible_message_ids: set[int],
    reservation_id: int | None,
) -> None:
    wa_unrouted = WhatsAppMessage.objects.filter(
        reservation_id__isnull=True,
        pk__lte=cutoff.whatsapp_id,
    ).filter(tenant=tenant)
    report.skipped_unrouted_whatsapp = wa_unrouted.count()
    if report.skipped_unrouted_whatsapp:
        report.add_info("unrouted_whatsapp_skipped", count=report.skipped_unrouted_whatsapp)

    res_filter = {"reservation__tenant": tenant}
    if reservation_id is not None:
        res_filter = {"reservation_id": reservation_id}

    outside = 0
    after = 0
    pairs = (
        ("channex", ChannexMessage, cutoff.channex_id),
        ("whatsapp", WhatsAppMessage, cutoff.whatsapp_id),
        ("inbound", GuestInboundMessage, cutoff.inbound_id),
        ("outbound", GuestOutboundMessage, cutoff.outbound_id),
    )
    for raw_type, model, cutoff_id in pairs:
        qs = model.objects.filter(**res_filter)
        after += qs.filter(pk__gt=cutoff_id).count()
        for pk in qs.filter(pk__lte=cutoff_id).values_list("pk", flat=True):
            if (raw_type, pk) not in group_source_keys:
                outside += 1
    report.raw_with_reservation_outside_timeline = outside
    report.raw_after_cutoff = after
    if outside:
        report.add_info("raw_outside_timeline", count=outside)
    if after:
        report.add_info("raw_after_cutoff", count=after)

    extra_visible = (
        GuestMessage.objects.filter(
            tenant=tenant,
            is_visible=True,
            conversation__reservation__tenant=tenant,
        )
        .exclude(pk__in=visible_message_ids)
    )
    if reservation_id is not None:
        extra_visible = extra_visible.filter(conversation__reservation_id=reservation_id)
    extra_count = extra_visible.count()
    if extra_count:
        report.add_info("visible_outside_timeline", count=extra_count)
