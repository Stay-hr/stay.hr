from __future__ import annotations

from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.integrations.channex.booking_service import (
    _resolve_channex_booking_payload,
    parse_channex_booking_id,
)
from apps.integrations.channex.client import ChannexClient
from apps.integrations.channex.config import ChannexRuntimeConfig
from apps.integrations.channex.exceptions import ChannexApiError, ChannexBookingIngestError
from apps.integrations.models import IntegrationConfig
from apps.reservations.channel_sync import IMPORT_SOURCE_CHANNEX
from apps.reservations.models import Reservation

OPERATOR_NOTE_PREFIX = "Channex cancel reconcile"


def is_channex_cancel_eligible(reservation: Reservation) -> bool:
    if reservation.import_source != IMPORT_SOURCE_CHANNEX:
        return False
    if reservation.status == Reservation.Status.CANCELED:
        return False
    return bool(parse_channex_booking_id(reservation.external_id))


def build_cancel_booking_payload(booking_data: dict[str, Any]) -> dict[str, Any]:
    attrs = dict(booking_data.get("attributes") or booking_data)
    return {
        "booking": {
            "status": "cancelled",
            "property_id": attrs["property_id"],
            "ota_reservation_code": attrs.get("ota_reservation_code"),
            "ota_name": attrs.get("ota_name"),
            "arrival_date": attrs.get("arrival_date"),
            "departure_date": attrs.get("departure_date"),
            "arrival_hour": attrs.get("arrival_hour") or "",
            "currency": attrs.get("currency"),
            "payment_collect": attrs.get("payment_collect"),
            "payment_type": attrs.get("payment_type"),
            "ota_commission": str(attrs.get("ota_commission") or "0"),
            "notes": attrs.get("notes") or "",
            "customer": attrs.get("customer") or {},
            "rooms": attrs.get("rooms") or [],
            "services": attrs.get("services") or [],
            "deposits": attrs.get("deposits") or [],
        }
    }


def cancel_booking_for_reservation(
    integration: IntegrationConfig,
    reservation: Reservation,
) -> str:
    """Cancel OTA booking in Channex (CRS). Returns Channex booking UUID."""
    config = ChannexRuntimeConfig.from_integration_dict(integration.get_config_dict())
    with ChannexClient(config) as client:
        resolved = _resolve_channex_booking_payload(client, reservation)
        if resolved is None:
            raise ChannexBookingIngestError(
                "Rezervacija nema Channex booking ID za otkaz."
            )
        booking_id, payload, _lookup = resolved
        cancel_payload = build_cancel_booking_payload(payload)
        try:
            client.cancel_booking(booking_id, cancel_payload)
        except ChannexApiError as exc:
            raise ChannexBookingIngestError(str(exc)) from exc
        return booking_id


def mark_reservation_canceled_locally(
    reservation: Reservation,
    *,
    note_suffix: str = "",
) -> None:
    now = timezone.now()
    existing = (reservation.notes or "").strip()
    line = f"Otkazano {now.date().isoformat()} — overbooking 24.7.2026.{note_suffix}"
    if line not in existing:
        reservation.notes = f"{existing}\n{line}".strip() if existing else line
    reservation.status = Reservation.Status.CANCELED
    reservation.booking_status = "cancelled"
    reservation.canceled_at = now
    reservation.save(
        update_fields=[
            "status",
            "booking_status",
            "canceled_at",
            "notes",
            "updated_at",
        ]
    )


def heal_channex_cancel_locally(reservation_id: int) -> str:
    """
    Apply a missed Channex cancel under row lock.

    Returns:
        healed | already_canceled | skipped_no_show | skipped_ineligible
    """
    with transaction.atomic():
        reservation = Reservation.objects.select_for_update().get(pk=reservation_id)

        if reservation.status == Reservation.Status.CANCELED:
            return "already_canceled"
        if reservation.status == Reservation.Status.NO_SHOW:
            return "skipped_no_show"
        if reservation.status not in {
            Reservation.Status.EXPECTED,
            Reservation.Status.CHECKED_IN,
        }:
            return "skipped_ineligible"

        now = timezone.now()
        note_line = f"{OPERATOR_NOTE_PREFIX} {now.date().isoformat()}"
        existing_notes = (reservation.notes or "").strip()
        if note_line not in existing_notes:
            reservation.notes = (
                f"{existing_notes}\n{note_line}".strip() if existing_notes else note_line
            )

        reservation.status = Reservation.Status.CANCELED
        reservation.booking_status = "cancelled"
        if reservation.canceled_at is None:
            reservation.canceled_at = now
        reservation.save(
            update_fields=[
                "status",
                "booking_status",
                "canceled_at",
                "notes",
                "updated_at",
            ]
        )
        return "healed"
