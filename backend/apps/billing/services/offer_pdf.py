"""Render booking offer PDF from immutable snapshot (not live fiscal settings)."""

from __future__ import annotations

import io
from decimal import Decimal
from pathlib import Path

from django.core.files.base import ContentFile
from django.template.loader import render_to_string
from xhtml2pdf import pisa

from apps.billing.models import BookingOffer
from apps.billing.services.pdf import (
    FONT_BOLD,
    FONT_REGULAR,
    _ensure_dejavu_fonts,
    _link_callback,
)


def _format_money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')):.2f}".replace(".", ",")

_OFFER_TEMPLATE = "billing/offer.html"


def _format_line_quantity(raw: str) -> str:
    value = Decimal(raw)
    if value == value.to_integral_value():
        return f"{value.quantize(Decimal('0.01')):.0f}"
    return f"{value:.2f}"


def offer_template_context(offer: BookingOffer) -> dict:
    snapshot = offer.snapshot or {}
    lines = snapshot.get("lines") or []
    seller = snapshot.get("seller") or {}
    buyer = snapshot.get("buyer") or {}
    stay = snapshot.get("stay") or {}
    issued = offer.issued_at.strftime("%d.%m.%Y")
    valid_until = ""
    if offer.valid_until:
        valid_until = offer.valid_until.strftime("%d.%m.%Y")
    elif snapshot.get("valid_until"):
        try:
            valid_until = date_from_iso(snapshot["valid_until"]).strftime("%d.%m.%Y")
        except ValueError:
            valid_until = str(snapshot["valid_until"])

    return {
        "offer": offer,
        "snapshot": snapshot,
        "seller": seller,
        "buyer": buyer,
        "stay": stay,
        "offer_number": snapshot.get("offer_number") or offer.offer_number,
        "issued_at_display": issued,
        "valid_until_display": valid_until,
        "payment_reference": snapshot.get("payment_reference") or "",
        "payment_note": snapshot.get("payment_note") or "",
        "formatted_lines": [
            {
                "description": line.get("description", ""),
                "quantity": _format_line_quantity(str(line.get("quantity", "0"))),
                "unit_price": _format_money(Decimal(str(line.get("unit_price", "0")))),
                "vat_rate": _format_money(Decimal(str(line.get("vat_rate", "0")))),
                "vat_amount": _format_money(Decimal(str(line.get("vat_amount", "0")))),
                "line_total": _format_money(Decimal(str(line.get("line_total", "0")))),
            }
            for line in lines
        ],
        "subtotal": _format_money(Decimal(str(snapshot.get("subtotal", "0")))),
        "vat_amount": _format_money(Decimal(str(snapshot.get("vat_amount", "0")))),
        "total": _format_money(Decimal(str(snapshot.get("total", "0")))),
        "currency": snapshot.get("currency") or "EUR",
        "tourist_tax_clause": (
            "Turistička pristojba ne podliježe oporezivanju sukladno čl. 33. st. 3. Zakona o PDV-u."
        ),
        "font_regular": "DejaVuSans.ttf",
        "font_bold": "DejaVuSans-Bold.ttf",
    }


def date_from_iso(value: str):
    from datetime import date

    return date.fromisoformat(value)


def render_offer_html(offer: BookingOffer) -> str:
    return render_to_string(_OFFER_TEMPLATE, offer_template_context(offer))


def render_offer_pdf(offer: BookingOffer) -> None:
    _ensure_dejavu_fonts()
    html = render_offer_html(offer)
    buffer = io.BytesIO()
    pdf = pisa.CreatePDF(
        html,
        dest=buffer,
        encoding="UTF-8",
        link_callback=_link_callback,
    )
    if pdf.err:
        raise RuntimeError("Failed to generate offer PDF.")
    safe_number = (offer.offer_number or "offer").replace("/", "-")
    filename = f"ponuda-{safe_number}.pdf"
    offer.pdf_file.save(filename, ContentFile(buffer.getvalue()), save=True)
