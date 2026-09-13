"""Classify guest invoice requests: personal vs company, with optional LLM extract."""

from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass
from typing import Any, Literal

from apps.ai.provider import GuestComposeError, complete_chat_json, llm_configured
from apps.communications.guest_email_quality import extract_usable_invoice_emails

logger = logging.getLogger(__name__)

BuyerKind = Literal["consumer", "business", "unknown"]

_COMPANY_SIGNAL = re.compile(
    r"("
    r"\bime\s+tvrtke\b|"
    r"\btvrtk[ae]\b|"
    r"\bfirma\b|"
    r"\bcompany\b|"
    r"\bbusiness\b|"
    r"\bna\s+firmu\b|"
    r"\bra[cč]un\s+na\s+(firmu|tvrtku)\b|"
    r"\br1\b|"
    r"\boib\b|"
    r"\bpdv\b|"
    r"\bvat\b|"
    r"\bu[sš]t[.\-\s]?id\b|"
    r"\bd\.?\s*o\.?\s*o\.?\b|"
    r"\bs\.?\s*p\.?\b|"
    r"\bgmbh\b|"
    r"\bltd\b|"
    r"\bs\.?r\.?l\.?\b"
    r")",
    re.IGNORECASE | re.UNICODE,
)

_EU_VAT_PREFIXES = (
    "AT|BE|BG|CY|CZ|DE|DK|EE|EL|ES|FI|FR|HR|HU|IE|IT|LT|LU|LV|MT|NL|PL|PT|RO|SE|SI|SK|XI"
)
_VAT_PREFIXED = re.compile(
    rf"\b({_EU_VAT_PREFIXES})\s*([A-Z0-9]*\d[A-Z0-9]{{5,13}})\b",
    re.IGNORECASE,
)
_OIB_LABELED = re.compile(
    r"\boib\b[:\s]*([0-9]{11})\b",
    re.IGNORECASE,
)
_HR_OIB_BARE = re.compile(r"\b([0-9]{11})\b")

_COUNTRY_HINTS = {
    "SI": "SI",
    "SLOVEN": "SI",
    "HR": "HR",
    "CROAT": "HR",
    "DE": "DE",
    "GERMANY": "DE",
    "NJEMAČ": "DE",
    "AT": "AT",
    "IT": "IT",
}

_SYSTEM_PROMPT = """You classify a guest message about a hotel invoice.
Return a JSON object only, no markdown.
Decide buyer_kind:
- "business" if they want the invoice issued to a company / VAT / OIB / legal name
- "consumer" if they only want the personal guest invoice emailed
- "unknown" if unclear
Extract any fields you can; use empty strings when missing.
Schema:
{
  "buyer_kind": "consumer"|"business"|"unknown",
  "company_name": "",
  "tax_id": "",
  "tax_id_country": "",
  "country": "",
  "address": "",
  "city": "",
  "postal_code": "",
  "email": "",
  "excerpt": ""
}
tax_id_country and country must be ISO 3166-1 alpha-2 or empty.
excerpt is a short quote of the company request (no secrets).
Do not invent an address or tax id that is not in the message.
"""


@dataclass(frozen=True)
class InvoiceIntentClassification:
    buyer_kind: BuyerKind
    company_name: str = ""
    tax_id: str = ""
    tax_id_country: str = ""
    country: str = ""
    address: str = ""
    city: str = ""
    postal_code: str = ""
    email: str = ""
    excerpt: str = ""
    source: str = "heuristic"

    def extracted_fields(self) -> dict[str, str]:
        values = {
            "company_name": self.company_name,
            "tax_id": self.tax_id,
            "tax_id_country": self.tax_id_country,
            "country": self.country,
            "address": self.address,
            "city": self.city,
            "postal_code": self.postal_code,
            "email": self.email,
            "source_excerpt": self.excerpt,
        }
        return {key: value.strip() for key, value in values.items() if value and value.strip()}

    @property
    def is_business(self) -> bool:
        return self.buyer_kind == "business"


def _normalize_iso2(value: str) -> str:
    code = (value or "").strip().upper()
    if len(code) == 2 and code.isalpha():
        return code
    return ""


