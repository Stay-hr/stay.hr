from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

ExistingReservationMode = Literal["sync", "fill_empty", "overwrite"]

import xlrd
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from apps.properties.models import Property
from apps.reservations.channel_sync import (
    IMPORT_SOURCE_BOOKING_PDF,
    IMPORT_SOURCE_BOOKING_XLS,
    IMPORT_SOURCE_BOOKING_XLS as CHANNEL_BOOKING_XLS,
    incoming_wins,
    is_pdf_authoritative,
)
from apps.integrations.evisitor.summary import evisitor_status_for_guest
from apps.reservations.guest_slots import ensure_adult_guest_slots
from apps.reservations.models import EvisitorGuestStatus
from apps.reservations.models import Guest, Reservation, ReservationUnit
from apps.reservations.reservation_units import (
    apply_unit_amounts_from_total,
    split_room_names,
    sync_reservation_units,
)
from apps.tenants.models import Tenant

LEGACY_XLS_OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
BLOCKED_BOOKING_EXPORT_EXTENSIONS = (".xlsx", ".xlsm", ".csv", ".txt", ".pdf", ".zip", ".doc", ".docx")

XLS_HEADER_ALIASES = {
    "broj rezervacije": "external_id",
    "nositelj rezervacije": "booker_name",
    "ime(na) gosta": "guest_names",
    "prijava": "check_in_date",
    "odjava": "check_out_date",
    "rezervirano": "booked_at",
    "status": "booking_status",
    "jedinice": "units_count",
    "osobe": "persons_count",
    "odrasli": "adults_count",
    "djeca": "children_count",
    "dob djece": "children_ages",
    "cijena": "price",
    "provizija %": "commission_percent",
    "iznos provizije": "commission_amount",
    "status placanja": "payment_status",
    "status plaćanja": "payment_status",
    "nacin placanja (pruzatelj usluga naplate)": "payment_provider",
    "način plaćanja (pružatelj usluga naplate)": "payment_provider",
    "napomene": "notes",
    "booker country": "booker_country",
    "svrha putovanja": "travel_purpose",
    "uredaj": "booking_device",
    "uređaj": "booking_device",
    "vrsta jedinice": "room_name",
    "trajanje (nocenja)": "nights_count",
    "trajanje (noćenja)": "nights_count",
    "datum otkazivanja": "canceled_at",
    "adresa": "booker_address",
    "broj telefona": "booker_phone",
    "book number": "external_id",
    "booking number": "external_id",
    "booked by": "booker_name",
    "guest name(s)": "guest_names",
    "guest names": "guest_names",
    "check-in": "check_in_date",
    "check-in date": "check_in_date",
    "check-out": "check_out_date",
    "check-out date": "check_out_date",
    "booked on": "booked_at",
    "rooms": "units_count",
    "persons": "persons_count",
    "adults": "adults_count",
    "children": "children_count",
    "children's age(s)": "children_ages",
    "childrens age(s)": "children_ages",
    "price": "price",
    "commission %": "commission_percent",
    "commission amount": "commission_amount",
    "payment status": "payment_status",
    "payment method (payment provider)": "payment_provider",
    "remarks": "notes",
    "travel purpose": "travel_purpose",
    "device": "booking_device",
    "unit type": "room_name",
    "duration (nights)": "nights_count",
    "cancellation date": "canceled_at",
    "address": "booker_address",
    "phone number": "booker_phone",
}


@dataclass(frozen=True)
class BookingXlsRow:
    external_id: str
    booker_name: str
    guest_names: list[str]
    check_in_date: date
    check_out_date: date
    booked_at: datetime | None
    booking_status: str
    units_count: int | None
    persons_count: int | None
    adults_count: int | None
    children_count: int | None
    children_ages: str
    total_amount: Decimal | None
    currency: str
    commission_percent: Decimal | None
    commission_amount: Decimal | None
    payment_status: str
    payment_provider: str
    notes: str
    booker_country: str
    travel_purpose: str
    booking_device: str
    room_name: str
    nights_count: int | None
    canceled_at: datetime | None
    booker_address: str
    booker_phone: str
    booker_email: str = ""
    unit_amounts: tuple[Decimal, ...] = ()


