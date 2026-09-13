from __future__ import annotations

import base64
import binascii
import io
import re
import tempfile
from decimal import Decimal
from pathlib import Path

import qrcode
from django.core.files.base import ContentFile
from django.template.loader import render_to_string
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from xhtml2pdf import pisa

from apps.billing.models import Invoice, InvoiceLine, InvoiceReplacement, TenantFiscalSettings
from apps.billing.services.invoice_replacement import InvoiceDocumentRole
from apps.billing.services.issuer_context import (
    has_frozen_issuer_context,
    reservation_reference_for,
)
from apps.billing.services.qr import build_invoice_qr_url

_STYLE_RE = re.compile(r"<style[^>]*>(.*?)</style>", re.IGNORECASE | re.DOTALL)
_BODY_RE = re.compile(r"<body[^>]*>(.*?)</body>", re.IGNORECASE | re.DOTALL)
_DATA_IMAGE_URI_RE = re.compile(
    r"^data:image/(?P<kind>png|jpe?g|gif);base64,(?P<data>.+)$",
    re.IGNORECASE | re.DOTALL,
)

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


def _decode_data_image_uri(uri: str) -> bytes | None:
    match = _DATA_IMAGE_URI_RE.match((uri or "").strip())
    if match is None:
        return None
    payload = re.sub(r"\s+", "", match.group("data"))
    try:
        return base64.b64decode(payload, validate=True)
    except binascii.Error:
        return None


def _write_temp_image(data: bytes, temp_files: list[Path]) -> str:
    handle = tempfile.NamedTemporaryFile(
        prefix="stay-invoice-qr-",
        suffix=".png",
        delete=False,
    )
    handle.write(data)
    handle.close()
    path = Path(handle.name)
    temp_files.append(path)
    return str(path)


def _link_callback(uri: str, rel: str, temp_files: list[Path] | None = None) -> str:
    del rel
    files = temp_files if temp_files is not None else []
    decoded = _decode_data_image_uri(uri)
    if decoded is not None:
        return _write_temp_image(decoded, files)
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
    frozen = (invoice.reservation_reference or "").strip()
    if frozen:
        return frozen
    return reservation_reference_for(invoice.reservation)


def _qr_png_bytes(invoice: Invoice) -> bytes:
    url = build_invoice_qr_url(invoice)
    if not url:
        return b""
    qr = qrcode.QRCode(border=1, box_size=4)
    qr.add_data(url)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    if hasattr(image, "convert"):
        image = image.convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _qr_data_uri(invoice: Invoice) -> str:
    png = _qr_png_bytes(invoice)
    if not png:
        return ""
    encoded = base64.b64encode(png).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _issuer_document_display(invoice: Invoice, settings: TenantFiscalSettings) -> dict:
    if has_frozen_issuer_context(invoice):
        return {
            "issuer_name": invoice.issuer_name,
            "issuer_address": invoice.issuer_address,
            "issuer_oib": invoice.issuer_oib,
            "issuer_iban": invoice.issuer_iban,
            "operator_code": invoice.operator_code or invoice.issuer_oib,
            "reservation_number": invoice.reservation_reference,
        }
    return {
        "issuer_name": settings.issuer_name,
        "issuer_address": settings.issuer_address,
        "issuer_oib": settings.issuer_oib,
        "issuer_iban": settings.issuer_iban,
        "operator_code": settings.operator_code or settings.issuer_oib,
        "reservation_number": reservation_reference_for(invoice.reservation),
    }


def _replacement_document_display(invoice: Invoice) -> dict:
    storno_case = (
        InvoiceReplacement.objects.filter(storno_invoice_id=invoice.pk)
        .select_related("original_invoice")
        .first()
    )
    if storno_case is not None:
        return {
            "document_role": InvoiceDocumentRole.STORNO.value,
            "referenced_invoice_number": storno_case.original_invoice.invoice_number,
            "referenced_storno_number": "",
        }
    replacement_case = (
        InvoiceReplacement.objects.filter(replacement_invoice_id=invoice.pk)
        .select_related("original_invoice", "storno_invoice")
        .first()
    )
    if replacement_case is not None:
        storno = replacement_case.storno_invoice
        return {
            "document_role": InvoiceDocumentRole.REPLACEMENT.value,
            "referenced_invoice_number": replacement_case.original_invoice.invoice_number,
            "referenced_storno_number": storno.invoice_number if storno is not None else "",
        }
    return {
        "document_role": InvoiceDocumentRole.STANDALONE.value,
        "referenced_invoice_number": "",
        "referenced_storno_number": "",
    }


def invoice_template_context(
    invoice: Invoice,
    settings: TenantFiscalSettings,
    *,
    qr_image_src: str | None = None,
) -> dict:
    lines = list(invoice.lines.order_by("sort_order", "id"))
    issuer_display = _issuer_document_display(invoice, settings)
    qr_src = _qr_data_uri(invoice) if qr_image_src is None else qr_image_src
    return {
        "invoice": invoice,
        "settings": settings,
        **issuer_display,
        **_replacement_document_display(invoice),
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
        "jir_display": invoice.jir or "u obradi",
        "zki_display": invoice.zki,
        "qr_data_uri": qr_src,
        "tourist_tax_clause": (
            "Turistička pristojba ne podliježe oporezivanju sukladno čl. 33. st. 3. Zakona o PDV-u."
        ),
        "font_regular": "DejaVuSans.ttf",
        "font_bold": "DejaVuSans-Bold.ttf",
    }


def render_invoice_html(
    invoice: Invoice,
    settings: TenantFiscalSettings,
    *,
    qr_image_src: str | None = None,
) -> str:
    context = invoice_template_context(
        invoice,
        settings,
        qr_image_src=qr_image_src,
    )
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
    buffer = io.BytesIO()
    temp_files: list[Path] = []
    qr_image_src = ""
    png = _qr_png_bytes(invoice)
    if png:
        qr_image_src = _write_temp_image(png, temp_files)
    html = render_invoice_html(
        invoice,
        settings,
        qr_image_src=qr_image_src,
    )

    def link_callback(uri: str, rel: str) -> str:
        return _link_callback(uri, rel, temp_files=temp_files)

    try:
        pdf = pisa.CreatePDF(
            html,
            dest=buffer,
            encoding="UTF-8",
            link_callback=link_callback,
        )
        if pdf.err:
            raise RuntimeError("Failed to generate invoice PDF.")
        filename = f"invoice-{invoice.invoice_number.replace('/', '-')}.pdf"
        invoice.pdf_file.save(filename, ContentFile(buffer.getvalue()), save=True)
    finally:
        for path in temp_files:
            path.unlink(missing_ok=True)
