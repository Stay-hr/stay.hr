from __future__ import annotations

import logging
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from apps.integrations.channex.booking_test import certification_property_slug
from apps.integrations.channex.client import ChannexClient
from apps.integrations.channex.config import ChannexBookingTestRoomLink, ChannexRuntimeConfig
from apps.integrations.channex.exceptions import ChannexApiError, ChannexBookingIngestError
from apps.integrations.models import ChannexBookingRevision, IntegrationConfig
from apps.properties.models import Property, Unit
from apps.reservations.channel_sync import is_pdf_authoritative
from apps.reservations.guest_slots import ensure_adult_guest_slots
from apps.reservations.models import Guest, Reservation, ReservationUnit
from apps.reservations.phone_validation import normalize_booker_phone
from apps.integrations.channex.booking_room_mismatch import (
    check_channex_revision_room_mismatch,
    flag_channex_ingest_room_warnings,
    should_preserve_units_on_channex_ingest,
)
from apps.communications.guest_email_quality import prefer_usable_invoice_email
from apps.reservations.overbooking import flag_ingest_overbooking
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

CHANNEX_EXTERNAL_ID_PREFIX = "channex:"


def channex_external_id(booking_id: str) -> str:
    booking_id = booking_id.strip()
    if booking_id.startswith(CHANNEX_EXTERNAL_ID_PREFIX):
        return booking_id
    return f"{CHANNEX_EXTERNAL_ID_PREFIX}{booking_id}"


def parse_channex_booking_id(external_id: str) -> str | None:
    external_id = (external_id or "").strip()
    if not external_id.startswith(CHANNEX_EXTERNAL_ID_PREFIX):
        return None
    booking_id = external_id[len(CHANNEX_EXTERNAL_ID_PREFIX) :].strip()
    return booking_id or None


def _channex_booking_lookup_codes(reservation: Reservation) -> list[str]:
    """
    OTA codes usable for GET /bookings?filter[ota_reservation_code]=… on legacy rows.

    Legacy migrations stored internal row ids (140xxxxxx) in external_id; those are excluded.
    """
    codes: list[str] = []
    seen: set[str] = set()

    def add(code: str) -> None:
        normalized = code.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            codes.append(normalized)

    add(reservation.booking_code or "")

    external_id = (reservation.external_id or "").strip()
    if external_id.isdigit() and not external_id.startswith("140"):
        add(external_id)

    return codes


def _resolve_channex_booking_payload(
    client: ChannexClient,
    reservation: Reservation,
) -> tuple[str, dict[str, Any], str] | None:
    """
    Resolve Channex booking payload for a stay.hr reservation.

    Returns (booking_id, payload, lookup_method) where lookup_method is
    ``external_id`` or ``booking_code``.
    """
    booking_id = parse_channex_booking_id(reservation.external_id)
    if booking_id:
        try:
            payload = client.get_booking(booking_id)
        except ChannexApiError:
            return None
        return booking_id, payload, "external_id"

    lookup_codes = _channex_booking_lookup_codes(reservation)
    if not lookup_codes:
        return None

    for code in lookup_codes:
        payload = client.find_booking_by_ota_reservation_code(code)
        if payload is None:
            continue
        resolved_id = str(payload.get("id") or "").strip()
        if not resolved_id:
            attrs = payload.get("attributes")
            if isinstance(attrs, dict):
                resolved_id = str(attrs.get("id") or "").strip()
        if resolved_id:
            return resolved_id, payload, "booking_code"

    return None


def _parse_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _commission_percent(amount: Decimal | None, commission: Decimal | None) -> Decimal | None:
    if amount is None or commission is None or amount <= 0:
        return None
    return (commission / amount * Decimal("100")).quantize(Decimal("0.01"))


def _channex_payment_provider(attrs: dict[str, Any]) -> str:
    payment_collect = str(attrs.get("payment_collect") or "").strip().lower()
    ota_name = str(attrs.get("ota_name") or "").strip()
    payment_type = str(attrs.get("payment_type") or "").strip()

    if payment_collect == "ota":
        if "booking" in ota_name.lower():
            return "Payments by Booking.com"
        if ota_name:
            return f"Payments by {ota_name}"
    if payment_type:
        return payment_type.replace("_", " ").title()
    return ""


