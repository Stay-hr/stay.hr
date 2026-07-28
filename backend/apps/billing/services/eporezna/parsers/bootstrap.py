from __future__ import annotations

from apps.billing.services.eporezna.parsers.booking_pdf import BookingPdfParser
from apps.billing.services.eporezna.parsers.registry import invoice_parser_registry


def bootstrap_invoice_parsers() -> None:
    """Register built-in parsers. Idempotent for process lifetime."""
    if "booking_pdf_v1" not in invoice_parser_registry.names():
        invoice_parser_registry.register(BookingPdfParser)


bootstrap_invoice_parsers()
