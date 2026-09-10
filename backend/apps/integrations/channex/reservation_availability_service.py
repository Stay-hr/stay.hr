from __future__ import annotations

import logging
from datetime import date, timedelta

from django.db import transaction

from apps.integrations.channex.ari_service import apply_availability_updates, get_active_channex_integration
from apps.integrations.channex.exceptions import ChannexBookingIngestError
from apps.integrations.channel_manager.resolver import get_channel_manager
from apps.integrations.models import UnitAvailabilityBlock
from apps.properties.models import Property, Unit
from apps.reservations.models import Reservation, ReservationUnit
from apps.tenants.models import ChannelManager

logger = logging.getLogger(__name__)

IMPORT_SOURCE_CHANNEX = "channex"

# Uzorita: multi-room / whole-property bookings must close all B.com listings.
UZORITA_WHOLE_PROPERTY_UNIT_CODES = frozenset({"R1", "R2", "R3", "R6"})

# Durable ARI close for property room types not held by ReservationUnit
# (multi-room suspect / mismatch / whole-property extras). Cleared on cancel.
PROPERTY_CLOSE_BLOCK_REF_PREFIX = "property-close:"

SYNC_AVAILABILITY_STATUSES = frozenset(
    {
        Reservation.Status.PENDING,
        Reservation.Status.EXPECTED,
        Reservation.Status.CHECKED_IN,
    }
)

# One-shot ops holds: keep a listing open while the original stay is still
# assigned, then close it if a replacement occupies the same unit/night
# (controlled overbooking). Tuple is (reservation_id, unit_code, night).
CONTROLLED_OVERBOOKING_HOLDS: tuple[tuple[int, str, date], ...] = ()


def should_sync_channex_availability(reservation: Reservation) -> bool:
    if get_channel_manager(reservation.tenant) != ChannelManager.CHANNEX:
        return False
    if reservation.import_source == IMPORT_SOURCE_CHANNEX:
        return False
    if reservation.status not in SYNC_AVAILABILITY_STATUSES:
        return False

    try:
        integration = get_active_channex_integration(reservation.tenant.slug)
    except ChannexBookingIngestError:
        return False

    config = integration.get_config_dict()
    mapped_codes = {
        str(row.get("unit_code") or "")
        for row in (config.get("room_types") or []) + (config.get("booking_test_rooms") or [])
        if row.get("unit_code")
    }
    if not mapped_codes:
        return False

    unit_codes = ReservationUnit.objects.filter(reservation=reservation).values_list(
        "unit__code", flat=True
    )
    return all(code and code in mapped_codes for code in unit_codes)


def compute_unit_availability(tenant, unit: Unit, day: date) -> int:
    """Availability for a single-unit room type on one night [day, day+1)."""
    base_count = 1
    night_end = day + timedelta(days=1)

    reservation_overlap = ReservationUnit.objects.filter(
        tenant=tenant,
        unit=unit,
        reservation__status__in=SYNC_AVAILABILITY_STATUSES,
        reservation__check_in__lt=night_end,
        reservation__check_out__gt=day,
    ).exists()
    if reservation_overlap:
        return max(0, base_count - 1)

    block_overlap = UnitAvailabilityBlock.objects.filter(
        tenant=tenant,
        unit=unit,
        check_in__lt=night_end,
        check_out__gt=day,
    ).exists()
    if block_overlap:
        return max(0, base_count - 1)

    return base_count


def _night_range(check_in: date, check_out: date) -> list[date]:
    nights: list[date] = []
    current = check_in
    while current < check_out:
        nights.append(current)
        current += timedelta(days=1)
    return nights


def push_availability_range_for_unit(
    tenant,
    unit: Unit,
    check_in: date,
    check_out: date,
) -> dict:
    integration = get_active_channex_integration(tenant.slug)
    updates = [
        {
            "unit_code": unit.code,
            "date": night.isoformat(),
            "availability": compute_unit_availability(tenant, unit, night),
        }
        for night in _night_range(check_in, check_out)
    ]
    if not updates:
        return {"skipped": True, "reason": "empty_range", "unit_code": unit.code}

    apply_availability_updates(integration, updates, queue_push=True)
    return {"pushed": True, "unit_code": unit.code, "nights": len(updates)}


def _mapped_unit_codes_on_reservation(reservation: Reservation) -> set[str]:
    return {
        row.unit.code
        for row in ReservationUnit.objects.filter(reservation=reservation).select_related("unit")
        if row.unit_id and row.unit.code
    }


