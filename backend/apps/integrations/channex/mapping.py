"""
Channex room type + rate plan UUIDs for Uzorita (5 physical units, count=1 each).

Staging UUIDs: certification on staging.channex.io
Production UUIDs: Luxury Room Uzorita B&B on app.channex.io (property bca8473d-…)
"""

from __future__ import annotations

from decimal import Decimal
from typing import TypedDict

# Booking.com OBP reduction model: primary occupancy = max adults (full/normal price).
# stay.hr stores 1-adult base; Channex push = normal price, auto decrease for fewer guests.
# Optional per-unit override when Booking channel rejects max occupancy (e.g. R6 fallback).
UZORITA_BOOKING_OBP_PRIMARY_OCCUPANCY: dict[str, int] = {}
UZORITA_BOOKING_OBP_ADULT_DELTA = Decimal("5.00")


class ChannexRoomTypeMapping(TypedDict):
    unit_code: str
    channex_room_type_id: str
    channex_title: str


class ChannexRatePlanMapping(TypedDict):
    code: str
    title: str
    channex_rate_plan_id: str
    currency: str


UZORITA_STAGING_ROOM_TYPES: tuple[ChannexRoomTypeMapping, ...] = (
    {
        "unit_code": "R1",
        "channex_room_type_id": "e8fc8060-3df5-4e49-bee9-32903786b4ee",
        "channex_title": "Luxury Room Uzorita - R1",
    },
    {
        "unit_code": "R2",
        "channex_room_type_id": "0d852a5e-41d5-4801-9bf1-679deabcfbec",
        "channex_title": "Luxury Room Uzorita - R2",
    },
    {
        "unit_code": "R6",
        "channex_room_type_id": "ecc2d4ab-7894-4fc9-8e20-c08d2317e4be",
        "channex_title": "Luxury Room Uzorita - R6",
    },
    {
        "unit_code": "R3",
        "channex_room_type_id": "6058e4da-0ed4-48a1-a877-fec38685589a",
        "channex_title": "Luxury Room Uzorita - R3",
    },
)

UZORITA_PRODUCTION_ROOM_TYPES: tuple[ChannexRoomTypeMapping, ...] = (
    {
        "unit_code": "R1",
        "channex_room_type_id": "6db954ee-ec81-4f8d-87fa-587eb43368b6",
        "channex_title": "Luxury Room Uzorita - R1",
    },
    {
        "unit_code": "R2",
        "channex_room_type_id": "757f647a-d16f-4c78-970f-c9247e45e7ed",
        "channex_title": "Luxury Room Uzorita - R2",
    },
    {
        "unit_code": "R3",
        "channex_room_type_id": "4874de9b-0d24-4392-b528-364ab35192d7",
        "channex_title": "Luxury Room Uzorita - R3",
    },
    {
        "unit_code": "R6",
        "channex_room_type_id": "362714ed-177d-4a5f-939e-e3c4de5cc2f1",
        "channex_title": "Luxury Room Uzorita - R6",
    },
    {
        "unit_code": "R4",
        "channex_room_type_id": "158d11e2-dcba-4c0d-b0aa-4d89073866ea",
        "channex_title": "Standard King Room R4",
    },
)

# Parent "Standard Rate" plans (stay.hr pushes restrictions here; channel derives Booking.com rates).
UZORITA_PRODUCTION_RATE_PLANS: dict[str, ChannexRatePlanMapping] = {
    "R1": {
        "code": "standard",
        "title": "Standard Rate",
        "channex_rate_plan_id": "60cf42a0-1448-428b-8409-a51cdfdad684",
        "currency": "EUR",
    },
    "R2": {
        "code": "standard",
        "title": "Standard Rate",
        "channex_rate_plan_id": "9b080961-b09d-44de-aa8b-259a271e0f11",
        "currency": "EUR",
    },
    "R3": {
        "code": "standard",
        "title": "Standard Rate",
        "channex_rate_plan_id": "a64b5c88-6348-4ae0-8690-284b427160bc",
        "currency": "EUR",
    },
    "R6": {
        "code": "standard",
        "title": "Standard Rate",
        "channex_rate_plan_id": "340cfd1c-eb83-4cc7-92e9-aab01735ac9d",
        "currency": "EUR",
    },
    "R4": {
        "code": "standard",
        "title": "Standard Rate",
        "channex_rate_plan_id": "cee70444-3667-4808-bb07-0affa0f26e81",
        "currency": "EUR",
    },
}

UZORITA_PRODUCTION_PROPERTY_ID = "bca8473d-7c36-4986-bcdb-b5760b633283"


def channex_push_rate_for_unit(unit_code: str, stay_rate: Decimal) -> Decimal:
    from apps.integrations.pricing.obp import channex_push_rate_for_unit as _push_rate

    return _push_rate(unit_code, stay_rate)


def _units_by_code(tenant_slug: str = "uzorita") -> dict[str, object]:
    from django.db.models import Count

    from apps.properties.models import Unit
    from apps.tenants.models import Tenant

    tenant = Tenant.objects.filter(slug=tenant_slug).first()
    if tenant is None:
        return {}

    units_by_code: dict[str, Unit] = {}
    for unit in (
        Unit.objects.filter(tenant=tenant, is_active=True)
        .annotate(reservation_links=Count("reservation_units"))
        .order_by("code", "-reservation_links", "-id")
    ):
        units_by_code.setdefault(unit.code, unit)
    return units_by_code


def room_types_config_payload(
    *,
    environment: str = "staging",
    tenant_slug: str = "uzorita",
) -> list[dict[str, str | int]]:
    """Build config.room_types list with stay Unit.id when units exist."""
    rows = (
        UZORITA_PRODUCTION_ROOM_TYPES
        if environment == "production"
        else UZORITA_STAGING_ROOM_TYPES
    )
    units_by_code = _units_by_code(tenant_slug)

    payload: list[dict[str, str | int]] = []
    for row in rows:
        item: dict[str, str | int] = dict(row)
        unit = units_by_code.get(row["unit_code"])
        if unit is not None:
            item["unit_id"] = unit.id
        payload.append(item)
    return payload


def booking_test_rooms_config_payload(
    *,
    environment: str = "production",
    tenant_slug: str = "uzorita",
) -> list[dict[str, object]]:
    """ChannelRatePlan seed source (booking_test_rooms JSON in IntegrationConfig)."""
    if environment != "production":
        return []

    rows = (
        UZORITA_PRODUCTION_ROOM_TYPES
        if environment == "production"
        else UZORITA_STAGING_ROOM_TYPES
    )
    units_by_code = _units_by_code(tenant_slug)
    payload: list[dict[str, object]] = []

    for row in rows:
        unit_code = row["unit_code"]
        rate = UZORITA_PRODUCTION_RATE_PLANS.get(unit_code)
        if rate is None:
            continue
        item: dict[str, object] = {
            "unit_code": unit_code,
            "channex_room_type_id": row["channex_room_type_id"],
            "channex_title": row["channex_title"],
            "rate_plans": [
                {
                    "code": rate["code"],
                    "title": rate["title"],
                    "channex_rate_plan_id": rate["channex_rate_plan_id"],
                    "currency": rate["currency"],
                    "default_gbp": "0",
                }
            ],
        }
        unit = units_by_code.get(unit_code)
        if unit is not None:
            item["unit_id"] = unit.id
        payload.append(item)
    return payload