def _strip_vat_country_prefix(tax_id: str, country: str) -> tuple[str, str]:
    raw = (tax_id or "").strip()
    iso = _normalize_iso2(country)
    if len(raw) >= 3 and raw[:2].isalpha() and raw[:2].isupper():
        prefix = raw[:2].upper()
        rest = raw[2:].lstrip()
        if rest:
            return rest, iso or prefix
    return raw, iso


def _heuristic_company_name(text: str) -> str:
    labeled = re.search(
        r"ime\s+tvrtke.*?(?:je|:)\s+([^\n]+)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if labeled:
        name = labeled.group(1).strip(" .;")
        name = re.sub(r"\s+", " ", name)
        if name:
            return name[:255]
    return ""


def _heuristic_tax(text: str) -> tuple[str, str]:
    labeled_oib = _OIB_LABELED.search(text)
    if labeled_oib:
        return labeled_oib.group(1), "HR"

    vat = _VAT_PREFIXED.search(text)
    if vat:
        country = vat.group(1).upper()
        ident = vat.group(2)
        if country.isalpha():
            return ident, country

    if re.search(r"\boib\b", text, re.IGNORECASE):
        bare = _HR_OIB_BARE.search(text)
        if bare:
            return bare.group(1), "HR"
    return "", ""


def _heuristic_country(text: str, tax_country: str) -> str:
    if tax_country:
        return tax_country
    upper = text.upper()
    for needle, iso in _COUNTRY_HINTS.items():
        if needle in upper:
            return iso
    return ""


def classify_invoice_request_heuristic(text: str) -> InvoiceIntentClassification:
    body = (text or "").strip()
    emails = extract_usable_invoice_emails(body)
    email = emails[0] if len(emails) == 1 else ""
    tax_id, tax_country = _heuristic_tax(body)
    company_name = _heuristic_company_name(body)
    country = _heuristic_country(body, tax_country)
    has_signal = bool(_COMPANY_SIGNAL.search(body) or tax_id or company_name)
    excerpt = ""
    if has_signal:
        excerpt = body[:240]
    return InvoiceIntentClassification(
        buyer_kind="business" if has_signal else "consumer",
        company_name=company_name,
        tax_id=tax_id,
        tax_id_country=tax_country,
        country=country,
        email=email,
        excerpt=excerpt,
        source="heuristic",
    )


def _classification_from_llm(raw: dict[str, Any], fallback: InvoiceIntentClassification) -> InvoiceIntentClassification:
    kind_raw = str(raw.get("buyer_kind") or "").strip().lower()
    kind: BuyerKind
    if kind_raw in {"consumer", "business", "unknown"}:
        kind = kind_raw  # type: ignore[assignment]
    else:
        kind = fallback.buyer_kind

    tax_id, tax_country = _strip_vat_country_prefix(
        str(raw.get("tax_id") or fallback.tax_id),
        str(raw.get("tax_id_country") or fallback.tax_id_country),
    )
    country = _normalize_iso2(str(raw.get("country") or fallback.country)) or tax_country
    email = str(raw.get("email") or fallback.email).strip()
    if email and extract_usable_invoice_emails(email) != [email]:
        email = fallback.email

    if kind == "unknown" and fallback.is_business:
        kind = "business"

    return InvoiceIntentClassification(
        buyer_kind=kind,
        company_name=str(raw.get("company_name") or fallback.company_name).strip()[:255],
        tax_id=tax_id,
        tax_id_country=_normalize_iso2(tax_country),
        country=country,
        address=str(raw.get("address") or fallback.address).strip(),
        city=str(raw.get("city") or fallback.city).strip(),
        postal_code=str(raw.get("postal_code") or fallback.postal_code).strip(),
        email=email,
        excerpt=str(raw.get("excerpt") or fallback.excerpt).strip()[:240],
        source="llm",
    )


def _running_django_tests() -> bool:
    return "test" in sys.argv


def classify_invoice_request(text: str) -> InvoiceIntentClassification:
    heuristic = classify_invoice_request_heuristic(text)
    if not llm_configured() or _running_django_tests():
        return heuristic
    try:
        parsed = complete_chat_json(_SYSTEM_PROMPT, (text or "").strip()[:4000])
    except GuestComposeError:
        logger.info("invoice_intent_llm_failed fallback=heuristic")
        return heuristic
    except Exception:
        logger.exception("invoice_intent_llm_unexpected fallback=heuristic")
        return heuristic
    if not isinstance(parsed, dict):
        return heuristic
    return _classification_from_llm(parsed, heuristic)
