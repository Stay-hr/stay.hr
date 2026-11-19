from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import fitz
from django.utils import timezone as django_timezone

from apps.reservations.booking_xls_import import BookingXlsRow, _normalize_guest_name_key

_BOOKING_NUMBER_RE = re.compile(r"Booking number:\s*(\d+)", re.IGNORECASE)
_GUEST_NAME_RE = re.compile(r"Guest name:\s*(.+?)(?:\s*\n|$)", re.IGNORECASE)
_GUEST_EMAIL_RE = re.compile(r"([\w.+-]+@guest\.booking\.com)", re.IGNORECASE)
_CHECK_IN_RE = re.compile(
    r"Check-in\s*\n\s*([A-Za-z]{3},\s+[A-Za-z]{3}\s+\d{1,2},\s+\d{4})",
    re.IGNORECASE,
)
_CHECK_OUT_RE = re.compile(
    r"Check-out\s*\n\s*([A-Za-z]{3},\s+[A-Za-z]{3}\s+\d{1,2},\s+\d{4})",
    re.IGNORECASE,
)
_EURO_PREFIX = r"[€\u00b7·]\s*"
_TOTAL_PRICE_RE = re.compile(rf"Total price\s*\n\s*{_EURO_PREFIX}([\d.,]+)", re.IGNORECASE)
_TOTAL_ROOM_PRICE_RE = re.compile(rf"Total room price\s*\n\s*{_EURO_PREFIX}([\d.,]+)", re.IGNORECASE)
_ADULTS_RE = re.compile(r"(\d+)\s*adults?", re.IGNORECASE)
_TOTAL_GUESTS_RE = re.compile(r"Total guests:\s*\n\s*(\d+)(?:\s*adults?)?", re.IGNORECASE)
_TOTAL_UNITS_RE = re.compile(r"Total units\s*\n\s*(\d+)", re.IGNORECASE)
_ROOM_LINE_RE = re.compile(
    r"^(.+Room[^\n]*\([^)]+\)[^\n]*)$",
    re.MULTILINE | re.IGNORECASE,
)
_ROOM_BLOCK_GUEST_RE = re.compile(
    r"Guest Name\s*\n\s*(.+?)(?:\s*\n|$)",
    re.IGNORECASE,
)
_ROOM_BOOKED_OCCUPANCY_RE = re.compile(
    r"Booked occupancy\s*\n\s*(\d+)\s*adults?",
    re.IGNORECASE,
)
_ROOM_LINE_PRICE_RE = re.compile(rf"{_EURO_PREFIX}([\d.,]+)")
_ISO2_AFTER_NAME_RE = re.compile(
    r"Guest name:\s*.+?\n\s*([a-z]{2})\s*\n",
    re.IGNORECASE | re.DOTALL,
)
_RECEIVED_RE = re.compile(
    r"Received\s*\n\s*([A-Za-z]{3},\s+[A-Za-z]{3}\s+\d{1,2},\s+\d{4})",
    re.IGNORECASE,
)
_CANCELED_BY_GUEST_DATE_RE = re.compile(
    r"Canceled by guest\s+([A-Za-z]{3},\s+[A-Za-z]{3}\s+\d{1,2},\s+\d{4})",
    re.IGNORECASE,
)
_BOOKING_DATE_FMT = "%a, %b %d, %Y"


@dataclass(frozen=True)
class PdfRoomBlock:
    room_name: str
    guest_name: str
    room_price: Decimal | None
    adults_count: int | None


def extract_pdf_text(content: bytes) -> str:
    if not content:
        raise ValueError("PDF datoteka je prazna.")
    doc = fitz.open(stream=content, filetype="pdf")
    try:
        return "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()


def _require_match(pattern: re.Pattern[str], text: str, label: str) -> str:
    match = pattern.search(text)
    if not match:
        raise ValueError(f"PDF potvrda nema polje: {label}")
    return match.group(1).strip()