def _property_close_block_ref(reservation_id: int, unit_id: int) -> str:
    return f"{PROPERTY_CLOSE_BLOCK_REF_PREFIX}{reservation_id}:{unit_id}"


def _distinct_mapped_unit_count(reservation: Reservation) -> int:
    """How many distinct physical units are assigned to this reservation."""
    return (
        ReservationUnit.objects.filter(reservation=reservation, unit_id__isnull=False)
        .values("unit_id")
        .distinct()
        .count()
    )


def multi_room_assignment_is_consistent(reservation: Reservation) -> bool:
    """
    True when the channel's room count is known and stay.hr holds exactly that
    many distinct units, with no open room warning.

    A consistent multi-room stay closes its own units through occupancy, so the
    competing listings must stay open. Anything else — unknown ``units_count``,
    a count mismatch in either direction, or an open room warning — keeps the
    whole-property close as an overbooking guard.
    """
    from apps.integrations.channex.booking_room_mismatch import (
        CHANNEX_EMPTY_ROOMS_NOTE,
        CHANNEX_ROOMS_MISMATCH_NOTE,
        MULTI_ROOM_SUSPECT_NOTE,
    )

    expected = reservation.units_count
    if not expected or expected < 1:
        return False
    if _distinct_mapped_unit_count(reservation) != expected:
        return False

    notes = reservation.notes or ""
    return not any(
        prefix in notes
        for prefix in (
            MULTI_ROOM_SUSPECT_NOTE,
            CHANNEX_EMPTY_ROOMS_NOTE,
            CHANNEX_ROOMS_MISMATCH_NOTE,
        )
    )


def mapped_channex_unit_codes_for_property(*, integration, property: Property) -> frozenset[str]:
    """Active stay.hr unit codes that have a Channex room-type mapping for this property."""
    config = integration.get_config_dict()
    mapped = {
        str(row.get("unit_code") or "")
        for row in (config.get("room_types") or []) + (config.get("booking_test_rooms") or [])
        if row.get("unit_code")
    }
    if not mapped:
        return frozenset()
    return frozenset(
        Unit.objects.filter(
            tenant=integration.tenant,
            property=property,
            code__in=mapped,
            is_active=True,
        ).values_list("code", flat=True)
    )


def property_whole_close_unit_codes(*, integration, property: Property) -> frozenset[str]:
    """
    Unit codes to close for whole-property / multi-room-suspect inventory.

    Uzorita keeps the explicit whole-property listing set; other properties close
    every active Channex-mapped room type on that property.
    """
    if property.slug == "uzorita":
        return UZORITA_WHOLE_PROPERTY_UNIT_CODES
    return mapped_channex_unit_codes_for_property(integration=integration, property=property)


def qualifies_for_whole_property_sync(
    reservation: Reservation,
    integration=None,
) -> bool:
    """True when multi-room assignment warrants closing all property listings."""
    close_codes = _resolve_property_close_codes(reservation, integration)
    if not close_codes:
        return False
    codes = _mapped_unit_codes_on_reservation(reservation)
    overlap = codes & close_codes
    if not overlap:
        return False
    if multi_room_assignment_is_consistent(reservation):
        return False
    return len(overlap) >= 2 or (
        (reservation.units_count or 0) >= 2 and len(overlap) >= 1
    )


def _resolve_property_close_codes(reservation: Reservation, integration=None) -> frozenset[str]:
    if reservation.property.slug == "uzorita":
        return UZORITA_WHOLE_PROPERTY_UNIT_CODES
    if integration is None:
        try:
            integration = get_active_channex_integration(reservation.tenant.slug)
        except ChannexBookingIngestError:
            return frozenset()
    return property_whole_close_unit_codes(
        integration=integration,
        property=reservation.property,
    )


def _ensure_property_close_block(reservation: Reservation, unit: Unit) -> UnitAvailabilityBlock:
    """Block a competing listing for this stay so compute_unit_availability stays at 0."""
    block_ref = _property_close_block_ref(reservation.pk, unit.pk)
    block, _ = UnitAvailabilityBlock.objects.update_or_create(
        tenant=reservation.tenant,
        block_ref=block_ref,
        defaults={
            "unit": unit,
            "reservation": reservation,
            "check_in": reservation.check_in,
            "check_out": reservation.check_out,
            "created_via": UnitAvailabilityBlock.CreatedVia.STAY,
        },
    )
    return block


