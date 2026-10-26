from __future__ import annotations

from decimal import Decimal
from urllib.parse import quote

from apps.billing.models import Invoice
from apps.billing.services.fisk1.timing import issued_at_for_f1


def build_invoice_qr_url(invoice: Invoice) -> str:
    if not invoice.jir:
        return ""
    # Same wall clock CIS stored in DatVrijeme. strftime on the UTC instant
    # would open Porezna two hours early while Zagreb is on summer time.
    datv = issued_at_for_f1(invoice).strftime("%d.%m.%Y %H:%M:%S")
    amount = f"{invoice.total.quantize(Decimal('0.01')):.2f}".replace(".", ",")
    return (
        "https://porezna.gov.hr/rn?"
        f"jir={quote(invoice.jir)}&datv={quote(datv)}&izn={quote(amount)}"
    )