def _channex_payment_status(attrs: dict[str, Any]) -> str:
    provider = _channex_payment_provider(attrs)
    if provider:
        return f"Payment is facilitated through {provider}"
    return ""


def _channex_financial_fields(attrs: dict[str, Any]) -> dict[str, Any]:
    """Map Channex booking revision financial attrs; omit keys when source data is absent."""
    fields: dict[str, Any] = {}

    amount = _parse_decimal(attrs.get("amount"))
    commission = _parse_decimal(attrs.get("ota_commission"))
    if commission is not None:
        fields["commission_amount"] = commission
        percent = _commission_percent(amount, commission)
        if percent is not None:
            fields["commission_percent"] = percent

    payment_provider = _channex_payment_provider(attrs)
    if payment_provider:
        fields["payment_provider"] = payment_provider

    payment_status = _channex_payment_status(attrs)
    if payment_status:
        fields["payment_status"] = payment_status

    return fields


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if not value:
        return None
    return parse_date(str(value))


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    parsed = parse_datetime(str(value))
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, dt_timezone.utc)
    return parsed


def _customer_name(customer: dict[str, Any]) -> str:
    parts = [str(customer.get("name") or "").strip(), str(customer.get("surname") or "").strip()]
    full = " ".join(part for part in parts if part)
    return full or str(customer.get("mail") or "Channex guest")


def _customer_country_iso2(customer: dict[str, Any]) -> str:
    return str(customer.get("country") or "").strip().upper()[:2]


def _extract_booking_code(attrs: dict[str, Any]) -> str:
    return str(
        attrs.get("ota_reservation_code") or attrs.get("unique_id") or attrs.get("system_id") or ""
    ).strip()


def _find_existing_channex_reservation(
    tenant: Tenant,
    booking_id: str,
    booking_code: str,
) -> Reservation | None:
    if booking_id:
        found = Reservation.objects.filter(
            tenant=tenant,
            external_id=channex_external_id(booking_id),
        ).first()
        if found is not None:
            return found
    if booking_code:
        return Reservation.objects.filter(
            tenant=tenant,
            booking_code=booking_code,
        ).first()
    return None


def _map_reservation_status(channex_status: str) -> str:
    status = (channex_status or "").strip().lower()
    if status == "cancelled":
        return Reservation.Status.CANCELED
    return Reservation.Status.EXPECTED


def resolve_ingest_property(
    tenant: Tenant,
    config: ChannexRuntimeConfig,
) -> Property:
    """Property for booking ingest — production uses sync_property_slug."""
    slug = (
        config.sync_property_slug
        or config.certification_property_slug
        or certification_property_slug(tenant.slug)
    )
    try:
        return Property.objects.get(tenant=tenant, slug=slug)
    except Property.DoesNotExist as exc:
        raise ChannexBookingIngestError(
            f"Ingest property '{slug}' not found for tenant {tenant.slug}."
        ) from exc


def resolve_certification_property(
    tenant: Tenant,
    config: ChannexRuntimeConfig,
) -> Property:
    return resolve_ingest_property(tenant, config)


def _unit_for_room_link(
    tenant: Tenant,
    property: Property,
    room_link: ChannexBookingTestRoomLink,
) -> Unit | None:
    if room_link.unit_id:
        unit = Unit.objects.filter(
            tenant=tenant,
            id=room_link.unit_id,
            is_active=True,
        ).first()
        if unit is not None and unit.property_id == property.id:
            return unit
    return Unit.objects.filter(
        tenant=tenant,
        property=property,
        code=room_link.unit_code,
        is_active=True,
    ).first()


