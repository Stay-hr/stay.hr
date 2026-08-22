"""LLM proposal parser for staff booking intake (not authoritative)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from apps.ai.provider import GuestComposeError, complete_chat_json, llm_configured, llm_model, prompt_version

logger = logging.getLogger(__name__)

PROMPT_VERSION = "booking_intake_v1"


@dataclass(frozen=True)
class BookingIntakeParseResult:
    parsed_json: dict[str, Any]
    property_slug: str
    unit_code: str
    check_in: date | None
    check_out: date | None
    amount: Decimal | None
    currency: str
    booker_name: str
    booker_phone: str
    booker_email: str
    booker_address: str
    buyer_company_name: str
    buyer_oib: str
    buyer_address: str
    invoice_email: str
    guest_first_name: str
    guest_last_name: str
    missing_fields: list[str]
    llm_model: str
    prompt_version: str


def _system_prompt() -> str:
    return (
        "You extract structured hotel booking details from staff-pasted WhatsApp or email text. "
        "Return a single JSON object only. "
        "Rules: "
        "1) amount is the guest-facing GROSS all-in stay total INCLUDING tourist tax "
        "(boravišna pristojba). Never treat nightly rate × nights as excluding tax unless stated. "
        "2) If nightly_rate and nights can be inferred, set amount = nightly_rate * nights "
        "when amount is missing. "
        "3) Dates must be ISO YYYY-MM-DD. Infer year from context today_iso / default_year. "
        "4) buyer_* fields are company billing snapshot (name, OIB 11 digits, address). "
        "5) guest_* is the natural person staying. "
        "6) Put uncertain keys in missing_fields as an array of field names. "
        "7) Do not invent availability or tenant; leave unit_code null if unknown. "
        "8) currency default EUR."
    )


def _user_prompt(
    *,
    raw_text: str,
    property_slug: str | None,
    known_unit_codes: list[str],
    today_iso: str,
    default_year: int,
) -> str:
    payload = {
        "raw_text": raw_text,
        "default_property_slug": property_slug or "",
        "known_unit_codes": known_unit_codes,
        "today_iso": today_iso,
        "default_year": default_year,
        "output_schema": {
            "property_slug": "string|null",
            "unit_code": "string|null",
            "check_in": "YYYY-MM-DD|null",
            "check_out": "YYYY-MM-DD|null",
            "nightly_rate": "string|null",
            "amount": "string|null",
            "currency": "EUR",
            "booker_name": "string|null",
            "booker_phone": "string|null",
            "booker_email": "string|null",
            "booker_address": "string|null",
            "buyer_company_name": "string|null",
            "buyer_oib": "string|null",
            "buyer_address": "string|null",
            "invoice_email": "string|null",
            "guest_first_name": "string|null",
            "guest_last_name": "string|null",
            "missing_fields": ["field_name"],
        },
    }
    return json.dumps(payload, ensure_ascii=False)


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _as_date(value: Any) -> date | None:
    text = _as_str(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _as_decimal(value: Any) -> Decimal | None:
    text = _as_str(value).replace(",", ".")
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _normalize_missing(raw: Any, required_empty: list[str]) -> list[str]:
    missing: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            name = _as_str(item)
            if name and name not in missing:
                missing.append(name)
    for name in required_empty:
        if name not in missing:
            missing.append(name)
    return missing


def parse_booking_intake_text(
    *,
    raw_text: str,
    property_slug: str | None = None,
    known_unit_codes: list[str] | None = None,
    today: date | None = None,
) -> BookingIntakeParseResult:
    """Call LLM and normalize into a draft proposal. LLM is never authoritative."""
    if not llm_configured():
        raise GuestComposeError("LLM is not configured")

    text = (raw_text or "").strip()
    if not text:
        raise GuestComposeError("raw_text is required")

    today = today or datetime.now(ZoneInfo("Europe/Zagreb")).date()
    codes = list(known_unit_codes or [])
    raw = complete_chat_json(
        _system_prompt(),
        _user_prompt(
            raw_text=text,
            property_slug=property_slug,
            known_unit_codes=codes,
            today_iso=today.isoformat(),
            default_year=today.year,
        ),
    )

    check_in = _as_date(raw.get("check_in"))
    check_out = _as_date(raw.get("check_out"))
    amount = _as_decimal(raw.get("amount"))
    nightly = _as_decimal(raw.get("nightly_rate"))
    if amount is None and nightly is not None and check_in and check_out and check_out > check_in:
        amount = (nightly * (check_out - check_in).days).quantize(Decimal("0.01"))

    currency = (_as_str(raw.get("currency")) or "EUR").upper()[:3]
    unit_code = _as_str(raw.get("unit_code")).upper()
    slug = _as_str(raw.get("property_slug")) or _as_str(property_slug)

    booker_name = _as_str(raw.get("booker_name"))
    company = _as_str(raw.get("buyer_company_name"))
    guest_first = _as_str(raw.get("guest_first_name"))
    guest_last = _as_str(raw.get("guest_last_name"))
    if not booker_name:
        if company and guest_first:
            booker_name = f"{company} / {guest_first} {guest_last}".strip()
        elif company:
            booker_name = company
        elif guest_first:
            booker_name = f"{guest_first} {guest_last}".strip()

    required_empty: list[str] = []
    if not unit_code:
        required_empty.append("unit_code")
    if not check_in:
        required_empty.append("check_in")
    if not check_out:
        required_empty.append("check_out")
    if amount is None:
        required_empty.append("amount")
    if not booker_name:
        required_empty.append("booker_name")

    return BookingIntakeParseResult(
        parsed_json=raw,
        property_slug=slug,
        unit_code=unit_code,
        check_in=check_in,
        check_out=check_out,
        amount=amount,
        currency=currency,
        booker_name=booker_name,
        booker_phone=_as_str(raw.get("booker_phone")),
        booker_email=_as_str(raw.get("booker_email")),
        booker_address=_as_str(raw.get("booker_address")),
        buyer_company_name=company,
        buyer_oib=_as_str(raw.get("buyer_oib")),
        buyer_address=_as_str(raw.get("buyer_address")),
        invoice_email=_as_str(raw.get("invoice_email")),
        guest_first_name=guest_first,
        guest_last_name=guest_last,
        missing_fields=_normalize_missing(raw.get("missing_fields"), required_empty),
        llm_model=llm_model(),
        prompt_version=prompt_version() or PROMPT_VERSION,
    )
