"""ADR 0019 Phase D4 — tenant read-flag, live validate, and JSON parity."""

from __future__ import annotations

import os
from typing import Any

from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from apps.communications.canonical_backfill import (
    cutoff_from_status,
    run_canonical_backfill,
)
from apps.communications.guest_message_timeline import (
    raw_timeline_for_reservation,
    timeline_merge_groups_for_reservation,
)
from apps.communications.canonical_timeline import canonical_timeline_for_reservation
from apps.communications.models import (
    CanonicalConversationBackfill,
    GuestMessage,
    GuestMessageSource,
)
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant


def tenant_reads_canonical(tenant_or_id: Tenant | int) -> bool:
    tenant_id = tenant_or_id.pk if isinstance(tenant_or_id, Tenant) else int(tenant_or_id)
    return CanonicalConversationBackfill.objects.filter(
        tenant_id=tenant_id,
        read_canonical_at__isnull=False,
    ).exists()


def _completed_by() -> str:
    sha = (os.environ.get("GIT_SHA") or os.environ.get("SOURCE_VERSION") or "").strip()
    if sha:
        return f"set_canonical_guest_message_read {sha[:12]}"
    return "set_canonical_guest_message_read"


def _status_row(tenant: Tenant) -> CanonicalConversationBackfill | None:
    return CanonicalConversationBackfill.objects.filter(tenant=tenant).first()


def status_payload(tenant: Tenant) -> dict[str, Any]:
    row = _status_row(tenant)
    if row is None:
        return {
            "tenant": tenant.slug,
            "completed_at": None,
            "read_canonical_at": None,
            "cutoff": None,
        }
    cutoff = cutoff_from_status(row)
    return {
        "tenant": tenant.slug,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "completed_by": row.completed_by,
        "read_canonical_at": row.read_canonical_at.isoformat() if row.read_canonical_at else None,
        "read_canonical_by": row.read_canonical_by,
        "cutoff": cutoff.as_dict() if cutoff else None,
    }


def validate_canonical_read(tenant: Tenant) -> dict[str, Any]:
    row = _status_row(tenant)
    blocking: list[dict[str, Any]] = []
    info: list[dict[str, Any]] = []
    if row is None or row.completed_at is None:
        blocking.append({"code": "backfill_incomplete"})
    cutoff = cutoff_from_status(row) if row is not None else None
    if cutoff is None:
        blocking.append({"code": "cutoff_missing"})

    report = None
    if cutoff is not None:
        report = run_canonical_backfill(tenant, validate_only=True)
        blocking.extend(report.anomalies["blocking"])
        info.extend(report.anomalies["info"])

    visible_without_source = list(
        GuestMessage.objects.filter(tenant=tenant, is_visible=True)
        .annotate(n=Count("sources"))
        .filter(n=0)
        .values_list("pk", "conversation__reservation_id")
    )
    if visible_without_source:
        blocking.append(
            {
                "code": "visible_message_without_source",
                "count": len(visible_without_source),
                "message_ids": [pk for pk, _rid in visible_without_source[:20]],
            }
        )

    source_ids = {
        "channex": set(
            GuestMessageSource.objects.filter(
                tenant=tenant, channex_message_id__isnull=False
            ).values_list("channex_message_id", flat=True)
        ),
        "whatsapp": set(
            GuestMessageSource.objects.filter(
                tenant=tenant, whatsapp_message_id__isnull=False
            ).values_list("whatsapp_message_id", flat=True)
        ),
        "inbound": set(
            GuestMessageSource.objects.filter(
                tenant=tenant, inbound_message_id__isnull=False
            ).values_list("inbound_message_id", flat=True)
        ),
        "outbound": set(
            GuestMessageSource.objects.filter(
                tenant=tenant, outbound_message_id__isnull=False
            ).values_list("outbound_message_id", flat=True)
        ),
    }
    missing_raws = 0
    for reservation in Reservation.objects.filter(tenant=tenant).iterator():
        for group in timeline_merge_groups_for_reservation(reservation):
            for member in group.source_members:
                if member.raw_pk not in source_ids.get(member.raw_type, set()):
                    missing_raws += 1
    if missing_raws:
        blocking.append({"code": "raw_without_source", "count": missing_raws})

    return {
        "tenant": tenant.slug,
        "blocking": blocking,
        "info": info,
        "blocking_count": len(blocking),
        "backfill": report.as_dict() if report is not None else None,
    }