@transaction.atomic
def _upsert_reservation_from_revision(
    *,
    tenant: Tenant,
    property: Property,
    config: ChannexRuntimeConfig,
    revision: dict[str, Any],
) -> tuple[Reservation, bool]:
    attrs = revision.get("attributes") or {}
    booking_id = str(attrs.get("booking_id") or revision.get("booking_id") or "").strip()
    if not booking_id:
        raise ChannexBookingIngestError("Booking revision missing booking_id.")

    booking_code = _extract_booking_code(attrs)
    check_in = _parse_date(attrs.get("arrival_date"))
    check_out = _parse_date(attrs.get("departure_date"))

    existing_for_dates = _find_existing_channex_reservation(tenant, booking_id, booking_code)
    if not check_in or not check_out:
        if existing_for_dates is not None:
            check_in = check_in or existing_for_dates.check_in
            check_out = check_out or existing_for_dates.check_out
    if not check_in or not check_out:
        raise ChannexBookingIngestError("Booking revision missing arrival/departure dates.")

    customer = attrs.get("customer") if isinstance(attrs.get("customer"), dict) else {}
    occupancy = attrs.get("occupancy") if isinstance(attrs.get("occupancy"), dict) else {}
    nights = (check_out - check_in).days
    if nights < 0:
        nights = 0

    adults = int(occupancy.get("adults") or 0)
    child_count = int(occupancy.get("children") or 0)
    infant_count = int(occupancy.get("infants") or 0)
    occupancy_counts: dict[str, int | None] = {}
    if occupancy:
        occupancy_counts = {
            "adults_count": adults,
            "children_count": child_count,
            "infants_count": infant_count,
            "persons_count": adults + child_count,
        }

    incoming_status = _map_reservation_status(str(attrs.get("status") or ""))
    existing = _find_existing_channex_reservation(tenant, booking_id, booking_code)
    mapped_status = incoming_status
    if (
        existing is not None
        and existing.status == Reservation.Status.NO_SHOW
        and incoming_status == Reservation.Status.CANCELED
    ):
        mapped_status = Reservation.Status.NO_SHOW

    incoming_mail = str(customer.get("mail") or "").strip()
    booker_email = incoming_mail
    guest_email = incoming_mail
    if existing is not None:
        booker_email = prefer_usable_invoice_email(existing.booker_email, incoming_mail)
        primary_guest = existing.guests.filter(is_primary=True).first()
        if primary_guest is not None:
            guest_email = prefer_usable_invoice_email(primary_guest.email, incoming_mail)
        else:
            guest_email = booker_email

    defaults: dict[str, Any] = {
        "property": property,
        "check_in": check_in,
        "check_out": check_out,
        "status": mapped_status,
        "booking_status": str(attrs.get("status") or ""),
        "booker_name": _customer_name(customer),
        "booker_email": booker_email,
        "booker_phone": normalize_booker_phone(str(customer.get("phone") or "")),
        "booker_country": _customer_country_iso2(customer),
        "booker_address": str(customer.get("address") or "").strip(),
        "amount": _parse_decimal(attrs.get("amount")),
        "currency": str(attrs.get("currency") or "GBP").strip()[:3] or "GBP",
        "source": str(attrs.get("ota_name") or "Channex").strip(),
        "import_source": "channex",
        "booked_at": _parse_datetime(attrs.get("inserted_at")),
        **occupancy_counts,
        "nights_count": nights or None,
        "notes": str(attrs.get("notes") or "").strip(),
        "canceled_at": timezone.now()
        if mapped_status == Reservation.Status.CANCELED
        else None,
        "details_pending": False,
    }
    defaults.update(_channex_financial_fields(attrs))

    channex_ext_id = channex_external_id(booking_id)
    if existing is not None:
        preserved_booked_at = existing.booked_at
        for key, value in defaults.items():
            setattr(existing, key, value)
        if preserved_booked_at is not None:
            # load_future_reservations sets inserted_at to pull time, not OTA book date
            existing.booked_at = preserved_booked_at
        if existing.external_id != channex_ext_id:
            existing.external_id = channex_ext_id
        if booking_code and existing.booking_code != booking_code:
            existing.booking_code = booking_code
        existing.save()
        reservation = existing
        created = False
    else:
        create_defaults = dict(defaults)
        if booking_code:
            create_defaults["booking_code"] = booking_code
        reservation, created = Reservation.objects.update_or_create(
            tenant=tenant,
            external_id=channex_ext_id,
            defaults=create_defaults,
        )

    rooms = attrs.get("rooms") or []
    if not isinstance(rooms, list):
        rooms = []

    channex_rooms_count = sum(1 for room in rooms if isinstance(room, dict))
    preserve_units = should_preserve_units_on_channex_ingest(
        reservation=reservation,
        created=created,
        channex_rooms_count=channex_rooms_count,
        incoming_status=mapped_status,
    )

    pdf_locked = is_pdf_authoritative(reservation)
    if pdf_locked or preserve_units:
        units_count = ReservationUnit.objects.filter(
            reservation=reservation,
            unit_id__isnull=False,
        ).count()
        if preserve_units and not pdf_locked:
            logger.warning(
                "channex ingest preserved stay.hr units (revision under-reports rooms)",
                extra={
                    "reservation_id": reservation.pk,
                    "booking_code": reservation.booking_code,
                    "channex_rooms_count": channex_rooms_count,
                    "mapped_units": units_count,
                },
            )
    else:
        reservation.units.all().delete()
        units_count = 0
        for index, room in enumerate(rooms):
            if not isinstance(room, dict):
                continue
            room_type_id = str(room.get("room_type_id") or "").strip()
            room_link = config.room_link_for_channex_room_type_id(room_type_id)
            unit = _unit_for_room_link(tenant, property, room_link) if room_link else None
            if unit is None and room_link is not None:
                logger.warning(
                    "channex booking room not mapped to stay unit",
                    extra={
                        "tenant_slug": tenant.slug,
                        "property_slug": property.slug,
                        "room_type_id": room_type_id,
                        "unit_code": room_link.unit_code,
                        "booking_id": booking_id,
                    },
                )
            room_name = (
                (room_link.channex_title if room_link else "")
                or str(room.get("room_type_name") or "")
                or room_type_id
                or f"Room {index + 1}"
            )
            ReservationUnit.objects.create(
                tenant=tenant,
                reservation=reservation,
                unit=unit,
                sort_order=index,
                room_name=room_name,
                amount=_parse_decimal(room.get("amount")),
            )
            units_count += 1

        if units_count:
            reservation.units_count = units_count
            reservation.save(update_fields=["units_count", "updated_at"])

    if customer and reservation.status not in {
        Reservation.Status.CANCELED,
        Reservation.Status.NO_SHOW,
    }:
        first = str(customer.get("name") or "").strip() or "Guest"
        last = str(customer.get("surname") or "").strip()
        guest_defaults = {
            "first_name": first,
            "last_name": last,
            "name": _customer_name(customer),
            "email": guest_email,
            "phone": str(customer.get("phone") or "").strip()[:32],
            "address": str(customer.get("address") or "").strip(),
            "is_primary": True,
        }
        country = _customer_country_iso2(customer)
        if country:
            guest_defaults["nationality"] = country
            guest_defaults["document_country_iso2"] = country
        Guest.objects.update_or_create(
            tenant=tenant,
            reservation=reservation,
            is_primary=True,
            defaults=guest_defaults,
        )
        ensure_adult_guest_slots(
            tenant=tenant,
            reservation=reservation,
            adults_count=reservation.adults_count,
        )

    if reservation.status not in {Reservation.Status.CANCELED, Reservation.Status.NO_SHOW}:
        flag_channex_ingest_room_warnings(
            reservation,
            channex_rooms_count=channex_rooms_count,
            adults_count=int(reservation.adults_count or 0),
        )
        if units_count:
            flag_ingest_overbooking(reservation)
            check_channex_revision_room_mismatch(reservation, attrs)

    return reservation, created