def _clear_property_close_blocks(reservation: Reservation) -> list[Unit]:
    """Remove property-close blocks for a reservation; return units that were blocked."""
    blocks = list(
        UnitAvailabilityBlock.objects.filter(
            tenant=reservation.tenant,
            reservation=reservation,
            block_ref__startswith=PROPERTY_CLOSE_BLOCK_REF_PREFIX,
        ).select_related("unit")
    )
    units = [b.unit for b in blocks if b.unit_id]
    if blocks:
        UnitAvailabilityBlock.objects.filter(pk__in=[b.pk for b in blocks]).delete()
    return units


def _push_property_close_units(
    reservation: Reservation,
    integration,
    *,
    close_codes: frozenset[str],
    results: list[dict],
    force_unmapped: bool,
) -> None:
    """
    Push ARI for property close codes.

    When force_unmapped is True, units not held by ReservationUnit get a durable
    property-close block so availability computes to 0 (and survives verify/full sync).
    """
    tenant = reservation.tenant
    pushed_codes = {r.get("unit_code") for r in results if r.get("unit_code")}
    mapped_unit_ids = set(
        ReservationUnit.objects.filter(
            reservation=reservation,
            unit_id__isnull=False,
        ).values_list("unit_id", flat=True)
    )
    for unit in Unit.objects.filter(
        tenant=tenant,
        property=reservation.property,
        code__in=close_codes,
        is_active=True,
    ):
        if unit.code in pushed_codes:
            continue
        if force_unmapped and unit.pk not in mapped_unit_ids:
            _ensure_property_close_block(reservation, unit)
        results.append(
            push_availability_range_for_unit(
                tenant,
                unit,
                reservation.check_in,
                reservation.check_out,
            )
        )


def maybe_push_whole_property_availability(
    reservation: Reservation,
    integration,
    *,
    results: list[dict],
) -> None:
    if not qualifies_for_whole_property_sync(reservation, integration):
        return
    close_codes = property_whole_close_unit_codes(
        integration=integration,
        property=reservation.property,
    )
    _push_property_close_units(
        reservation,
        integration,
        close_codes=close_codes,
        results=results,
        force_unmapped=True,
    )


def force_close_property_channex_availability(
    reservation: Reservation,
    *,
    reason: str = "",
) -> dict:
    """
    Force ARI close for all property room types over the reservation stay.

    Used on MULTI_ROOM_SUSPECT / CHANNEX_EMPTY_ROOMS / rooms mismatch so under-
    reported multi-room bookings cannot leave competing listings open — even when
    stay.hr has 0–1 mapped units.
    """
    if get_channel_manager(reservation.tenant) != ChannelManager.CHANNEX:
        return {"skipped": True, "reason": "not_channex", "reservation_id": reservation.pk}

    if reservation.status in {Reservation.Status.CANCELED, Reservation.Status.NO_SHOW}:
        return {"skipped": True, "reason": "inactive_status", "reservation_id": reservation.pk}

    if reservation.status not in SYNC_AVAILABILITY_STATUSES:
        return {
            "skipped": True,
            "reason": "status_not_syncable",
            "reservation_id": reservation.pk,
        }

    try:
        integration = get_active_channex_integration(reservation.tenant.slug)
    except ChannexBookingIngestError as exc:
        return {"skipped": True, "reason": str(exc), "reservation_id": reservation.pk}

    close_codes = property_whole_close_unit_codes(
        integration=integration,
        property=reservation.property,
    )
    if not close_codes:
        return {
            "skipped": True,
            "reason": "no_property_close_codes",
            "reservation_id": reservation.pk,
        }

    results: list[dict] = []
    # Refresh mapped units first (occupancy-driven), then force-close the rest.
    for row in ReservationUnit.objects.filter(reservation=reservation).select_related("unit"):
        if row.unit is None:
            continue
        results.append(
            push_availability_range_for_unit(
                reservation.tenant,
                row.unit,
                reservation.check_in,
                reservation.check_out,
            )
        )

    _push_property_close_units(
        reservation,
        integration,
        close_codes=close_codes,
        results=results,
        force_unmapped=True,
    )

    from apps.integrations.channex.ari_service import push_channex_ari

    push_channex_ari(integration)
    logger.warning(
        "channex property-wide ARI close forced",
        extra={
            "reservation_id": reservation.pk,
            "booking_code": reservation.booking_code,
            "reason": reason,
            "close_codes": sorted(close_codes),
            "units": len(results),
        },
    )
    return {
        "reservation_id": reservation.pk,
        "pushed": True,
        "forced": True,
        "reason": reason,
        "units": results,
    }