@dataclass(frozen=True)
class XlsImportResult:
    external_id: str
    created: bool
    skipped: bool = False
    updated: bool = False
    merged: bool = False
    skip_reason: str = ""
    reservation_id: int | None = None


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def _merge_empty_fields(
    instance: Any,
    updates: dict[str, Any],
    *,
    exclude: frozenset[str] | None = None,
) -> list[str]:
    """Apply only updates where the instance field is currently empty."""
    exclude = exclude or frozenset()
    changed: list[str] = []
    for field, new_value in updates.items():
        if field in exclude or _is_blank(new_value):
            continue
        if not _is_blank(getattr(instance, field)):
            continue
        setattr(instance, field, new_value)
        changed.append(field)
    return changed


def is_legacy_xls_content(content: bytes) -> bool:
    return len(content) >= 8 and content[:8] == LEGACY_XLS_OLE_SIGNATURE


def validate_booking_export_file(*, filename: str, content: bytes) -> None:
    lower = (filename or "").lower()
    if any(lower.endswith(ext) for ext in BLOCKED_BOOKING_EXPORT_EXTENSIONS):
        raise ValueError(f"Datoteka '{filename}' nije podržana (koristite Booking .xls export).")
    if not is_legacy_xls_content(content):
        raise ValueError(
            f"Datoteka '{filename}' nije stari Excel (.xls) format. "
            "Booking export mora biti .xls (Excel 97–2003), ne .xlsx."
        )