def process_channex_booking_revision(
    integration_row: IntegrationConfig,
    revision_id: str,
    *,
    client: ChannexClient | None = None,
) -> Reservation | None:
    revision_id = revision_id.strip()
    if not revision_id:
        raise ChannexBookingIngestError("revision_id is required.")

    if ChannexBookingRevision.objects.filter(revision_id=revision_id).exists():
        existing = ChannexBookingRevision.objects.select_related("reservation").get(
            revision_id=revision_id
        )
        logger.info(
            "channex booking revision already processed",
            extra={"revision_id": revision_id, "reservation_id": existing.reservation_id},
        )
        return existing.reservation

    config = ChannexRuntimeConfig.from_integration_dict(integration_row.get_config_dict())
    tenant = integration_row.tenant
    property = resolve_ingest_property(tenant, config)

    owns_client = client is None
    if owns_client:
        client = ChannexClient(config)

    try:
        revision = client.get_booking_revision(revision_id)
        attrs = revision.get("attributes") or {}
        booking_id = str(attrs.get("booking_id") or "").strip()
        channex_status = str(attrs.get("status") or "")
        booking_code = _extract_booking_code(attrs)
        check_in = _parse_date(attrs.get("arrival_date"))
        check_out = _parse_date(attrs.get("departure_date"))
        existing = _find_existing_channex_reservation(tenant, booking_id, booking_code)

        if (
            channex_status.strip().lower() == "cancelled"
            and (not check_in or not check_out)
            and existing is None
        ):
            client.acknowledge_booking_revision(revision_id)
            ChannexBookingRevision.objects.create(
                tenant=tenant,
                revision_id=revision_id,
                booking_id=booking_id,
                reservation=None,
                channex_status=channex_status,
            )
            logger.info(
                "channex booking revision ack-only (orphan cancel)",
                extra={
                    "revision_id": revision_id,
                    "booking_id": booking_id,
                    "booking_code": booking_code,
                },
            )
            return None

        with transaction.atomic():
            reservation, created = _upsert_reservation_from_revision(
                tenant=tenant,
                property=property,
                config=config,
                revision=revision,
            )

        client.acknowledge_booking_revision(revision_id)

        ChannexBookingRevision.objects.create(
            tenant=tenant,
            revision_id=revision_id,
            booking_id=booking_id,
            reservation=reservation,
            channex_status=channex_status,
        )

        logger.info(
            "channex booking ingested and acknowledged",
            extra={
                "revision_id": revision_id,
                "booking_id": booking_id,
                "reservation_id": reservation.id,
                "reservation_created": created,
                "reservation_status": reservation.status,
                "property_slug": property.slug,
            },
        )

        from apps.integrations.channex.reservation_availability_service import (
            push_channex_inventory_after_ingest,
        )

        transaction.on_commit(
            lambda reservation_id=reservation.pk: push_channex_inventory_after_ingest(
                reservation_id
            )
        )
        from apps.integrations.channex.review_service import relink_unlinked_channex_reviews
        from apps.integrations.channex.message_service import relink_unlinked_channex_messages

        transaction.on_commit(lambda t=tenant: relink_unlinked_channex_reviews(t))
        transaction.on_commit(lambda t=tenant: relink_unlinked_channex_messages(t))
        return reservation
    finally:
        if owns_client and client is not None:
            client.close()