def _parse_booking_date(raw: str) -> date:
    try:
        return datetime.strptime(raw.strip(), _BOOKING_DATE_FMT).date()
    except ValueError as exc:
        raise ValueError(f"Neispravan datum u PDF-u: {raw!r}") from exc


def _parse_money(raw: str) -> Decimal | None:
    cleaned = (raw or "").strip().replace(",", ".")
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _parse_booking_status(text: str) -> str:
    if re.search(r"Canceled\s+This\s+Booking", text, re.IGNORECASE):
        return "cancelled_by_guest"
    if re.search(r"Canceled by guest", text, re.IGNORECASE):
        return "cancelled_by_guest"
    return "ok"


def _aware_datetime(dt: datetime) -> datetime:
    if django_timezone.is_naive(dt):
        return django_timezone.make_aware(dt, django_timezone.get_current_timezone())
    return dt


def _parse_canceled_at(text: str) -> datetime | None:
    match = _CANCELED_BY_GUEST_DATE_RE.search(text)
    if not match:
        return None
    return _aware_datetime(
        datetime.combine(_parse_booking_date(match.group(1)), datetime.min.time())
    )


def _clean_room_name(raw: str) -> str:
    return re.sub(r"\s*Canceled by guest\s*$", "", raw, flags=re.IGNORECASE).strip()


def _parse_room_block(block_text: str, *, room_name: str) -> PdfRoomBlock:
    guest_match = _ROOM_BLOCK_GUEST_RE.search(block_text)
    guest_name = re.sub(r"\s+", " ", guest_match.group(1)).strip() if guest_match else ""

    price_match = _TOTAL_ROOM_PRICE_RE.search(block_text)
    room_price = _parse_money(price_match.group(1)) if price_match else None
    if room_price is None:
        for line_match in _ROOM_LINE_PRICE_RE.finditer(block_text):
            candidate = _parse_money(line_match.group(1))
            if candidate is not None:
                room_price = candidate
                break

    adults_match = _ROOM_BOOKED_OCCUPANCY_RE.search(block_text)
    adults_count = int(adults_match.group(1)) if adults_match else None

    return PdfRoomBlock(
        room_name=_clean_room_name(room_name),
        guest_name=guest_name,
        room_price=room_price,
        adults_count=adults_count,
    )


def _parse_room_blocks(text: str) -> list[PdfRoomBlock]:
    matches = list(_ROOM_LINE_RE.finditer(text))
    if not matches:
        return []

    blocks: list[PdfRoomBlock] = []
    for idx, match in enumerate(matches):
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        block_text = text[start:end]
        blocks.append(_parse_room_block(block_text, room_name=match.group(1).strip()))
    return blocks


def _parse_room_name(text: str, room_blocks: list[PdfRoomBlock]) -> str:
    if room_blocks:
        return ", ".join(block.room_name for block in room_blocks if block.room_name)
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if "(" in stripped and ")" in stripped and "Room" in stripped:
            return _clean_room_name(stripped)
    return ""


def _parse_units_count(text: str, room_blocks: list[PdfRoomBlock]) -> int:
    match = _TOTAL_UNITS_RE.search(text)
    if match:
        return max(int(match.group(1)), 1)
    if room_blocks:
        return len(room_blocks)
    return 1


def _is_truncated_guest_name(candidate_key: str, existing_key: str) -> bool:
    """Room-block names are sometimes clipped; skip if a prefix of a longer name."""
    if not candidate_key or not existing_key or candidate_key == existing_key:
        return False
    if len(candidate_key) >= len(existing_key):
        return False
    if not existing_key.startswith(candidate_key):
        return False
    return existing_key[len(candidate_key)] in " \t"


def _drop_truncated_guest_names(names: list[str]) -> list[str]:
    if len(names) <= 1:
        return names
    keys = [_normalize_guest_name_key(name) for name in names]
    keep = [True] * len(names)
    for i, key_i in enumerate(keys):
        if not key_i:
            keep[i] = False
            continue
        for j, key_j in enumerate(keys):
            if i == j or not key_j:
                continue
            if _is_truncated_guest_name(key_i, key_j):
                keep[i] = False
                break
            if _is_truncated_guest_name(key_j, key_i):
                keep[j] = False
    return [name for name, ok in zip(names, keep) if ok]