def _normalize_header(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _cell_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _parse_money(value: Any) -> tuple[Decimal | None, str]:
    raw = _cell_str(value)
    if not raw:
        return (None, "EUR")
    match = re.match(r"^([\d.,]+)\s*([A-Za-z]{3})?$", raw.replace(" ", ""))
    if not match:
        try:
            return (Decimal(raw.replace(",", ".")), "EUR")
        except InvalidOperation:
            return (None, "EUR")
    amount_raw, currency = match.group(1), (match.group(2) or "EUR")
    try:
        return (Decimal(amount_raw.replace(",", ".")), currency.upper())
    except InvalidOperation:
        return (None, currency.upper())


def _parse_decimal(value: Any) -> Decimal | None:
    raw = _cell_str(value)
    if not raw:
        return None
    try:
        return Decimal(raw.replace(",", "."))
    except InvalidOperation:
        return None


def _parse_int(value: Any) -> int | None:
    raw = _cell_str(value)
    if not raw:
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def _parse_xls_datetime(value: Any, book: xlrd.book.Book) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        try:
            dt_tuple = xlrd.xldate_as_tuple(value, book.datemode)
            dt = datetime(*dt_tuple[:6])
        except Exception:
            return None
    else:
        raw = _cell_str(value)
        if not raw:
            return None
        parsed = parse_datetime(raw)
        if parsed:
            dt = parsed
        else:
            d = parse_date(raw)
            if not d:
                return None
            dt = datetime.combine(d, datetime.min.time())
    if timezone.is_naive(dt):
        return timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _parse_xls_date(value: Any, book: xlrd.book.Book) -> date | None:
    dt = _parse_xls_datetime(value, book)
    if dt:
        return dt.date()
    raw = _cell_str(value)
    if not raw:
        return None
    return parse_date(raw[:10]) if len(raw) >= 10 else parse_date(raw)


def _split_guest_names(raw: str) -> list[str]:
    if not raw:
        return []
    text = raw.strip()
    if ";" in text:
        return [p.strip() for p in text.split(";") if p.strip()]
    if "," in text:
        parts = [p.strip() for p in text.split(",") if p.strip()]
        if len(parts) > 1 and all(len(part.split()) >= 2 for part in parts):
            return parts
    return [text]


def _parse_guest_name(full_name: str) -> tuple[str, str]:
    name = html.unescape((full_name or "").strip())
    if not name:
        return ("", "")
    if "," in name:
        last, _, first = name.partition(",")
        return (first.strip() or "-", last.strip() or "-")
    parts = [p for p in name.split() if p]
    if len(parts) == 1:
        return (parts[0], "")
    return (" ".join(parts[:-1]), parts[-1])


def _normalize_guest_name_key(name: str) -> str:
    import unicodedata

    _latin_folds = str.maketrans(
        {
            "ł": "l",
            "Ł": "L",
            "đ": "d",
            "Đ": "D",
            "ø": "o",
            "Ø": "O",
            "æ": "ae",
            "Æ": "AE",
            "ß": "ss",
        }
    )
    text = re.sub(r"\s+", " ", html.unescape(name or "").strip())
    text = text.translate(_latin_folds)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    # German digraphs: Boettcher/Böttcher, Mueller/Müller, etc.
    text = text.replace("ae", "a").replace("oe", "o").replace("ue", "u")
    return text.casefold()


def _guest_display_name(guest: Guest) -> str:
    stored = (guest.name or "").strip()
    if stored:
        return stored
    return f"{guest.first_name} {guest.last_name}".strip()


def _find_guest_by_full_name(
    reservation: Reservation,
    full_name: str,
    *,
    prefer_primary: bool = False,
) -> Guest | None:
    key = _normalize_guest_name_key(full_name)
    if not key:
        return None
    matches = [
        guest
        for guest in Guest.objects.filter(reservation=reservation)
        if _normalize_guest_name_key(_guest_display_name(guest)) == key
    ]
    if not matches:
        return None
    if prefer_primary:
        for guest in matches:
            if guest.is_primary:
                return guest
    return matches[0]


def _remove_duplicate_named_guests(
    reservation: Reservation,
    guest_names: list[str],
    *,
    keep_guest_ids: set[int],
) -> None:
    named_keys = {_normalize_guest_name_key(name) for name in guest_names if name.strip()}
    if not named_keys:
        return

    for guest in Guest.objects.filter(reservation=reservation, is_primary=False):
        if guest.pk in keep_guest_ids:
            continue
        if _normalize_guest_name_key(_guest_display_name(guest)) not in named_keys:
            continue
        if evisitor_status_for_guest(guest) != EvisitorGuestStatus.NOT_SENT:
            continue
        guest.delete()


def _operational_status_from_booking(booking_status: str) -> str:
    normalized = (booking_status or "").strip().lower()
    if normalized in {"cancelled_by_guest", "cancelled", "canceled", "cancelled_by_hotel"}:
        return Reservation.Status.CANCELED
    return Reservation.Status.EXPECTED


def _map_row_dict(raw: dict[str, Any], book: xlrd.book.Book) -> BookingXlsRow:
    external_id = _cell_str(raw.get("external_id"))
    if not external_id:
        raise ValueError("Missing booking number")
    try:
        external_id = str(int(float(external_id)))
    except (TypeError, ValueError):
        pass

    check_in = _parse_xls_date(raw.get("check_in_date"), book)
    check_out = _parse_xls_date(raw.get("check_out_date"), book)
    if not check_in or not check_out:
        raise ValueError(f"Missing check-in/out dates for booking {external_id}")

    total_amount, currency = _parse_money(raw.get("price"))

    return BookingXlsRow(
        external_id=external_id,
        booker_name=_cell_str(raw.get("booker_name")),
        guest_names=_split_guest_names(_cell_str(raw.get("guest_names"))),
        check_in_date=check_in,
        check_out_date=check_out,
        booked_at=_parse_xls_datetime(raw.get("booked_at"), book),
        booking_status=_cell_str(raw.get("booking_status")),
        units_count=_parse_int(raw.get("units_count")),
        persons_count=_parse_int(raw.get("persons_count")),
        adults_count=_parse_int(raw.get("adults_count")),
        children_count=_parse_int(raw.get("children_count")),
        children_ages=_cell_str(raw.get("children_ages")),
        total_amount=total_amount,
        currency=currency,
        commission_percent=_parse_decimal(raw.get("commission_percent")),
        commission_amount=_parse_money(raw.get("commission_amount"))[0],
        payment_status=_cell_str(raw.get("payment_status")),
        payment_provider=_cell_str(raw.get("payment_provider")),
        notes=html.unescape(_cell_str(raw.get("notes"))),
        booker_country=_cell_str(raw.get("booker_country")).upper()[:8],
        travel_purpose=_cell_str(raw.get("travel_purpose")),
        booking_device=_cell_str(raw.get("booking_device")),
        room_name=_cell_str(raw.get("room_name")) or "Unknown",
        nights_count=_parse_int(raw.get("nights_count")),
        canceled_at=_parse_xls_datetime(raw.get("canceled_at"), book),
        booker_address=_cell_str(raw.get("booker_address")),
        booker_phone=_cell_str(raw.get("booker_phone")),
    )


def parse_booking_xls_workbook(book) -> list[BookingXlsRow]:
    sheet = book.sheet_by_index(0)
    if sheet.nrows < 2:
        return []

    header_map: dict[int, str] = {}
    for col in range(sheet.ncols):
        label = _normalize_header(_cell_str(sheet.cell_value(0, col)))
        field = XLS_HEADER_ALIASES.get(label)
        if field:
            header_map[col] = field

    rows: list[BookingXlsRow] = []
    for row_idx in range(1, sheet.nrows):
        raw: dict[str, Any] = {}
        empty = True
        for col, field in header_map.items():
            value = sheet.cell_value(row_idx, col)
            if _cell_str(value):
                empty = False
            raw[field] = value
        if empty:
            continue
        rows.append(_map_row_dict(raw, book))
    return rows


def parse_booking_xls(path: str) -> list[BookingXlsRow]:
    return parse_booking_xls_workbook(xlrd.open_workbook(path))


def parse_booking_xls_bytes(content: bytes) -> list[BookingXlsRow]:
    return parse_booking_xls_workbook(xlrd.open_workbook(file_contents=content))


def _guest_updates_from_row(
    *,
    is_primary: bool,
    first_name: str,
    last_name: str,
    booker_country: str,
    booker_email: str,
) -> dict[str, Any]:
    full_name = f"{first_name} {last_name}".strip()
    updates: dict[str, Any] = {}
    if full_name:
        updates["name"] = full_name
    if is_primary and booker_email:
        updates["email"] = booker_email
    if booker_country:
        updates["nationality"] = booker_country
        updates["document_country_iso2"] = booker_country
    return updates


def _find_guest_for_fill_empty(
    reservation: Reservation,
    *,
    first_name: str,
    last_name: str,
    is_primary: bool,
    guest_names_count: int,
) -> Guest | None:
    """Match by name first; fall back to sole/primary guest when XLS name parsing differs."""
    guest = Guest.objects.filter(
        reservation=reservation,
        first_name=first_name or "-",
        last_name=last_name or "-",
    ).first()
    if guest is not None:
        return guest

    existing = Guest.objects.filter(reservation=reservation)
    if not existing.exists():
        return None
    if guest_names_count == 1 and existing.count() == 1:
        return existing.first()
    if is_primary:
        return existing.filter(is_primary=True).first()
    return None


def _preserve_existing_contact_fields(
    *,
    existing: Reservation,
    field_updates: dict[str, Any],
    booker_email: str,
) -> str:
    preserved_email = booker_email
    if (existing.booker_phone or "").strip():
        field_updates["booker_phone"] = existing.booker_phone
    if (existing.booker_email or "").strip():
        preserved_email = existing.booker_email.strip()
        field_updates["booker_email"] = preserved_email
    return preserved_email


def _sync_guests(
    *,
    tenant: Tenant,
    reservation: Reservation,
    guest_names: list[str],
    booker_country: str,
    booker_email: str,
    fill_empty_only: bool = False,
    preserve_primary_contact: bool = False,
    authoritative_pdf: bool = False,
) -> None:
    if not guest_names:
        return

    if not fill_empty_only:
        Guest.objects.filter(reservation=reservation).update(is_primary=False)

    synced_guest_ids: set[int] = set()

    for idx, full_name in enumerate(guest_names):
        first_name, last_name = _parse_guest_name(full_name)
        if not first_name and not last_name:
            continue
        is_primary = idx == 0
        guest = Guest.objects.filter(
            reservation=reservation,
            first_name=first_name or "-",
            last_name=last_name or "-",
        ).first()
        if guest is None and fill_empty_only:
            guest = _find_guest_for_fill_empty(
                reservation,
                first_name=first_name,
                last_name=last_name,
                is_primary=is_primary,
                guest_names_count=len(guest_names),
            )
        if guest is None:
            guest = _find_guest_by_full_name(
                reservation,
                full_name,
                prefer_primary=is_primary,
            )
        guest_updates = _guest_updates_from_row(
            is_primary=is_primary,
            first_name=first_name,
            last_name=last_name,
            booker_country=booker_country,
            booker_email=booker_email,
        )

        if guest is None:
            if fill_empty_only and Guest.objects.filter(reservation=reservation).exists():
                continue
            guest = Guest.objects.create(
                tenant=tenant,
                reservation=reservation,
                first_name=first_name or "-",
                last_name=last_name or "-",
                name=guest_updates.get("name") or f"{first_name} {last_name}".strip(),
                email=guest_updates.get("email", ""),
                nationality=guest_updates.get("nationality", ""),
                document_country_iso2=guest_updates.get("document_country_iso2", ""),
                is_primary=is_primary,
            )
            synced_guest_ids.add(guest.pk)
            continue

        if fill_empty_only:
            changed = _merge_empty_fields(guest, guest_updates)
            if changed:
                guest.save(update_fields=[*changed, "updated_at"])
            synced_guest_ids.add(guest.pk)
            continue

        guest.is_primary = is_primary
        guest.first_name = first_name or "-"
        guest.last_name = last_name or "-"
        guest.name = guest_updates.get("name") or guest.name
        if (
            is_primary
            and booker_email
            and not (preserve_primary_contact and (guest.email or "").strip())
        ):
            guest.email = booker_email
        if booker_country:
            if authoritative_pdf:
                guest.nationality = booker_country
                guest.document_country_iso2 = booker_country
            elif not guest.nationality:
                guest.nationality = booker_country
                guest.document_country_iso2 = booker_country
        guest.save(
            update_fields=[
                "is_primary",
                "first_name",
                "last_name",
                "name",
                "email",
                "nationality",
                "document_country_iso2",
                "updated_at",
            ]
        )
        synced_guest_ids.add(guest.pk)

    if authoritative_pdf and not fill_empty_only:
        _remove_duplicate_named_guests(
            reservation,
            guest_names,
            keep_guest_ids=synced_guest_ids,
        )


def _find_existing_reservation(*, tenant: Tenant, external_id: str) -> Reservation | None:
    if not external_id:
        return None
    existing = Reservation.objects.filter(
        tenant=tenant,
        external_id=external_id,
    ).first()
    if existing is not None:
        return existing
    return Reservation.objects.filter(
        tenant=tenant,
        booking_code=external_id,
    ).first()


def _reservation_field_updates_from_row(
    *,
    property: Property,
    row: BookingXlsRow,
    booker_email: str,
    now: datetime,
    authoritative_pdf: bool = False,
) -> dict[str, Any]:
    import_source = IMPORT_SOURCE_BOOKING_PDF if authoritative_pdf else IMPORT_SOURCE_BOOKING_XLS
    updates: dict[str, Any] = {
        "property": property,
        "booking_code": row.external_id,
        "check_in": row.check_in_date,
        "check_out": row.check_out_date,
        "status": _operational_status_from_booking(row.booking_status),
        "booker_name": row.booker_name or "Booking guest",
        "booker_email": booker_email,
        "booker_phone": row.booker_phone,
        "booker_country": row.booker_country,
        "booker_address": row.booker_address,
        "amount": row.total_amount,
        "currency": row.currency or "EUR",
        "source": "Booking.com",
        "import_source": import_source,
        "imported_at": now,
        "xls_imported_at": now,
        "booked_at": row.booked_at,
        "booking_status": row.booking_status,
        "units_count": row.units_count,
        "persons_count": row.persons_count,
        "adults_count": row.adults_count,
        # OTA booked occupancy only — never write expected_checkin_adults.
        "children_count": row.children_count,
        "children_ages": row.children_ages,
        "commission_percent": row.commission_percent,
        "commission_amount": row.commission_amount,
        "payment_status": row.payment_status,
        "payment_provider": row.payment_provider,
        "notes": row.notes,
        "travel_purpose": row.travel_purpose,
        "booking_device": row.booking_device,
        "nights_count": row.nights_count,
        "canceled_at": row.canceled_at,
        "details_pending": False,
    }
    if authoritative_pdf:
        updates["pdf_imported_at"] = now
    return updates


def _count_mapped_units(reservation: Reservation) -> int:
    return ReservationUnit.objects.filter(
        reservation=reservation,
        unit_id__isnull=False,
    ).count()


def _maybe_sync_rooms_on_fill_empty(
    *,
    tenant: Tenant,
    property: Property,
    reservation: Reservation,
    row: BookingXlsRow,
) -> bool:
    """
    When XLS shows more rooms than stay.hr has mapped, sync units and push Channex.

    Prevents channel overbooking when legacy rows only held one ReservationUnit.
    """
    if is_pdf_authoritative(reservation):
        return False
    if reservation.status in {
        Reservation.Status.CANCELED,
        Reservation.Status.NO_SHOW,
    }:
        return False

    room_segments = split_room_names(row.room_name)
    if not room_segments:
        return False

    xls_units = row.units_count or len(room_segments)
    mapped_before = _count_mapped_units(reservation)
    if len(room_segments) <= mapped_before and xls_units <= mapped_before:
        return False

    from apps.reservations.channel_availability_sync import (
        queue_sync_if_units_changed,
        reservation_unit_codes,
    )

    before_codes = reservation_unit_codes(reservation)
    if xls_units > (reservation.units_count or 0):
        reservation.units_count = xls_units
        reservation.save(update_fields=["units_count", "updated_at"])

    units = sync_reservation_units(
        tenant=tenant,
        property=property,
        reservation=reservation,
        room_name=row.room_name,
    )
    apply_unit_amounts_from_total(
        reservation=reservation,
        total_amount=row.total_amount or reservation.amount,
        units=units,
        unit_amounts=row.unit_amounts or None,
    )
    return queue_sync_if_units_changed(reservation, before_codes=before_codes)


@transaction.atomic
def upsert_reservation_from_xls_row(
    *,
    tenant: Tenant,
    property: Property,
    row: BookingXlsRow,
    skip_existing: bool = False,
    existing_mode: ExistingReservationMode | None = None,
    authoritative_pdf: bool = False,
) -> XlsImportResult:
    if existing_mode is None:
        existing_mode = "sync"

    existing = _find_existing_reservation(tenant=tenant, external_id=row.external_id)
    now = timezone.now()

    if existing is not None and is_pdf_authoritative(existing) and not authoritative_pdf:
        return XlsImportResult(
            external_id=row.external_id,
            created=False,
            skipped=True,
            updated=False,
            skip_reason="pdf_locked",
            reservation_id=existing.id,
        )

    incoming_source = (
        IMPORT_SOURCE_BOOKING_PDF if authoritative_pdf else CHANNEL_BOOKING_XLS
    )
    if (
        existing is not None
        and existing_mode in {"sync", "overwrite"}
        and not incoming_wins(
            existing,
            source=incoming_source,
            incoming_at=now,
        )
    ):
        return XlsImportResult(
            external_id=row.external_id,
            created=False,
            skipped=True,
            updated=False,
            skip_reason="stale_xls",
            reservation_id=existing.id,
        )

    if existing is not None and existing_mode == "sync":
        existing_mode = "overwrite"

    use_pdf_authority = authoritative_pdf and existing_mode == "overwrite"

    booker_email = (row.booker_email or "").strip()
    if not booker_email and row.booker_name and "@" not in row.booker_name:
        slug = re.sub(r"[^a-z0-9]+", ".", row.booker_name.lower()).strip(".")
        if slug:
            booker_email = f"{slug}@booking-import.local"

    field_updates = _reservation_field_updates_from_row(
        property=property,
        row=row,
        booker_email=booker_email,
        now=now,
        authoritative_pdf=use_pdf_authority or (existing is None and authoritative_pdf),
    )

    if existing is not None and existing_mode == "fill_empty":
        exclude = frozenset({"property"})
        if existing.status in Reservation.OPERATIONAL_STATUSES - {
            Reservation.Status.EXPECTED,
            Reservation.Status.CANCELED,
        }:
            exclude = exclude | frozenset({"status"})
        if not (existing.external_id or "").strip():
            field_updates["external_id"] = row.external_id
        changed = _merge_empty_fields(existing, field_updates, exclude=exclude)
        if changed:
            existing.save(update_fields=[*changed, "updated_at"])
        _sync_guests(
            tenant=tenant,
            reservation=existing,
            guest_names=row.guest_names or ([row.booker_name] if row.booker_name else []),
            booker_country=row.booker_country,
            booker_email=booker_email,
            fill_empty_only=True,
        )
        ensure_adult_guest_slots(
            tenant=tenant,
            reservation=existing,
            adults_count=row.adults_count or existing.adults_count,
        )
        _maybe_sync_rooms_on_fill_empty(
            tenant=tenant,
            property=property,
            reservation=existing,
            row=row,
        )
        return XlsImportResult(
            external_id=row.external_id,
            created=False,
            skipped=False,
            updated=False,
            merged=True,
            reservation_id=existing.id,
        )

    if existing is None:
        reservation = Reservation.objects.create(
            tenant=tenant,
            external_id=row.external_id,
            **field_updates,
        )
        created = True
    else:
        reservation = existing
        created = False
        if reservation.status in (
            Reservation.Status.CHECKED_IN,
            Reservation.Status.CHECKED_OUT,
        ):
            field_updates["status"] = reservation.status
        if use_pdf_authority:
            booker_email = _preserve_existing_contact_fields(
                existing=reservation,
                field_updates=field_updates,
                booker_email=booker_email,
            )
        for field, value in field_updates.items():
            setattr(reservation, field, value)
        reservation.save()

    _sync_guests(
        tenant=tenant,
        reservation=reservation,
        guest_names=row.guest_names or ([row.booker_name] if row.booker_name else []),
        booker_country=row.booker_country,
        booker_email=booker_email,
        fill_empty_only=False,
        preserve_primary_contact=use_pdf_authority and existing is not None,
        authoritative_pdf=use_pdf_authority or (existing is None and authoritative_pdf),
    )
    ensure_adult_guest_slots(
        tenant=tenant,
        reservation=reservation,
        adults_count=row.adults_count,
    )
    from apps.reservations.channel_availability_sync import (
        queue_sync_if_units_changed,
        reservation_unit_codes,
    )

    before_unit_codes = reservation_unit_codes(reservation)
    units = sync_reservation_units(
        tenant=tenant,
        property=property,
        reservation=reservation,
        room_name=row.room_name,
    )
    apply_unit_amounts_from_total(
        reservation=reservation,
        total_amount=row.total_amount,
        units=units,
        unit_amounts=row.unit_amounts or None,
    )
    queue_sync_if_units_changed(reservation, before_codes=before_unit_codes)

    return XlsImportResult(
        external_id=row.external_id,
        created=created,
        skipped=False,
        updated=not created,
        reservation_id=reservation.id,
    )


def import_booking_xls_rows(
    *,
    tenant: Tenant,
    property: Property,
    rows: list[BookingXlsRow],
    dry_run: bool = False,
    skip_existing: bool = False,
    existing_mode: ExistingReservationMode | None = None,
) -> dict[str, Any]:
    if existing_mode is None:
        existing_mode = "sync"

    stats: dict[str, Any] = {
        "created": 0,
        "updated": 0,
        "merged": 0,
        "skipped": 0,
        "errors": [],
        "rows": [],
    }

    for row in rows:
        try:
            if dry_run:
                exists = _find_existing_reservation(
                    tenant=tenant,
                    external_id=row.external_id,
                ) is not None
                if exists and existing_mode == "sync":
                    action = "updated"
                    stats["updated"] += 1
                elif exists and existing_mode == "fill_empty":
                    action = "merged"
                    stats["merged"] += 1
                elif exists:
                    action = "updated"
                    stats["updated"] += 1
                else:
                    action = "created"
                    stats["created"] += 1
                stats["rows"].append(
                    {
                        "external_id": row.external_id,
                        "action": action,
                        "check_in": row.check_in_date.isoformat(),
                        "room_name": row.room_name,
                    }
                )
                continue

            result = upsert_reservation_from_xls_row(
                tenant=tenant,
                property=property,
                row=row,
                existing_mode=existing_mode,
            )
            if result.skipped:
                stats["skipped"] += 1
            elif result.merged:
                stats["merged"] += 1
            elif result.created:
                stats["created"] += 1
            else:
                stats["updated"] += 1
            stats["rows"].append(
                {
                    "external_id": row.external_id,
                    "reservation_id": result.reservation_id,
                    "created": result.created,
                    "skipped": result.skipped,
                    "merged": result.merged,
                }
            )
        except Exception as exc:
            stats["errors"].append({"external_id": row.external_id, "error": str(exc)})

    stats["total"] = len(rows)
    return stats


def import_booking_xls_file(
    path: str,
    *,
    tenant: Tenant,
    property: Property,
    dry_run: bool = False,
    check_in_from: date | None = None,
    check_in_to: date | None = None,
    skip_existing: bool = False,
    existing_mode: ExistingReservationMode | None = None,
) -> dict[str, Any]:
    rows = parse_booking_xls(path)
    if check_in_from or check_in_to:
        filtered: list[BookingXlsRow] = []
        for row in rows:
            if check_in_from and row.check_in_date < check_in_from:
                continue
            if check_in_to and row.check_in_date > check_in_to:
                continue
            filtered.append(row)
        rows = filtered
    return import_booking_xls_rows(
        tenant=tenant,
        property=property,
        rows=rows,
        dry_run=dry_run,
        skip_existing=skip_existing,
        existing_mode=existing_mode,
    )
