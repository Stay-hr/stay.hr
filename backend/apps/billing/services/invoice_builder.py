from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from apps.billing.exceptions import FiscalConfigError, InvoiceBuildError
from apps.billing.models import InvoiceLine, TenantFiscalSettings
from apps.billing.services.country_names_hr import country_display_name_hr
from apps.billing.services.payment import build_payment_note, resolve_payment_method
from apps.reservations.models import Guest, Reservation
from apps.tourist_tax.services.calculator import (
    TouristTaxConfigError,
    TouristTaxValidationError,
    calculate_tourist_tax_for_reservation,
)


@dataclass(frozen=True)
class BuiltInvoiceLine:
    sort_order: int
    line_kind: str
    description: str
    quantity: Decimal
    unit_price: Decimal
    vat_rate: Decimal
    vat_amount: Decimal
    line_total: Decimal


@dataclass(frozen=True)
class BuiltInvoice:
    buyer_name: str
    buyer_document_number: str
    buyer_address: str
    buyer_country: str
    payment_method: str
    payment_note: str
    lines: tuple[BuiltInvoiceLine, ...]
    subtotal: Decimal
    vat_amount: Decimal
    total: Decimal
    currency: str


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _split_gross_to_net_vat(gross: Decimal, vat_rate: Decimal) -> tuple[Decimal, Decimal]:
    if gross <= 0:
        return Decimal("0.00"), Decimal("0.00")
    divisor = Decimal("1") + (vat_rate / Decimal("100"))
    net = _quantize(gross / divisor)
    vat = _quantize(gross - net)
    return net, vat


def has_b2b_buyer_snapshot(reservation: Reservation) -> bool:
    return bool((getattr(reservation, "buyer_company_name", None) or "").strip())


def resolve_buyer_name(reservation: Reservation) -> str:
    company = (getattr(reservation, "buyer_company_name", None) or "").strip()
    if company:
        return company
    primary = reservation.guests.filter(is_primary=True).first()
    if primary is not None:
        name = f"{primary.first_name} {primary.last_name}".strip() or primary.name.strip()
        if name:
            return name
    booker = (reservation.booker_name or "").strip()
    if booker:
        return booker
    raise InvoiceBuildError("Primary guest name is required for the invoice.")


def resolve_buyer_identity(reservation: Reservation) -> tuple[str, str]:
    if has_b2b_buyer_snapshot(reservation):
        document_number = (getattr(reservation, "buyer_oib", None) or "").strip()
        address = (getattr(reservation, "buyer_address", None) or "").strip()
        if not address:
            address = (reservation.booker_address or "").strip()
        return document_number, address

    primary = reservation.guests.filter(is_primary=True).first()
    document_number = ""
    address = ""
    if primary is not None:
        document_number = (primary.document_number or "").strip()
        if not document_number:
            document_number = (primary.personal_id_number or "").strip()
        address = (primary.address or "").strip()
    if not address:
        address = (reservation.booker_address or "").strip()
    return document_number, address


def resolve_buyer_country(reservation: Reservation) -> str:
    if has_b2b_buyer_snapshot(reservation):
        booker_country = country_display_name_hr(reservation.booker_country)
        if booker_country:
            return booker_country
        return country_display_name_hr("HR") or "Hrvatska"

    primary = reservation.guests.filter(is_primary=True).first()
    if primary is not None:
        nationality = country_display_name_hr(primary.nationality)
        if nationality:
            return nationality
        document_country = (primary.document_country or "").strip()
        if document_country:
            return document_country
        document_iso2 = country_display_name_hr(primary.document_country_iso2)
        if document_iso2:
            return document_iso2
    return country_display_name_hr(reservation.booker_country)


def _guest_counts_label(reservation: Reservation) -> str:
    adults = reservation.adults_count or 0
    children = reservation.children_count or 0
    if adults or children:
        return f"{adults} odraslih + {children} djece"
    guests = list(reservation.guests.all())
    return f"{len(guests)} gostiju"