def process_channex_booking_webhook(
    integration_row: IntegrationConfig,
    *,
    revision_id: str,
    booking_id: str,
    event: str,
) -> Reservation | None:
    if not revision_id:
        logger.warning(
            "channex booking webhook missing revision_id",
            extra={"event": event, "booking_id": booking_id},
        )
        return None
    return process_channex_booking_revision(integration_row, revision_id)


def process_channex_booking_revisions_feed(
    integration_row: IntegrationConfig,
    *,
    client: ChannexClient | None = None,
) -> dict[str, Any]:
    """
    Process non-acknowledged revisions from Channex feed (missed webhook fallback).

    Returns ``{"ingested": [...], "ack_only": N, "errors": N}``.
    """
    config = ChannexRuntimeConfig.from_integration_dict(integration_row.get_config_dict())
    owns_client = client is None
    if owns_client:
        client = ChannexClient(config)

    ingested: list[Reservation] = []
    ack_only = 0
    errors = 0
    try:
        revision_ids = client.list_booking_revisions_feed()
        logger.info(
            "channex booking revisions feed",
            extra={"count": len(revision_ids)},
        )
        for revision_id in revision_ids:
            if ChannexBookingRevision.objects.filter(revision_id=revision_id).exists():
                logger.debug(
                    "channex feed revision already processed",
                    extra={"revision_id": revision_id},
                )
                continue
            try:
                reservation = process_channex_booking_revision(
                    integration_row,
                    revision_id,
                    client=client,
                )
            except ChannexBookingIngestError:
                logger.exception(
                    "channex feed revision ingest failed",
                    extra={"revision_id": revision_id},
                )
                errors += 1
                continue
            except ChannexApiError:
                logger.exception(
                    "channex feed revision API error",
                    extra={"revision_id": revision_id},
                )
                errors += 1
                continue
            except Exception:
                logger.exception(
                    "channex feed revision unexpected error",
                    extra={"revision_id": revision_id},
                )
                errors += 1
                continue
            if reservation is None:
                ack_only += 1
            else:
                ingested.append(reservation)
    finally:
        if owns_client and client is not None:
            client.close()

    if ack_only or errors:
        logger.info(
            "channex booking revisions feed summary",
            extra={"ingested": len(ingested), "ack_only": ack_only, "errors": errors},
        )
    return {"ingested": ingested, "ack_only": ack_only, "errors": errors}