def _strip_canonical_id(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if key != "canonical_id"}


def _diff_items(raw_item: dict[str, Any], canonical_item: dict[str, Any]) -> dict[str, Any]:
    left = _strip_canonical_id(raw_item)
    right = _strip_canonical_id(canonical_item)
    keys = sorted(set(left) | set(right))
    fields: dict[str, Any] = {}
    for key in keys:
        if left.get(key) != right.get(key):
            fields[key] = {"raw": left.get(key), "canonical": right.get(key)}
    return fields


def compare_timeline_parity(tenant: Tenant) -> dict[str, Any]:
    mismatches: list[dict[str, Any]] = []
    compared = 0
    for reservation in Reservation.objects.filter(tenant=tenant).iterator():
        raw = raw_timeline_for_reservation(reservation)
        canonical = canonical_timeline_for_reservation(reservation)
        if not raw and not canonical:
            continue
        compared += 1
        if len(raw) != len(canonical):
            mismatches.append(
                {
                    "reservation_id": reservation.pk,
                    "reason": "length",
                    "raw_count": len(raw),
                    "canonical_count": len(canonical),
                }
            )
            continue
        field_diffs: list[dict[str, Any]] = []
        for index, (raw_item, canonical_item) in enumerate(zip(raw, canonical)):
            fields = _diff_items(raw_item, canonical_item)
            if fields:
                field_diffs.append({"index": index, "fields": fields})
        if field_diffs:
            mismatches.append(
                {
                    "reservation_id": reservation.pk,
                    "reason": "fields",
                    "diffs": field_diffs,
                }
            )
    return {
        "tenant": tenant.slug,
        "reservations_compared": compared,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:50],
        "blocking_count": len(mismatches),
    }


def enable_canonical_read(tenant: Tenant) -> dict[str, Any]:
    with transaction.atomic():
        row = (
            CanonicalConversationBackfill.objects.select_for_update()
            .filter(tenant=tenant)
            .first()
        )
        if row is None:
            raise ValueError("mark-complete required before enable")
        validate = validate_canonical_read(tenant)
        if validate["blocking_count"]:
            raise ValueError(
                f"live validation failed blocking={validate['blocking_count']}"
            )
        parity = compare_timeline_parity(tenant)
        if parity["blocking_count"]:
            raise ValueError(f"parity failed mismatches={parity['blocking_count']}")
        row.read_canonical_at = timezone.now()
        row.read_canonical_by = _completed_by()
        row.read_snapshot = {"validate": validate, "parity": parity}
        row.save(
            update_fields=[
                "read_canonical_at",
                "read_canonical_by",
                "read_snapshot",
                "updated_at",
            ]
        )
        return {
            "tenant": tenant.slug,
            "read_canonical_at": row.read_canonical_at.isoformat(),
            "read_canonical_by": row.read_canonical_by,
            "validate": validate,
            "parity": parity,
        }


def disable_canonical_read(tenant: Tenant) -> dict[str, Any]:
    with transaction.atomic():
        row = (
            CanonicalConversationBackfill.objects.select_for_update()
            .filter(tenant=tenant)
            .first()
        )
        if row is None:
            return {"tenant": tenant.slug, "read_canonical_at": None}
        row.read_canonical_at = None
        row.read_canonical_by = ""
        row.read_snapshot = {}
        row.save(
            update_fields=[
                "read_canonical_at",
                "read_canonical_by",
                "read_snapshot",
                "updated_at",
            ]
        )
        return {
            "tenant": tenant.slug,
            "read_canonical_at": None,
            "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        }
