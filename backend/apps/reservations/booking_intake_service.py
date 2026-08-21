"""Booking intake draft lifecycle: parse → draft → confirming → confirmed."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.db.models import Q

from apps.properties.models import Property, Unit
from apps.reservations.booking_intake_models import BookingIntakeDraft
from apps.reservations.booking_intake_parse import parse_booking_intake_text
from apps.reservations.create_reception_reservation import (
    CreateReceptionReservationInput,
    ReceptionGuestInput,
    create_reception_reservation,
)
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant


class BookingIntakeError(Exception):
    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class ConfirmPayload:
    property_slug: str
    unit_id: int
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
    guest_first_name: str = ""
    guest_last_name: str = ""


def _unit_codes_for_tenant(tenant: Tenant, property_slug: str | None) -> list[str]:
    qs = Unit.objects.for_tenant(tenant).filter(is_active=True)
    if property_slug:
        qs = qs.filter(property__slug=property_slug)
    return list(qs.order_by("code").values_list("code", flat=True))


def resolve_unit(
    tenant: Tenant,
    *,
    property_slug: str,
    unit_id: int | None = None,
    unit_code: str = "",
) -> tuple[Property, Unit]:
    try:
        prop = Property.objects.get(tenant=tenant, slug=property_slug)
    except Property.DoesNotExist as exc:
        raise BookingIntakeError("property_not_found", "Property not found.") from exc

    unit = None
    if unit_id is not None:
        unit = (
            Unit.objects.for_tenant(tenant)
            .filter(pk=unit_id, property=prop, is_active=True)
            .first()
        )
    elif unit_code:
        unit = (
            Unit.objects.for_tenant(tenant)
            .filter(property=prop, is_active=True)
            .filter(Q(code__iexact=unit_code.strip()))
            .first()
        )
    if unit is None:
        raise BookingIntakeError("unit_not_found", "Unit not found for this property.")
    return prop, unit


def create_draft_from_parse(
    *,
    tenant: Tenant,
    raw_text: str,
    property_slug: str | None = None,
    created_by=None,
) -> BookingIntakeDraft:
    codes = _unit_codes_for_tenant(tenant, property_slug)
    parsed = parse_booking_intake_text(
        raw_text=raw_text,
        property_slug=property_slug,
        known_unit_codes=codes,
    )

    unit_id = None
    slug = parsed.property_slug or (property_slug or "")
    if slug and parsed.unit_code:
        try:
            _, unit = resolve_unit(
                tenant,
                property_slug=slug,
                unit_code=parsed.unit_code,
            )
            unit_id = unit.pk
        except BookingIntakeError:
            unit_id = None

    return BookingIntakeDraft.objects.create(
        tenant=tenant,
        status=BookingIntakeDraft.Status.DRAFT,
        raw_text=raw_text.strip(),
        parsed_json=parsed.parsed_json,
        missing_fields=parsed.missing_fields,
        property_slug=slug,
        unit_id=unit_id,
        unit_code=parsed.unit_code,
        check_in=parsed.check_in,
        check_out=parsed.check_out,
        amount=parsed.amount,
        currency=parsed.currency,
        booker_name=parsed.booker_name,
        booker_phone=parsed.booker_phone,
        booker_email=parsed.booker_email,
        booker_address=parsed.booker_address,
        buyer_company_name=parsed.buyer_company_name,
        buyer_oib=parsed.buyer_oib,
        buyer_address=parsed.buyer_address,
        invoice_email=parsed.invoice_email,
        guest_first_name=parsed.guest_first_name,
        guest_last_name=parsed.guest_last_name,
        created_by=created_by if getattr(created_by, "is_authenticated", False) else None,
        llm_model=parsed.llm_model,
        prompt_version=parsed.prompt_version,
    )


def apply_confirm_fields(draft: BookingIntakeDraft, payload: ConfirmPayload) -> None:
    draft.property_slug = payload.property_slug
    draft.unit_id = payload.unit_id
    draft.check_in = payload.check_in
    draft.check_out = payload.check_out
    draft.amount = payload.amount
    draft.currency = payload.currency
    draft.booker_name = payload.booker_name
    draft.booker_phone = payload.booker_phone
    draft.booker_email = payload.booker_email
    draft.booker_address = payload.booker_address
    draft.buyer_company_name = payload.buyer_company_name
    draft.buyer_oib = payload.buyer_oib
    draft.buyer_address = payload.buyer_address
    draft.invoice_email = payload.invoice_email
    draft.guest_first_name = payload.guest_first_name
    draft.guest_last_name = payload.guest_last_name


def confirm_draft(
    *,
    tenant: Tenant,
    draft_id: int,
    payload: ConfirmPayload,
) -> tuple[BookingIntakeDraft, Reservation]:
    """Atomically confirm a draft. Idempotent if already confirmed."""
    with transaction.atomic():
        draft = (
            BookingIntakeDraft.objects.select_for_update()
            .filter(tenant=tenant, pk=draft_id)
            .first()
        )
        if draft is None:
            raise BookingIntakeError("draft_not_found", "Draft not found.")

        if draft.status == BookingIntakeDraft.Status.DISCARDED:
            raise BookingIntakeError("draft_discarded", "Draft was discarded.")

        if draft.status == BookingIntakeDraft.Status.CONFIRMED:
            if draft.confirmed_reservation_id:
                reservation = Reservation.objects.filter(
                    tenant=tenant, pk=draft.confirmed_reservation_id
                ).first()
                if reservation is not None:
                    return draft, reservation
            raise BookingIntakeError(
                "draft_confirmed_missing_reservation",
                "Draft is confirmed but reservation is missing.",
            )

        if draft.status not in {
            BookingIntakeDraft.Status.DRAFT,
            BookingIntakeDraft.Status.CONFIRMING,
        }:
            raise BookingIntakeError("draft_invalid_status", f"Cannot confirm status={draft.status}.")

        # Idempotent retry: confirming with reservation already attached.
        if (
            draft.status == BookingIntakeDraft.Status.CONFIRMING
            and draft.confirmed_reservation_id
        ):
            reservation = Reservation.objects.filter(
                tenant=tenant, pk=draft.confirmed_reservation_id
            ).first()
            if reservation is not None:
                draft.status = BookingIntakeDraft.Status.CONFIRMED
                draft.save(update_fields=["status", "updated_at"])
                return draft, reservation

        apply_confirm_fields(draft, payload)
        draft.status = BookingIntakeDraft.Status.CONFIRMING
        draft.save()

        prop, unit = resolve_unit(
            tenant,
            property_slug=payload.property_slug,
            unit_id=payload.unit_id,
        )
        draft.unit_code = unit.code or draft.unit_code

        guest = None
        if payload.guest_first_name.strip():
            guest = ReceptionGuestInput(
                first_name=payload.guest_first_name,
                last_name=payload.guest_last_name,
            )

        try:
            reservation = create_reception_reservation(
                CreateReceptionReservationInput(
                    tenant=tenant,
                    property=prop,
                    unit=unit,
                    check_in=payload.check_in,
                    check_out=payload.check_out,
                    booker_name=payload.booker_name,
                    booker_phone=payload.booker_phone,
                    booker_email=payload.booker_email,
                    booker_address=payload.booker_address,
                    amount=payload.amount,
                    currency=payload.currency,
                    buyer_company_name=payload.buyer_company_name,
                    buyer_oib=payload.buyer_oib,
                    buyer_address=payload.buyer_address,
                    invoice_email=payload.invoice_email,
                    guest=guest,
                )
            )
        except ValueError as exc:
            draft.status = BookingIntakeDraft.Status.DRAFT
            draft.save(update_fields=["status", "updated_at"])
            raise BookingIntakeError("create_failed", str(exc)) from exc

        draft.confirmed_reservation = reservation
        draft.status = BookingIntakeDraft.Status.CONFIRMED
        draft.unit_id = unit.pk
        draft.save()
        return draft, reservation


def serialize_draft(draft: BookingIntakeDraft) -> dict[str, Any]:
    return {
        "id": draft.pk,
        "status": draft.status,
        "raw_text": draft.raw_text,
        "parsed_json": draft.parsed_json,
        "missing_fields": draft.missing_fields,
        "property_slug": draft.property_slug,
        "unit_id": draft.unit_id,
        "unit_code": draft.unit_code,
        "check_in": draft.check_in.isoformat() if draft.check_in else None,
        "check_out": draft.check_out.isoformat() if draft.check_out else None,
        "amount": str(draft.amount) if draft.amount is not None else None,
        "currency": draft.currency,
        "booker_name": draft.booker_name,
        "booker_phone": draft.booker_phone,
        "booker_email": draft.booker_email,
        "booker_address": draft.booker_address,
        "buyer_company_name": draft.buyer_company_name,
        "buyer_oib": draft.buyer_oib,
        "buyer_address": draft.buyer_address,
        "invoice_email": draft.invoice_email,
        "guest_first_name": draft.guest_first_name,
        "guest_last_name": draft.guest_last_name,
        "confirmed_reservation_id": draft.confirmed_reservation_id,
        "llm_model": draft.llm_model,
        "prompt_version": draft.prompt_version,
        "created_at": draft.created_at.isoformat() if draft.created_at else None,
    }