def _parse_all_guest_names(booker_name: str, room_blocks: list[PdfRoomBlock]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()

    def add_name(raw: str) -> None:
        cleaned = re.sub(r"\s+", " ", (raw or "").strip())
        if not cleaned:
            return
        key = _normalize_guest_name_key(cleaned)
        if key in seen:
            return
        for existing in names:
            if _is_truncated_guest_name(key, _normalize_guest_name_key(existing)):
                return
            if _is_truncated_guest_name(_normalize_guest_name_key(existing), key):
                seen.discard(_normalize_guest_name_key(existing))
                names[:] = [n for n in names if _normalize_guest_name_key(n) != _normalize_guest_name_key(existing)]
                break
        seen.add(key)
        names.append(cleaned)

    add_name(booker_name)
    for block in room_blocks:
        add_name(block.guest_name)
    return _drop_truncated_guest_names(names)


def _parse_adults_count(text: str, room_blocks: list[PdfRoomBlock]) -> int | None:
    match = _TOTAL_GUESTS_RE.search(text)
    if match:
        return int(match.group(1))
    if room_blocks:
        total = sum(block.adults_count or 0 for block in room_blocks)
        if total > 0:
            return total
    match = _ADULTS_RE.search(text)
    if match:
        return int(match.group(1))
    return None


def _parse_booker_country(text: str) -> str:
    match = _ISO2_AFTER_NAME_RE.search(text)
    if match:
        return match.group(1).upper()
    return ""


def _parse_total_amount(text: str) -> Decimal | None:
    total_match = _TOTAL_PRICE_RE.search(text)
    total = _parse_money(total_match.group(1)) if total_match else None
    if total is not None and total > 0:
        return total
    room_match = _TOTAL_ROOM_PRICE_RE.search(text)
    if room_match:
        return _parse_money(room_match.group(1))
    return total


def _parse_unit_amounts(room_blocks: list[PdfRoomBlock]) -> tuple[Decimal, ...]:
    amounts: list[Decimal] = []
    for block in room_blocks:
        if block.room_price is not None:
            amounts.append(block.room_price)
    if len(amounts) == len(room_blocks) and amounts:
        return tuple(amounts)
    return ()


# --- Croatian extranet "Detalji rezervacije" print-out -------------------------------------
# Same Booking.com data, different layout: "Label:\nvalue" pairs, Croatian month
# abbreviations ("5. stu. 2026.") and "1.234,56 €" money.

_HR_BOOKING_NUMBER_RE = re.compile(r"Broj rezervacije:\s*(\d+)", re.IGNORECASE)
_HR_GUEST_BLOCK_RE = re.compile(
    r"Ime gosta:\s*\n(?P<name>[^\n]+)\n\s*(?P<country>[A-Za-z]{2})\s*\n",
    re.IGNORECASE,
)
_HR_DATE_RE = r"([^\n\d,]+,\s*\d{1,2}\.\s*[^\s\d.]+\.?\s*\d{4}\.?)"
_HR_CHECK_IN_RE = re.compile(rf"Prijava:\s*\n\s*{_HR_DATE_RE}", re.IGNORECASE)
_HR_CHECK_OUT_RE = re.compile(rf"Odjava:\s*\n\s*{_HR_DATE_RE}", re.IGNORECASE)
_HR_RECEIVED_RE = re.compile(rf"Primljeno:\s*\n\s*{_HR_DATE_RE}", re.IGNORECASE)
_HR_MONEY = r"([\d.]+,\d{2}|\d+)\s*€"
_HR_TOTAL_PRICE_RE = re.compile(rf"Ukupna cijena:\s*\n\s*{_HR_MONEY}", re.IGNORECASE)
_HR_COMMISSION_RE = re.compile(rf"Provizija i naknade:\s*\n\s*{_HR_MONEY}", re.IGNORECASE)
_HR_TOTAL_GUESTS_RE = re.compile(r"Ukupan broj gostiju:\s*\n\s*(\d+)", re.IGNORECASE)
_HR_TOTAL_UNITS_RE = re.compile(r"Ukupan broj jedinica:\s*\n\s*(\d+)", re.IGNORECASE)
_HR_PHONE_RE = re.compile(r"(\+\d[\d ]{6,}\d)")
# "<room name> (<property> - R1)\n<price> €" — one block per booked unit.
_HR_ROOM_RE = re.compile(
    rf"^([^\n]+\([^)\n]+\))\s*\n\s*{_HR_MONEY}",
    re.MULTILINE,
)
_HR_MONTHS = {
    "sij": 1, "velj": 2, "ožu": 3, "tra": 4, "svi": 5, "lip": 6,
    "srp": 7, "kol": 8, "ruj": 9, "lis": 10, "stu": 11, "pro": 12,
}
_HR_DATE_PARTS_RE = re.compile(r"(\d{1,2})\.\s*([^\s\d.]+)\.?\s*(\d{4})")


def _is_hr_extranet_text(text: str) -> bool:
    return bool(_HR_BOOKING_NUMBER_RE.search(text))


def _parse_hr_date(raw: str) -> date:
    match = _HR_DATE_PARTS_RE.search(raw or "")
    month = _HR_MONTHS.get(match.group(2).lower()) if match else None
    if not match or month is None:
        raise ValueError(f"Neispravan datum u PDF-u: {raw!r}")
    return date(int(match.group(3)), month, int(match.group(1)))


def _parse_hr_money(raw: str) -> Decimal | None:
    cleaned = (raw or "").strip().replace(".", "").replace(",", ".")
    try:
        return Decimal(cleaned) if cleaned else None
    except InvalidOperation:
        return None


def parse_booking_extranet_hr_text(text: str) -> BookingXlsRow:
    external_id = _require_match(_HR_BOOKING_NUMBER_RE, text, "Broj rezervacije")
    guest_match = _HR_GUEST_BLOCK_RE.search(text)
    if not guest_match:
        raise ValueError("PDF potvrda nema polje: Ime gosta")
    booker_name = re.sub(r"\s+", " ", guest_match.group("name")).strip()
    booker_country = guest_match.group("country").upper()
    guest_tail = text[guest_match.end(): guest_match.end() + 200]
    email_match = _GUEST_EMAIL_RE.search(guest_tail)
    phone_match = _HR_PHONE_RE.search(guest_tail)

    check_in = _parse_hr_date(_require_match(_HR_CHECK_IN_RE, text, "Prijava"))
    check_out = _parse_hr_date(_require_match(_HR_CHECK_OUT_RE, text, "Odjava"))
    total_amount = _parse_hr_money(_require_match(_HR_TOTAL_PRICE_RE, text, "Ukupna cijena"))

    commission_match = _HR_COMMISSION_RE.search(text)
    commission_amount = _parse_hr_money(commission_match.group(1)) if commission_match else None
    commission_percent = None
    if commission_amount is not None and total_amount:
        commission_percent = (commission_amount / total_amount * 100).quantize(Decimal("0.01"))

    room_matches = list(_HR_ROOM_RE.finditer(text))
    room_names = [_clean_room_name(m.group(1)) for m in room_matches]
    prices = [_parse_hr_money(m.group(2)) for m in room_matches]
    unit_amounts = tuple(p for p in prices if p is not None)
    if len(unit_amounts) != len(room_matches):
        unit_amounts = ()

    units_match = _HR_TOTAL_UNITS_RE.search(text)
    units_count = max(int(units_match.group(1)), 1) if units_match else max(len(room_matches), 1)
    guests_match = _HR_TOTAL_GUESTS_RE.search(text)
    adults_count = int(guests_match.group(1)) if guests_match else None

    booked_at = None
    received_match = _HR_RECEIVED_RE.search(text)
    if received_match:
        booked_at = _aware_datetime(
            datetime.combine(_parse_hr_date(received_match.group(1)), datetime.min.time())
        )

    return BookingXlsRow(
        external_id=external_id,
        booker_name=booker_name,
        guest_names=[booker_name],
        check_in_date=check_in,
        check_out_date=check_out,
        booked_at=booked_at,
        booking_status="ok",
        units_count=units_count,
        persons_count=adults_count,
        adults_count=adults_count,
        children_count=0,
        children_ages="",
        total_amount=total_amount,
        currency="EUR",
        commission_percent=commission_percent,
        commission_amount=commission_amount,
        payment_status="",
        payment_provider="Payments by Booking.com",
        notes="",
        booker_country=booker_country,
        travel_purpose="",
        booking_device="",
        room_name=", ".join(room_names),
        nights_count=(check_out - check_in).days if check_out > check_in else None,
        canceled_at=None,
        booker_address="",
        booker_phone=phone_match.group(1).strip() if phone_match else "",
        booker_email=email_match.group(1).strip() if email_match else "",
        unit_amounts=unit_amounts,
    )


def parse_booking_pdf_text(text: str) -> BookingXlsRow:
    if not text.strip():
        raise ValueError("PDF datoteka je prazna ili nije čitljiva.")
    if _is_hr_extranet_text(text):
        return parse_booking_extranet_hr_text(text)

    external_id = _require_match(_BOOKING_NUMBER_RE, text, "Booking number")
    booker_name = _require_match(_GUEST_NAME_RE, text, "Guest name")
    booker_name = re.sub(r"\s+", " ", booker_name).strip()

    email_match = _GUEST_EMAIL_RE.search(text)
    booker_email = email_match.group(1).strip() if email_match else ""

    check_in = _parse_booking_date(_require_match(_CHECK_IN_RE, text, "Check-in"))
    check_out = _parse_booking_date(_require_match(_CHECK_OUT_RE, text, "Check-out"))

    booking_status = _parse_booking_status(text)
    room_blocks = _parse_room_blocks(text)
    total_amount = _parse_total_amount(text)
    adults_count = _parse_adults_count(text, room_blocks)
    room_name = _parse_room_name(text, room_blocks)
    booker_country = _parse_booker_country(text)
    guest_names = _parse_all_guest_names(booker_name, room_blocks)
    units_count = _parse_units_count(text, room_blocks)
    unit_amounts = _parse_unit_amounts(room_blocks)

    booked_at = None
    received_match = _RECEIVED_RE.search(text)
    if received_match:
        booked_at = _aware_datetime(
            datetime.combine(
                _parse_booking_date(received_match.group(1)),
                datetime.min.time(),
            )
        )

    canceled_at = _parse_canceled_at(text) if booking_status == "cancelled_by_guest" else None
    nights_count = (check_out - check_in).days if check_out > check_in else None

    return BookingXlsRow(
        external_id=external_id,
        booker_name=booker_name,
        guest_names=guest_names,
        check_in_date=check_in,
        check_out_date=check_out,
        booked_at=booked_at,
        booking_status=booking_status,
        units_count=units_count,
        persons_count=adults_count,
        adults_count=adults_count,
        children_count=0,
        children_ages="",
        total_amount=total_amount,
        currency="EUR",
        commission_percent=None,
        commission_amount=None,
        payment_status="",
        payment_provider="Payments by Booking.com",
        notes="",
        booker_country=booker_country,
        travel_purpose="",
        booking_device="",
        room_name=room_name,
        nights_count=nights_count,
        canceled_at=canceled_at,
        booker_address="",
        booker_phone="",
        booker_email=booker_email,
        unit_amounts=unit_amounts,
    )


def parse_booking_pdf(content: bytes) -> BookingXlsRow:
    return parse_booking_pdf_text(extract_pdf_text(content))
