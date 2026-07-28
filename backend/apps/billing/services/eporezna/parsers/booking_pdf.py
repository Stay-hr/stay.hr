from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO

import fitz

from apps.billing.models import ForeignServiceInvoice
from apps.billing.services.eporezna.dto import ParsedForeignServiceInvoice
from apps.billing.services.eporezna.errors import ParseError
from apps.billing.services.eporezna.parsers.base import ForeignServiceInvoiceParser

_VAT_ID_RE = re.compile(
    r"(?:NIF-IVA|VAT\s*ID|VAT\s*number|PDV\s*ID)\s*:\s*([A-Z]{2})\s*([A-Z0-9]+)",
    re.IGNORECASE,
)
_PERIOD_RE = re.compile(
    r"(?:Periodo|Period|Razdoblje)\s*:\s*"
    r"(\d{2}[/.-]\d{2}[/.-]\d{4})\s*[-–]\s*(\d{2}[/.-]\d{2}[/.-]\d{4})",
    re.IGNORECASE,
)
_INVOICE_NUMBER_RE = re.compile(
    r"(?:N[uú]mero\s+de\s+factura|Invoice\s+number|Broj\s+ra[cč]una)\s*:\s*(\S+)",
    re.IGNORECASE,
)
_INVOICE_DATE_RE = re.compile(
    r"(?:Fecha|Date|Datum)\s*:\s*(\d{2}[/.-]\d{2}[/.-]\d{4})",
    re.IGNORECASE,
)
_TOTAL_RE = re.compile(
    r"(?:Importe\s+total\s+pendiente|Total\s+amount\s+due|Ukupan\s+iznos)"
    r"[^\d]*EUR\s*([\d.,]+)",
    re.IGNORECASE,
)
_SUPPLIER_RE = re.compile(r"Booking\.com\s+B\.V\.", re.IGNORECASE)


def extract_pdf_text(raw: bytes) -> str:
    doc = fitz.open(stream=BytesIO(raw), filetype="pdf")
    try:
        parts: list[str] = []
        for page in doc:
            parts.append(page.get_text("text") or "")
        return "\n".join(parts)
    finally:
        doc.close()


def _parse_date(value: str) -> date:
    for fmt in ("%d/%m/%Y", "%d.%m.%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ParseError(f"Unrecognized date format: {value!r}")


def _parse_amount(value: str) -> Decimal:
    normalized = value.strip().replace(" ", "")
    if "," in normalized and "." in normalized:
        if normalized.rfind(",") > normalized.rfind("."):
            normalized = normalized.replace(".", "").replace(",", ".")
        else:
            normalized = normalized.replace(",", "")
    elif "," in normalized:
        normalized = normalized.replace(",", ".")
    try:
        amount = Decimal(normalized)
    except InvalidOperation as exc:
        raise ParseError(f"Invalid amount: {value!r}") from exc
    return amount.quantize(Decimal("0.01"))


class BookingPdfParser(ForeignServiceInvoiceParser):
    """Booking.com B.V. commission / payment-service PDF (EU reverse charge)."""

    name = "booking_pdf_v1"

    @classmethod
    def can_parse(cls, *, filename: str, raw: bytes, text: str | None) -> bool:
        sample = text if text is not None else extract_pdf_text(raw)
        if not _SUPPLIER_RE.search(sample):
            return False
        return bool(_VAT_ID_RE.search(sample) or "NIF-IVA" in sample.upper())

    def parse(self, raw: bytes) -> ParsedForeignServiceInvoice:
        text = extract_pdf_text(raw)
        if not _SUPPLIER_RE.search(text):
            raise ParseError("Not a Booking.com B.V. invoice PDF")

        vat_m = _VAT_ID_RE.search(text)
        if not vat_m:
            raise ParseError("Missing supplier VAT ID (NIF-IVA)")
        country = vat_m.group(1).upper()
        vat_id = vat_m.group(2).upper()

        period_m = _PERIOD_RE.search(text)
        if not period_m:
            raise ParseError("Missing invoice period (Periodo)")
        period_from = _parse_date(period_m.group(1))
        period_to = _parse_date(period_m.group(2))
        tax_period = f"{period_from.year:04d}-{period_from.month:02d}"

        number_m = _INVOICE_NUMBER_RE.search(text)
        if not number_m:
            raise ParseError("Missing invoice number")
        invoice_number = number_m.group(1).strip()

        date_m = _INVOICE_DATE_RE.search(text)
        if not date_m:
            raise ParseError("Missing invoice date")
        invoice_date = _parse_date(date_m.group(1))

        total_m = _TOTAL_RE.search(text)
        if not total_m:
            raise ParseError("Missing total amount (Importe total pendiente)")
        taxable_amount = _parse_amount(total_m.group(1))

        return ParsedForeignServiceInvoice(
            provider=ForeignServiceInvoice.Provider.BOOKING,
            supplier_name="Booking.com B.V.",
            supplier_country=country,
            supplier_vat_id=vat_id,
            invoice_number=invoice_number,
            invoice_date=invoice_date,
            tax_period=tax_period,
            period_from=period_from,
            period_to=period_to,
            taxable_amount=taxable_amount,
            currency="EUR",
            raw_fields={
                "parser": self.name,
                "vat_match": vat_m.group(0),
                "period_match": period_m.group(0),
                "total_match": total_m.group(0),
            },
        )