def _aggregate_tourist_tax_lines(reservation: Reservation) -> tuple[BuiltInvoiceLine | None, BuiltInvoiceLine | None]:
    try:
        tax_result = calculate_tourist_tax_for_reservation(reservation)
    except (TouristTaxConfigError, TouristTaxValidationError) as exc:
        raise InvoiceBuildError(str(exc)) from exc

    adult_qty = Decimal("0")
    adult_total = Decimal("0.00")
    child_qty = Decimal("0")
    child_total = Decimal("0.00")

    for night_line in tax_result.lines:
        for guest_line in night_line.guest_lines:
            if guest_line.amount <= 0:
                child_qty += 1
                continue
            adult_qty += 1
            adult_total += guest_line.amount

    adult_line = None
    child_line = None
    if adult_qty > 0:
        unit = _quantize(adult_total / adult_qty) if adult_qty else Decimal("0.00")
        adult_line = BuiltInvoiceLine(
            sort_order=2,
            line_kind=InvoiceLine.LineKind.TOURIST_TAX_ADULT,
            description="Turistička pristojba - Odrasli",
            quantity=adult_qty,
            unit_price=unit,
            vat_rate=Decimal("0.00"),
            vat_amount=Decimal("0.00"),
            line_total=_quantize(adult_total),
        )
    if child_qty > 0:
        child_line = BuiltInvoiceLine(
            sort_order=3,
            line_kind=InvoiceLine.LineKind.TOURIST_TAX_CHILD,
            description="Turistička pristojba - Djeca",
            quantity=child_qty,
            unit_price=Decimal("0.00"),
            vat_rate=Decimal("0.00"),
            vat_amount=Decimal("0.00"),
            line_total=Decimal("0.00"),
        )
    return adult_line, child_line


def build_invoice_from_reservation(
    reservation: Reservation,
    settings: TenantFiscalSettings,
) -> BuiltInvoice:
    if reservation.amount is None:
        raise InvoiceBuildError("Reservation amount is required to issue an invoice.")
    if not settings.is_vat_registered:
        raise FiscalConfigError("Tenant is not marked as VAT registered.")

    vat_rate = settings.accommodation_vat_rate
    adult_tax_line, child_tax_line = _aggregate_tourist_tax_lines(reservation)
    tourist_tax_total = Decimal("0.00")
    if adult_tax_line is not None:
        tourist_tax_total += adult_tax_line.line_total

    accommodation_gross = _quantize(reservation.amount - tourist_tax_total)
    if accommodation_gross < 0:
        raise InvoiceBuildError("Reservation amount is lower than tourist tax total.")

    net, vat = _split_gross_to_net_vat(accommodation_gross, vat_rate)
    accommodation_line = BuiltInvoiceLine(
        sort_order=1,
        line_kind=InvoiceLine.LineKind.ACCOMMODATION,
        description=f"Noćenje ({_guest_counts_label(reservation)})",
        quantity=Decimal("1"),
        unit_price=net,
        vat_rate=vat_rate,
        vat_amount=vat,
        line_total=accommodation_gross,
    )

    lines: list[BuiltInvoiceLine] = [accommodation_line]
    if adult_tax_line is not None:
        lines.append(adult_tax_line)
    if child_tax_line is not None:
        lines.append(child_tax_line)

    subtotal = _quantize(sum(line.unit_price * line.quantity for line in lines))
    vat_amount = _quantize(sum(line.vat_amount for line in lines))
    total = _quantize(sum(line.line_total for line in lines))

    payment_method = resolve_payment_method(reservation)
    buyer_document_number, buyer_address = resolve_buyer_identity(reservation)
    buyer_country = resolve_buyer_country(reservation)
    return BuiltInvoice(
        buyer_name=resolve_buyer_name(reservation),
        buyer_document_number=buyer_document_number,
        buyer_address=buyer_address,
        buyer_country=buyer_country,
        payment_method=payment_method,
        payment_note=build_payment_note(reservation, payment_method),
        lines=tuple(lines),
        subtotal=subtotal,
        vat_amount=vat_amount,
        total=total,
        currency=reservation.currency or "EUR",
    )