def push_reservation_channex_availability_unconditional(reservation: Reservation) -> dict:
    """
    Recompute and push ARI for all mapped units (and whole-property extras).

    Used after unit assignment changes regardless of import_source.
    """
    if get_channel_manager(reservation.tenant) != ChannelManager.CHANNEX:
        return {"skipped": True, "reason": "not_channex", "reservation_id": reservation.pk}

    if reservation.status in {Reservation.Status.CANCELED, Reservation.Status.NO_SHOW}:
        return remove_reservation_channex_availability(reservation)

    if reservation.status not in SYNC_AVAILABILITY_STATUSES:
        return {
            "skipped": True,
            "reason": "status_not_syncable",
            "reservation_id": reservation.pk,
        }

    try:
        integration = get_active_channex_integration(reservation.tenant.slug)
    except ChannexBookingIngestError as exc:
        return {"skipped": True, "reason": str(exc), "reservation_id": reservation.pk}

    unit_rows = list(
        ReservationUnit.objects.filter(reservation=reservation).select_related("unit")
    )
    if not unit_rows:
        return {"skipped": True, "reason": "no_units", "reservation_id": reservation.pk}

    results: list[dict] = []
    for row in unit_rows:
        if row.unit is None:
            results.append({"skipped": True, "reason": "unmapped_unit", "room_name": row.room_name})
            continue
        results.append(
            push_availability_range_for_unit(
                reservation.tenant,
                row.unit,
                reservation.check_in,
                reservation.check_out,
            )
        )

    maybe_push_whole_property_availability(reservation, integration, results=results)

    from apps.integrations.channex.ari_service import push_channex_ari

    push_channex_ari(integration)
    logger.info(
        "channex inventory pushed for reservation",
        extra={
            "reservation_id": reservation.pk,
            "status": reservation.status,
            "import_source": reservation.import_source,
            "units": len(results),
        },
    )
    return {"reservation_id": reservation.pk, "pushed": True, "units": results}


def occupying_reservation_ids(tenant, unit: Unit, day: date) -> list[int]:
    """Active reservation PKs that occupy ``unit`` on ``day``."""
    night_end = day + timedelta(days=1)
    return list(
        ReservationUnit.objects.filter(
            tenant=tenant,
            unit=unit,
            reservation__status__in=SYNC_AVAILABILITY_STATUSES,
            reservation__check_in__lt=night_end,
            reservation__check_out__gt=day,
        )
        .order_by("reservation_id")
        .values_list("reservation_id", flat=True)
        .distinct()
    )


def sync_controlled_overbooking_holds(*, tenant=None) -> list[dict]:
    """Re-apply controlled overbooking ARI after ingest (open while alone, close if replaced)."""
    from apps.integrations.channex.ari_service import push_channex_ari

    results: list[dict] = []
    for reservation_id, unit_code, night in CONTROLLED_OVERBOOKING_HOLDS:
        original = (
            Reservation.objects.filter(pk=reservation_id)
            .select_related("tenant")
            .first()
        )
        if original is None:
            results.append(
                {"skipped": True, "reason": "missing_reservation", "reservation_id": reservation_id}
            )
            continue
        if tenant is not None and original.tenant_id != tenant.pk:
            continue
        unit = Unit.objects.filter(
            tenant=original.tenant,
            code=unit_code,
            is_active=True,
        ).first()
        if unit is None:
            results.append(
                {
                    "skipped": True,
                    "reason": "missing_unit",
                    "reservation_id": reservation_id,
                    "unit_code": unit_code,
                }
            )
            continue
        occupant_ids = occupying_reservation_ids(original.tenant, unit, night)
        original_occupying = reservation_id in occupant_ids
        replacement_occupying = any(pk != reservation_id for pk in occupant_ids)
        if replacement_occupying:
            availability = 0
        elif original_occupying:
            availability = 1
        else:
            results.append(
                {
                    "skipped": True,
                    "reason": "original_released",
                    "reservation_id": reservation_id,
                    "unit_code": unit_code,
                    "occupants": occupant_ids,
                }
            )
            continue
        try:
            integration = get_active_channex_integration(original.tenant.slug)
        except ChannexBookingIngestError as exc:
            results.append(
                {
                    "skipped": True,
                    "reason": str(exc),
                    "reservation_id": reservation_id,
                }
            )
            continue
        apply_availability_updates(
            integration,
            [
                {
                    "unit_code": unit_code,
                    "date": night.isoformat(),
                    "availability": availability,
                }
            ],
            queue_push=True,
        )
        push_channex_ari(integration)
        logger.info(
            "controlled overbooking ARI hold applied",
            extra={
                "reservation_id": reservation_id,
                "unit_code": unit_code,
                "night": night.isoformat(),
                "availability": availability,
                "occupants": occupant_ids,
            },
        )
        results.append(
            {
                "reservation_id": reservation_id,
                "unit_code": unit_code,
                "night": night.isoformat(),
                "availability": availability,
                "occupants": occupant_ids,
            }
        )
    return results


