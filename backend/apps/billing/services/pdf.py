from __future__ import annotations

import base64
import io
import re
from decimal import Decimal
from pathlib import Path

import qrcode
from django.core.files.base import ContentFile
from django.template.loader import render_to_string
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from xhtml2pdf import pisa

from apps.billing.models import Invoice, InvoiceLine, TenantFiscalSettings
from apps.billing.services.qr import build_invoice_qr_url

_STYLE_RE = re.compile(r"<style[^>]*>(.*?)</style>", re.IGNORECASE | re.DOTALL)
_BODY_RE = re.compile(r"<body[^>]*>(.*?)</body>", re.IGNORECASE | re.DOTALL)

FONTS_DIR = Path(__file__).resolve().parent.parent / "static" / "fonts"
FONT_REGULAR = FONTS_DIR / "DejaVuSans.ttf"
FONT_BOLD = FONTS_DIR / "DejaVuSans-Bold.ttf"
_DEJAVU_REGISTERED = False


def _ensure_dejavu_fonts() -> None:
    global _DEJAVU_REGISTERED
    if _DEJAVU_REGISTERED:
        return
    if not FONT_REGULAR.is_file() or not FONT_BOLD.is_file():
        raise RuntimeError("DejaVu Sans font files are missing from billing/static/fonts.")
    pdfmetrics.registerFont(TTFont("DejaVuSans", str(FONT_REGULAR)))
    pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", str(FONT_BOLD)))
    _DEJAVU_REGISTERED = True


def _link_callback(uri: str, rel: str) -> str:
    del rel
    name = Path(uri).name
    font_path = FONTS_DIR / name
    if font_path.is_file():
        return str(font_path)
    if Path(uri).is_file():
        return uri
    raise RuntimeError(f"Unable to resolve invoice PDF asset: {uri}")


def _format_money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')):.2f}".replace(".", ",")


def resolve_reservation_number(invoice: Invoice) -> str:
    reservation = invoice.reservation
    booking_code = (reservation.booking_code or "").strip()
    if booking_code:
        return booking_code
    external_id = (reservation.external_id or "").strip()
    if external_id:
        return external_id
    return str(reservation.pk)


def _qr_data_uri(invoice: Invoice) -> str:
    url = build_invoice_qr_url(invoice)
    if not url:
        return ""
    qr = qrcode.QRCode(border=1, box_size=4)
    qr.add_data(url)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def invoice_template_context(invoice: Invoice, settings: TenantFiscalSettings) -> dict:
    lines = list(invoice.lines.order_by("sort_order", "id"))
    return {
        "invoice": invoice,
        "settings": settings,
        "lines": lines,
        "formatted_lines": [
            {
                "description": line.description,
                "quantity": f"{line.quantity.quantize(Decimal('0.01')):.0f}"
                if line.quantity == line.quantity.to_integral_value()
                else f"{line.quantity:.2f}",
                "unit_price": _format_money(line.unit_price),
                "vat_rate": _format_money(line.vat_rate),
                "vat_amount": _format_money(line.vat_amount),
                "line_total": _format_money(line.line_total),
            }
            for line in lines
        ],
        "subtotal": _format_money(invoice.subtotal),
        "vat_amount": _format_money(invoice.vat_amount),
        "total": _format_money(invoice.total),
        "issued_at_display": invoice.issued_at.strftime("%d.%m.%Y %H:%M"),
        "reservation_number": resolve_reservation_number(invoice),
        "jir_display": invoice.jir or "u obradi",
        "zki_display": invoice.zki,
        "operator_code": settings.operator_code or settings.issuer_oib,
        "qr_data_uri": _qr_data_uri(invoice),
        "tourist_tax_clause": (
            "Turistička pristojba ne podliježe oporezivanju sukladno čl. 33. st. 3. Zakona o PDV-u."
        ),
        "font_regular": "DejaVuSans.ttf",
        "font_bold": "DejaVuSans-Bold.ttf",
    }


def render_invoice_html(invoice: Invoice, settings: TenantFiscalSettings) -> str:
    context = invoice_template_context(invoice, settings)
    return render_to_string("billing/invoice.html", context)


def split_rendered_invoice_html(html: str) -> tuple[str, str]:
    """Extract style CSS and body inner HTML from render_invoice_html() output.

    Guest portal wraps these parts; PDF keeps the full document. Legal layout
    stays in billing/invoice.html only.
    """
    styles = "\n".join(match.strip() for match in _STYLE_RE.findall(html))
    body_match = _BODY_RE.search(html)
    body_html = body_match.group(1).strip() if body_match else html
    return styles, body_html


def render_invoice_pdf(invoice: Invoice, settings: TenantFiscalSettings) -> None:
    _ensure_dejavu_fonts()
    html = render_invoice_html(invoice, settings)
    buffer = io.BytesIO()
    pdf = pisa.CreatePDF(
        html,
        dest=buffer,
        encoding="UTF-8",
        link_callback=_link_callback,
    )
    if pdf.err:
        raise RuntimeError("Failed to generate invoice PDF.")
    filename = f"invoice-{invoice.invoice_number.replace('/', '-')}.pdf"
    invoice.pdf_file.save(filename, ContentFile(buffer.getvalue()), save=True)