def backfill_channex_financial_fields(
    integration_row: IntegrationConfig,
    *,
    only_missing_commission: bool = True,
    dry_run: bool = False,
    limit: int | None = None,
    client: ChannexClient | None = None,
) -> dict[str, int | list[dict[str, Any]]]:
    """
    Fetch latest Channex booking details and apply financial fields to existing reservations.

    Uses GET /bookings/:id (latest revision). Does not acknowledge revisions or re-ingest guests.
    """
    tenant = integration_row.tenant
    config = ChannexRuntimeConfig.from_integration_dict(integration_row.get_config_dict())
    owns_client = client is None
    if owns_client:
        client = ChannexClient(config)

    stats: dict[str, int | list[dict[str, Any]]] = {
        "processed": 0,
        "updated": 0,
        "skipped_has_commission": 0,
        "skipped_no_lookup_code": 0,
        "skipped_no_financial_data": 0,
        "not_found": 0,
        "errors": 0,
        "updates": [],
    }

    reservations = (
        Reservation.objects.filter(
            tenant=tenant,
            import_source="channex",
        )
        .order_by("id")
    )
    if only_missing_commission:
        reservations = reservations.filter(commission_amount__isnull=True)
    if limit is not None:
        reservations = reservations[:limit]

    try:
        for reservation in reservations:
            stats["processed"] = int(stats["processed"]) + 1

            if only_missing_commission and reservation.commission_amount is not None:
                stats["skipped_has_commission"] = int(stats["skipped_has_commission"]) + 1
                continue

            if not parse_channex_booking_id(reservation.external_id) and not _channex_booking_lookup_codes(
                reservation
            ):
                stats["skipped_no_lookup_code"] = int(stats["skipped_no_lookup_code"]) + 1
                logger.warning(
                    "channex financial backfill: no Channex UUID or booking code",
                    extra={"reservation_id": reservation.id, "external_id": reservation.external_id},
                )
                continue

            try:
                resolved = _resolve_channex_booking_payload(client, reservation)
            except ChannexApiError as exc:
                stats["errors"] = int(stats["errors"]) + 1
                logger.warning(
                    "channex financial backfill: API error",
                    extra={
                        "reservation_id": reservation.id,
                        "error": str(exc),
                    },
                )
                continue

            if resolved is None:
                stats["not_found"] = int(stats["not_found"]) + 1
                continue

            booking_id, payload, lookup_method = resolved

            attrs = payload.get("attributes")
            if not isinstance(attrs, dict):
                stats["errors"] = int(stats["errors"]) + 1
                continue

            fields = _channex_financial_fields(attrs)
            if not fields:
                stats["skipped_no_financial_data"] = int(stats["skipped_no_financial_data"]) + 1
                continue

            update_entry = {
                "reservation_id": reservation.id,
                "booking_id": booking_id,
                "booking_code": reservation.booking_code,
                "lookup_method": lookup_method,
                **fields,
            }
            updates = stats["updates"]
            assert isinstance(updates, list)
            updates.append(update_entry)

            if dry_run:
                continue

            for key, value in fields.items():
                setattr(reservation, key, value)
            reservation.save(update_fields=[*fields.keys(), "updated_at"])
            stats["updated"] = int(stats["updated"]) + 1
            logger.info(
                "channex financial backfill updated reservation",
                extra={
                    "reservation_id": reservation.id,
                    "booking_id": booking_id,
                    "fields": list(fields.keys()),
                },
            )
    finally:
        if owns_client and client is not None:
            client.close()

    return stats
