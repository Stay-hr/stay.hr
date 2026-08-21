"""Canonical reception reservation create (manual / intake confirm).

All staff-facing create paths must call ``create_reception_reservation`` so
availability, amount, B2B snapshot, guest, and inventory side-effects stay one
code path. Outbound channel sync remains on Reservation post_save signals.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from django.db import transaction

from apps.properties.models import Property, Unit
from apps.reservations.availability import validate_unit_available_for_booking
from apps.reservations.models import Guest, Reservation, ReservationUnit
from apps.tenants.models import Tenant


@dataclass(frozen=True)
class ReceptionGuestInput:
    first_name: str
    last_name: str = ""


@dataclass(frozen=True)
class CreateReceptionReservationInput:
    tenant: Tenant
    property: Property
    unit: Unit
    check_in: date
    check_out: date
    booker_name: str
    booker_phone: str = ""
    booker_email: str = ""
    booker_address: str = ""
    amount: Decimal | None = None
    currency: str = "EUR"
    buyer_company_name: str = ""
    buyer_oib: str = ""
    buyer_address: str = ""
    invoice_email: str = ""
    guest: ReceptionGuestInput | None = None


def generate_booking_code(tenant: Tenant) -> str:
    for _ in range(10):
        code = secrets.token_hex(4).upper()
        if not Reservation.objects.filter(tenant=tenant, booking_code=code).exists():
            return code
    return secrets.token_hex(6).upper()


def b2b_billing_snapshot_locked(reservation: Reservation) -> bool:
    """True once a fiscal Invoice exists for the reservation."""
    from apps.billing.models import Invoice

    return Invoice.objects.filter(reservation_id=reservation.pk).exists()


def create_reception_reservation(data: CreateReceptionReservationInput) -> Reservation:
    """Create an expected manual reservation with optional amount, B2B snapshot, guest.

    Raises:
        ValueError: availability or date validation failure (caller maps to API errors).
    """
    if data.check_out <= data.check_in:
        raise ValueError("Check-out must be after check-in.")

    validate_unit_available_for_booking(
        data.tenant,
        data.unit,
        data.check_in,
        data.check_out,
    )

    nights = (data.check_out - data.check_in).days
    currency = (data.currency or "EUR").strip().upper() or "EUR"
    booking_code = generate_booking_code(data.tenant)

    with transaction.atomic():
        reservation = Reservation.objects.create(
            tenant=data.tenant,
            property=data.property,
            booking_code=booking_code,
            check_in=data.check_in,
            check_out=data.check_out,
            booker_name=data.booker_name.strip(),
            booker_phone=(data.booker_phone or "").strip(),
            booker_email=(data.booker_email or "").strip(),
            booker_address=(data.booker_address or "").strip(),
            amount=data.amount,
            currency=currency,
            buyer_company_name=(data.buyer_company_name or "").strip(),
            buyer_oib=(data.buyer_oib or "").strip(),
            buyer_address=(data.buyer_address or "").strip(),
            invoice_email=(data.invoice_email or "").strip(),
            import_source="manual",
            source="reception",
            status=Reservation.Status.EXPECTED,
            nights_count=nights if nights > 0 else None,
            adults_count=1 if data.guest is not None else None,
        )
        ReservationUnit.objects.create(
            tenant=data.tenant,
            reservation=reservation,
            unit=data.unit,
            sort_order=0,
            room_name=data.unit.name or data.unit.code,
            amount=data.amount,
        )
        if data.guest is not None:
            first = data.guest.first_name.strip()
            last = (data.guest.last_name or "").strip()
            Guest.objects.create(
                tenant=data.tenant,
                reservation=reservation,
                first_name=first,
                last_name=last,
                name=f"{first} {last}".strip() if last else first,
                is_primary=True,
            )
        return reservation


def create_reception_reservation_from_validated(
    *,
    tenant: Tenant,
    validated: dict[str, Any],
) -> Reservation:
    """Bridge from DRF validated_data (property/unit already resolved)."""
    guest_data = validated.get("guest")
    guest = None
    if guest_data:
        guest = ReceptionGuestInput(
            first_name=guest_data["first_name"],
            last_name=guest_data.get("last_name") or "",
        )
    return create_reception_reservation(
        CreateReceptionReservationInput(
            tenant=tenant,
            property=validated["property"],
            unit=validated["unit"],
            check_in=validated["check_in"],
            check_out=validated["check_out"],
            booker_name=validated["booker_name"],
            booker_phone=validated.get("booker_phone") or "",
            booker_email=validated.get("booker_email") or "",
            booker_address=validated.get("booker_address") or "",
            amount=validated.get("amount"),
            currency=validated.get("currency") or "EUR",
            buyer_company_name=validated.get("buyer_company_name") or "",
            buyer_oib=validated.get("buyer_oib") or "",
            buyer_address=validated.get("buyer_address") or "",
            invoice_email=validated.get("invoice_email") or "",
            guest=guest,
        )
    )
