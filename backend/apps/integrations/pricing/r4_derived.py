"""R4 channel rates derived from the R1/R2 king-room band (90% of same-day base)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from apps.integrations.channex.ari_service import (
    apply_rate_updates,
    get_active_channex_integration,
)
from apps.integrations.models import ChannelRatePlan, RatePlanDay, SalesChannel
from apps.properties.models import Property
from apps.tenants.models import Tenant

R4_RATE_MULTIPLIER = Decimal("0.90")
R4_SOURCE_UNIT_CODES: tuple[str, ...] = ("R1", "R2")
R4_TARGET_UNIT_CODE = "R4"
R4_RATE_PLAN_CODE = "standard"
R4_SALES_CHANNELS: tuple[str, ...] = (
    SalesChannel.BOOKING_COM,
    SalesChannel.DIRECT,
)


def derive_r4_base_rate(source_rate: Decimal) -> Decimal:
    """R4 1-adult base = 90% of R1/R2 same-day base, rounded to EUR cents."""
    return (Decimal(str(source_rate)) * R4_RATE_MULTIPLIER).quantize(Decimal("0.01"))


def merge_king_band_day_rates(
    rates_by_unit: dict[str, dict[date, Decimal]],
    *,
    prefer: tuple[str, ...] = R4_SOURCE_UNIT_CODES,
) -> dict[date, Decimal]:
    """Prefer R1, fall back to R2 (same king-room band); do not invent dates."""
    merged: dict[date, Decimal] = {}
    for unit_code in prefer:
        for day, rate in rates_by_unit.get(unit_code, {}).items():
            merged.setdefault(day, rate)
    return merged


def compress_rate_days(
    day_rates: dict[date, Decimal],
) -> list[tuple[date, date, Decimal]]:
    """Group consecutive calendar days that share the same rate into ranges."""
    if not day_rates:
        return []

    ordered = sorted(day_rates.items())
    ranges: list[tuple[date, date, Decimal]] = []
    start, end, rate = ordered[0][0], ordered[0][0], ordered[0][1]

    for day, day_rate in ordered[1:]:
        if day == end + timedelta(days=1) and day_rate == rate:
            end = day
            continue
        ranges.append((start, end, rate))
        start, end, rate = day, day, day_rate

    ranges.append((start, end, rate))
    return ranges


@dataclass(frozen=True)
class R4DerivedSyncResult:
    sales_channel: str
    source_days: int
    written: int
    created: int
    updated: int
    unchanged: int
    dry_run: bool


def _source_day_rates(
    *,
    tenant: Tenant,
    prop: Property,
    sales_channel: str,
    rate_plan_code: str,
    source_unit_codes: tuple[str, ...],
    date_from: date | None,
    date_to: date | None,
) -> dict[str, dict[date, Decimal]]:
    qs = RatePlanDay.objects.filter(
        tenant=tenant,
        rate_plan__property=prop,
        rate_plan__sales_channel=sales_channel,
        rate_plan__code=rate_plan_code,
        rate_plan__is_active=True,
        rate_plan__unit__code__in=source_unit_codes,
    ).select_related("rate_plan__unit")
    if date_from is not None:
        qs = qs.filter(date__gte=date_from)
    if date_to is not None:
        qs = qs.filter(date__lte=date_to)

    by_unit: dict[str, dict[date, Decimal]] = {code: {} for code in source_unit_codes}
    for row in qs.iterator(chunk_size=500):
        by_unit[row.rate_plan.unit.code][row.date] = row.rate
    return by_unit


def sync_r4_rates_from_king_band(
    *,
    tenant_slug: str = "uzorita",
    property_slug: str = "uzorita",
    rate_plan_code: str = R4_RATE_PLAN_CODE,
    source_unit_codes: tuple[str, ...] = R4_SOURCE_UNIT_CODES,
    target_unit_code: str = R4_TARGET_UNIT_CODE,
    sales_channels: tuple[str, ...] = R4_SALES_CHANNELS,
    date_from: date | None = None,
    date_to: date | None = None,
    queue_push: bool = True,
    dry_run: bool = False,
) -> list[R4DerivedSyncResult]:
    """
    For each date where R1 (preferred) or R2 has a RatePlanDay, set R4 base to 90%.

    OBP is unchanged (same as R1: primary occupancy 2, adult_delta €5) — only the
    stored 1-adult base is derived; Channex push still uses channex_push_rate_for_unit.
    """
    tenant = Tenant.objects.filter(slug=tenant_slug).first()
    if tenant is None:
        raise ValueError(f"Tenant not found: {tenant_slug}")

    prop = Property.objects.filter(tenant=tenant, slug=property_slug).first()
    if prop is None:
        raise ValueError(f"Property not found: {property_slug} (tenant {tenant_slug})")

    target_plans = {
        plan.sales_channel: plan
        for plan in ChannelRatePlan.objects.filter(
            tenant=tenant,
            property=prop,
            unit__code=target_unit_code,
            code=rate_plan_code,
            is_active=True,
            sales_channel__in=sales_channels,
        ).select_related("unit")
    }
    missing = [ch for ch in sales_channels if ch not in target_plans]
    if missing:
        raise ValueError(
            f"Missing active {target_unit_code}/{rate_plan_code} rate plan(s) "
            f"for sales_channel={missing}"
        )

    integration = None
    if queue_push and not dry_run and SalesChannel.BOOKING_COM in sales_channels:
        integration = get_active_channex_integration(tenant_slug)

    results: list[R4DerivedSyncResult] = []
    for sales_channel in sales_channels:
        target_plan = target_plans[sales_channel]
        source_by_unit = _source_day_rates(
            tenant=tenant,
            prop=prop,
            sales_channel=sales_channel,
            rate_plan_code=rate_plan_code,
            source_unit_codes=source_unit_codes,
            date_from=date_from,
            date_to=date_to,
        )
        merged = merge_king_band_day_rates(source_by_unit, prefer=source_unit_codes)
        derived = {day: derive_r4_base_rate(rate) for day, rate in merged.items()}

        existing = {
            row.date: row.rate
            for row in RatePlanDay.objects.filter(
                tenant=tenant,
                rate_plan=target_plan,
                date__in=derived.keys(),
            )
        }

        created = updated = unchanged = 0
        updates: list[dict[str, Any]] = []
        for day, rate in sorted(derived.items()):
            current = existing.get(day)
            if current == rate:
                unchanged += 1
                continue
            if current is None:
                created += 1
            else:
                updated += 1
            updates.append(
                {
                    "unit_code": target_unit_code,
                    "rate_plan_code": rate_plan_code,
                    "sales_channel": sales_channel,
                    "date_from": day,
                    "date_to": day,
                    "rate": rate,
                }
            )

        # Compress consecutive same-rate days for fewer outbox values.
        change_days = {
            item["date_from"]: item["rate"]
            for item in updates
        }
        compressed_updates = [
            {
                "unit_code": target_unit_code,
                "rate_plan_code": rate_plan_code,
                "sales_channel": sales_channel,
                "date_from": start,
                "date_to": end,
                "rate": rate,
            }
            for start, end, rate in compress_rate_days(change_days)
        ]

        written = 0
        if compressed_updates and not dry_run:
            if integration is not None:
                push = queue_push and sales_channel == SalesChannel.BOOKING_COM
                saved = apply_rate_updates(
                    integration,
                    compressed_updates,
                    queue_push=push,
                )
                written = len(saved)
            else:
                for item in compressed_updates:
                    day = item["date_from"]
                    day_to = item["date_to"]
                    rate = Decimal(str(item["rate"]))
                    current = day
                    while current <= day_to:
                        RatePlanDay.objects.update_or_create(
                            tenant=tenant,
                            rate_plan=target_plan,
                            date=current,
                            defaults={"rate": rate, "synced_at": None},
                        )
                        written += 1
                        current += timedelta(days=1)

        results.append(
            R4DerivedSyncResult(
                sales_channel=sales_channel,
                source_days=len(merged),
                written=written if not dry_run else 0,
                created=created,
                updated=updated,
                unchanged=unchanged,
                dry_run=dry_run,
            )
        )

    return results