def push_channex_inventory_after_ingest(reservation_id: int) -> dict:
    """
    Recompute and push ARI to Channex after inbound booking ingest/cancel.

    Unlike manual/web outbound sync, this runs for import_source=channex so the
    channel calendar reflects stay.hr inventory including the new booking.
    """
    reservation = (
        Reservation.objects.filter(pk=reservation_id)
        .select_related("tenant", "property")
        .first()
    )
    if reservation is None:
        return {"skipped": True, "reason": "not_found", "reservation_id": reservation_id}

    result = push_reservation_channex_availability_unconditional(reservation)
    result["controlled_overbooking_holds"] = sync_controlled_overbooking_holds(
        tenant=reservation.tenant,
    )
    return result


@transaction.atomic
def sync_reservation_channex_availability(reservation: Reservation) -> dict:
    if not should_sync_channex_availability(reservation):
        return {"skipped": True, "reason": "should_not_sync", "reservation_id": reservation.pk}

    integration = get_active_channex_integration(reservation.tenant.slug)
    results: list[dict] = []
    for row in ReservationUnit.objects.filter(reservation=reservation).select_related("unit"):
        if row.unit is None:
            continue
        result = push_availability_range_for_unit(
            reservation.tenant,
            row.unit,
            reservation.check_in,
            reservation.check_out,
        )
        results.append(result)

    from apps.integrations.channex.ari_service import push_channex_ari

    push_channex_ari(integration)
    return {
        "reservation_id": reservation.pk,
        "pushed": True,
        "units": results,
    }


def remove_reservation_channex_availability(reservation: Reservation) -> dict:
    if get_channel_manager(reservation.tenant) != ChannelManager.CHANNEX:
        return {"skipped": True, "reason": "not_channex", "reservation_id": reservation.pk}

    integration = get_active_channex_integration(reservation.tenant.slug)
    # Drop property-close blocks before recompute so competing listings reopen.
    blocked_units = _clear_property_close_blocks(reservation)
    results: list[dict] = []
    seen_unit_ids: set[int] = set()
    for row in ReservationUnit.objects.filter(reservation=reservation).select_related("unit"):
        if row.unit is None:
            continue
        seen_unit_ids.add(row.unit_id)
        result = push_availability_range_for_unit(
            reservation.tenant,
            row.unit,
            reservation.check_in,
            reservation.check_out,
        )
        results.append(result)

    for unit in blocked_units:
        if unit.pk in seen_unit_ids:
            continue
        seen_unit_ids.add(unit.pk)
        results.append(
            push_availability_range_for_unit(
                reservation.tenant,
                unit,
                reservation.check_in,
                reservation.check_out,
            )
        )

    from apps.integrations.channex.ari_service import push_channex_ari

    push_channex_ari(integration)
    return {
        "reservation_id": reservation.pk,
        "pushed": True,
        "units": results,
    }


def sync_reservation_channex_availability_for_dates(
    reservation: Reservation,
    check_in: date,
    check_out: date,
) -> dict:
    """Push availability delta for a specific date range (e.g. after date change)."""
    if get_channel_manager(reservation.tenant) != ChannelManager.CHANNEX:
        return {"skipped": True, "reason": "not_channex", "reservation_id": reservation.pk}

    integration = get_active_channex_integration(reservation.tenant.slug)
    results: list[dict] = []
    for row in ReservationUnit.objects.filter(reservation=reservation).select_related("unit"):
        if row.unit is None:
            continue
        results.append(
            push_availability_range_for_unit(
                reservation.tenant,
                row.unit,
                check_in,
                check_out,
            )
        )

    from apps.integrations.channex.ari_service import push_channex_ari

    push_channex_ari(integration)
    return {"reservation_id": reservation.pk, "pushed": True, "units": results}
