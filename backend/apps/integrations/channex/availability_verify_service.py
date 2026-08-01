"""Compare stay.hr occupancy vs live Channex availability.

Invariant: verify and repair share one diff engine.

    verify() → mismatch_list
    repair(mismatch_list) → guards → threshold → write

Repair must consume the mismatch list from verify (or the identical
``find_availability_mismatches`` helper). It must not recompute expected
availability with a different formula.

Beat runs verify-only. Repair requires an explicit CLI ``--repair`` on an
authorized writer (see ADR 0014 / incident 2026-08-01).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from django.conf import settings
from django.utils import timezone

from apps.integrations.channex.ari_service import (
    apply_availability_updates,
    get_active_channex_integration,
    push_channex_ari,
    sync_property,
)
from apps.integrations.channex.client import ChannexClient
from apps.integrations.channex.config import ChannexRuntimeConfig
from apps.integrations.channex.exceptions import (
    ChannexApiError,
    ChannexBookingIngestError,
    ChannexWriteDisabled,
)
from apps.integrations.channex.outbound_guard import assert_can_write
from apps.integrations.channex.reservation_availability_service import (
    compute_unit_availability,
)
from apps.properties.models import Unit
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

DEFAULT_VERIFY_DAYS = 90

# Process-local metrics (reset on restart; exposed via /system/status).
_verify_mismatches_total: int = 0
_repair_skipped_threshold_total: int = 0
_repair_success_total: int = 0


@dataclass(frozen=True)
class AvailabilityMismatch:
    unit_code: str
    room_type_id: str
    day: date
    expected: int
    actual: int


def get_channex_verify_mismatches_total() -> int:
    return _verify_mismatches_total


def get_channex_repair_skipped_threshold_total() -> int:
    return _repair_skipped_threshold_total


def get_channex_repair_success_total() -> int:
    return _repair_success_total


def reset_channex_verify_repair_counters() -> None:
    global _verify_mismatches_total, _repair_skipped_threshold_total, _repair_success_total
    _verify_mismatches_total = 0
    _repair_skipped_threshold_total = 0
    _repair_success_total = 0


def _room_type_id_for_unit(integration, config: ChannexRuntimeConfig, unit: Unit) -> str | None:
    room_type_id = config.room_type_id_for_unit_code(unit.code)
    if room_type_id:
        return room_type_id
    for room in integration.get_config_dict().get("booking_test_rooms") or []:
        if str(room.get("unit_code")) == unit.code:
            room_type_id = str(room.get("channex_room_type_id") or "")
            if room_type_id:
                return room_type_id
    for link in config.booking_test_rooms:
        if link.unit_id == unit.id and link.channex_room_type_id:
            return link.channex_room_type_id
    return None


def _mapped_units(
    integration,
    config: ChannexRuntimeConfig,
) -> list[tuple[Unit, str]]:
    """Return (unit, channex_room_type_id) for units with a Channex mapping."""
    tenant = integration.tenant
    prop = sync_property(tenant, config)
    units = list(Unit.objects.filter(tenant=tenant, property=prop, is_active=True))
    mapped: list[tuple[Unit, str]] = []
    for unit in units:
        room_type_id = _room_type_id_for_unit(integration, config, unit)
        if room_type_id:
            mapped.append((unit, room_type_id))
    return mapped


def _parse_live_availability(
    live: dict[str, Any],
    room_type_id: str,
    day: date,
) -> int | None:
    """Return Channex availability for room_type/day, or None if missing."""
    by_room = live.get(room_type_id)
    if not isinstance(by_room, dict):
        return None
    raw = by_room.get(day.isoformat())
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def find_availability_mismatches(
    *,
    tenant_slug: str,
    days: int = DEFAULT_VERIFY_DAYS,
    from_date: date | None = None,
    client: ChannexClient | None = None,
) -> tuple[list[AvailabilityMismatch], dict[str, Any]]:
    """Shared diff engine: stay.hr expected vs live Channex GET /availability."""
    if not (tenant_slug or "").strip():
        raise ValueError("tenant_slug is required")

    integration = get_active_channex_integration(tenant_slug)
    config = ChannexRuntimeConfig.from_integration_dict(integration.get_config_dict())
    if not config.property_id:
        raise ChannexBookingIngestError("Channex property_id missing")

    start = from_date or timezone.localdate()
    end = start + timedelta(days=max(1, days) - 1)
    mapped = _mapped_units(integration, config)
    if not mapped:
        return [], {
            "tenant_slug": tenant_slug,
            "property_id": config.property_id,
            "from_date": start.isoformat(),
            "to_date": end.isoformat(),
            "units_checked": 0,
            "skipped": True,
            "reason": "no_mapped_units",
        }

    owns_client = client is None
    if owns_client:
        client = ChannexClient(config)
    try:
        live = client.get_availability(
            property_id=config.property_id,
            date_from=start.isoformat(),
            date_to=end.isoformat(),
        )
    finally:
        if owns_client:
            client.close()

    mismatches: list[AvailabilityMismatch] = []
    current = start
    while current <= end:
        for unit, room_type_id in mapped:
            expected = compute_unit_availability(integration.tenant, unit, current)
            actual = _parse_live_availability(live, room_type_id, current)
            if actual is None:
                # Missing day in Channex response — treat as mismatch vs expected.
                actual = -1
            if actual != expected:
                mismatches.append(
                    AvailabilityMismatch(
                        unit_code=unit.code,
                        room_type_id=room_type_id,
                        day=current,
                        expected=expected,
                        actual=actual,
                    )
                )
        current += timedelta(days=1)

    meta = {
        "tenant_slug": tenant_slug,
        "tenant_id": integration.tenant_id,
        "property_id": config.property_id,
        "from_date": start.isoformat(),
        "to_date": end.isoformat(),
        "units_checked": len(mapped),
        "mismatch_count": len(mismatches),
    }
    return mismatches, meta


def evaluate_repair_blast_radius(
    mismatches: list[AvailabilityMismatch],
    *,
    units_checked: int,
) -> dict[str, Any]:
    """Return blast-radius stats and whether the repair threshold trips."""
    by_unit: dict[str, set[date]] = defaultdict(set)
    for row in mismatches:
        by_unit[row.unit_code].add(row.day)

    units = len(by_unit)
    max_days = max((len(days) for days in by_unit.values()), default=0)
    checked = max(1, int(units_checked or 0))
    affected_percent = (units / checked) * 100.0

    max_units = int(getattr(settings, "CHANNEX_ARI_REPAIR_MAX_UNITS", 5))
    max_percent = float(getattr(settings, "CHANNEX_ARI_REPAIR_MAX_UNIT_PERCENT", 20.0))
    max_days_per_unit = int(getattr(settings, "CHANNEX_ARI_REPAIR_MAX_DAYS_PER_UNIT", 3))

    reasons: list[str] = []
    if units >= max_units:
        reasons.append("max_units")
    # Percent only when the mapped set is large enough (avoids 1/1 = 100% false trips).
    if checked >= max_units and affected_percent > max_percent:
        reasons.append("percent")
    if max_days >= max_days_per_unit:
        reasons.append("max_days")

    return {
        "units": units,
        "units_checked": checked,
        "affected_percent": round(affected_percent, 2),
        "max_days": max_days,
        "threshold": "trip" if reasons else "ok",
        "reasons": reasons,
        "limits": {
            "max_units": max_units,
            "max_percent": max_percent,
            "max_days_per_unit": max_days_per_unit,
        },
    }


def _repair_mismatches(integration, mismatches: list[AvailabilityMismatch]) -> int:
    """Push expected availability from the provided mismatch list (no re-diff)."""
    if not mismatches:
        return 0
    updates = [
        {
            "unit_code": row.unit_code,
            "date": row.day.isoformat(),
            "availability": row.expected,
        }
        for row in mismatches
        if row.expected >= 0
    ]
    if not updates:
        return 0
    apply_availability_updates(integration, updates, queue_push=True)
    push_channex_ari(integration)
    return len(updates)


def _notify_mismatches(tenant: Tenant, mismatches: list[AvailabilityMismatch]) -> None:
    from apps.core.notifications import send_tenant_reception_push
    from apps.core.push_payload import reception_push_data

    lines = [
        f"{m.unit_code} {m.day.isoformat()}: expected={m.expected} channex={m.actual}"
        for m in mismatches[:5]
    ]
    if len(mismatches) > 5:
        lines.append(f"+{len(mismatches) - 5} još")
    body = "; ".join(lines)
    data = reception_push_data(
        event_type="channel.ari_mismatch",
        reservation_id=0,
        summary=body,
        tenant_id=str(tenant.pk),
        mismatch_count=str(len(mismatches)),
    )
    send_tenant_reception_push(
        tenant_id=tenant.pk,
        title=f"Channex ARI mismatch ({len(mismatches)})",
        body=body,
        data=data,
    )


def _mismatch_payload(mismatches: list[AvailabilityMismatch]) -> list[dict[str, Any]]:
    return [
        {
            "unit_code": m.unit_code,
            "day": m.day.isoformat(),
            "expected": m.expected,
            "actual": m.actual,
        }
        for m in mismatches[:50]
    ]


def verify_availability(
    *,
    tenant_slug: str,
    days: int = DEFAULT_VERIFY_DAYS,
    from_date: date | None = None,
    notify: bool = True,
    client: ChannexClient | None = None,
) -> dict[str, Any]:
    """Verify-only: shared diff engine + optional notify. Never writes to Channex."""
    global _verify_mismatches_total
    try:
        mismatches, meta = find_availability_mismatches(
            tenant_slug=tenant_slug,
            days=days,
            from_date=from_date,
            client=client,
        )
    except (ChannexBookingIngestError, ChannexApiError, ValueError) as exc:
        logger.warning(
            "channex availability verify failed: %s",
            exc,
            extra={"tenant_slug": tenant_slug},
        )
        return {
            "skipped": True,
            "reason": str(exc),
            "tenant_slug": tenant_slug,
            "mismatch_count": None,
            "repaired": 0,
        }

    _verify_mismatches_total += len(mismatches)
    if mismatches:
        logger.warning(
            "channex availability mismatches detected",
            extra={"tenant_slug": tenant_slug, "mismatch_count": len(mismatches)},
        )

    if notify and mismatches:
        tenant = Tenant.objects.filter(slug=tenant_slug).first()
        if tenant is not None:
            _notify_mismatches(tenant, mismatches)

    return {
        **meta,
        "mismatch_count": len(mismatches),
        "repaired": 0,
        "mismatches": _mismatch_payload(mismatches),
    }


def repair_availability(
    *,
    tenant_slug: str,
    mismatches: list[AvailabilityMismatch],
    units_checked: int,
    notify: bool = True,
    caller: str = "cli",
) -> dict[str, Any]:
    """Repair from an existing mismatch list (same objects verify produced)."""
    global _repair_skipped_threshold_total, _repair_success_total

    if not mismatches:
        return {
            "tenant_slug": tenant_slug,
            "mismatch_count": 0,
            "repaired": 0,
            "repair_skipped": False,
            "mismatches": [],
        }

    blast = evaluate_repair_blast_radius(mismatches, units_checked=units_checked)
    if blast["threshold"] == "trip":
        _repair_skipped_threshold_total += 1
        logger.warning(
            "channex_repair_skipped_threshold units=%s affected_percent=%s max_days=%s",
            blast["units"],
            blast["affected_percent"],
            blast["max_days"],
            extra={
                "event": "channex_repair_skipped_threshold",
                "tenant": tenant_slug,
                "units": blast["units"],
                "affected_percent": blast["affected_percent"],
                "max_days": blast["max_days"],
                "threshold": "trip",
                "reasons": blast["reasons"],
                "limits": blast["limits"],
            },
        )
        if notify:
            tenant = Tenant.objects.filter(slug=tenant_slug).first()
            if tenant is not None:
                _notify_mismatches(tenant, mismatches)
        return {
            "tenant_slug": tenant_slug,
            "mismatch_count": len(mismatches),
            "repaired": 0,
            "repair_skipped": True,
            "repair_skip_reason": "threshold",
            "blast_radius": blast,
            "mismatches": _mismatch_payload(mismatches),
        }

    try:
        assert_can_write(
            tenant=tenant_slug,
            operation="availability.repair",
            caller=caller,
        )
    except ChannexWriteDisabled as exc:
        return {
            "tenant_slug": tenant_slug,
            "mismatch_count": len(mismatches),
            "repaired": 0,
            "repair_skipped": True,
            "repair_skip_reason": str(exc.reason or "write_disabled"),
            "blast_radius": blast,
            "mismatches": _mismatch_payload(mismatches),
        }

    integration = get_active_channex_integration(tenant_slug)
    repaired = _repair_mismatches(integration, mismatches)
    if repaired:
        _repair_success_total += repaired
        logger.warning(
            "channex availability mismatches repaired",
            extra={
                "tenant_slug": tenant_slug,
                "mismatch_count": len(mismatches),
                "repaired": repaired,
            },
        )

    if notify and mismatches:
        tenant = Tenant.objects.filter(slug=tenant_slug).first()
        if tenant is not None:
            _notify_mismatches(tenant, mismatches)

    return {
        "tenant_slug": tenant_slug,
        "mismatch_count": len(mismatches),
        "repaired": repaired,
        "repair_skipped": False,
        "blast_radius": blast,
        "mismatches": _mismatch_payload(mismatches),
    }


def verify_and_repair_availability(
    *,
    tenant_slug: str,
    days: int = DEFAULT_VERIFY_DAYS,
    from_date: date | None = None,
    repair: bool = False,
    notify: bool = True,
    client: ChannexClient | None = None,
    caller: str = "cli",
) -> dict[str, Any]:
    """Verify via shared diff engine; optionally repair the same mismatch list.

    Default ``repair=False`` (verify-only). Callers that write must pass
    ``repair=True`` explicitly on an authorized writer host.
    """
    global _verify_mismatches_total
    try:
        mismatches, meta = find_availability_mismatches(
            tenant_slug=tenant_slug,
            days=days,
            from_date=from_date,
            client=client,
        )
    except (ChannexBookingIngestError, ChannexApiError, ValueError) as exc:
        logger.warning(
            "channex availability verify failed: %s",
            exc,
            extra={"tenant_slug": tenant_slug},
        )
        return {
            "skipped": True,
            "reason": str(exc),
            "tenant_slug": tenant_slug,
            "mismatch_count": None,
            "repaired": 0,
        }

    _verify_mismatches_total += len(mismatches)
    base = {
        **meta,
        "mismatch_count": len(mismatches),
        "repaired": 0,
        "mismatches": _mismatch_payload(mismatches),
    }

    if not repair:
        if mismatches:
            logger.warning(
                "channex availability mismatches detected",
                extra={"tenant_slug": tenant_slug, "mismatch_count": len(mismatches)},
            )
        if notify and mismatches:
            tenant = Tenant.objects.filter(slug=tenant_slug).first()
            if tenant is not None:
                _notify_mismatches(tenant, mismatches)
        return base

    if not mismatches:
        return base

    return {
        **base,
        **repair_availability(
            tenant_slug=tenant_slug,
            mismatches=mismatches,
            units_checked=int(meta.get("units_checked") or 0),
            notify=notify,
            caller=caller,
        ),
        "from_date": meta.get("from_date"),
        "to_date": meta.get("to_date"),
        "units_checked": meta.get("units_checked"),
        "tenant_id": meta.get("tenant_id"),
        "property_id": meta.get("property_id"),
    }
