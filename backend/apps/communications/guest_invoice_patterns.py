"""Regex helpers for guest invoice / receipt requests."""

from __future__ import annotations

import re

_INVOICE_INTENT = re.compile(
    r"("
    r"\binvoice\b|"
    r"\binvoices\b|"
    r"\breceipt\b|"
    r"\bfiscal\s+receipt\b|"
    r"\bbill\b|"
    r"\bfacture\b|"
    r"\bfactura\b|"
    r"\brechnung\b|"
    r"\bra[cč]un\b|"
    r"can\s+i\s+get\s+(an\s+)?invoice|"
    r"please\s+send\s+(an\s+)?invoice|"
    r"need\s+(an\s+)?invoice|"
    r"send\s+(me\s+)?(an\s+)?invoice|"
    r"invoice\s+(by|via|to)\s+e-?mail|"
    r"ra[cč]un\s+molim|"
    r"molim\s+(vas\s+)?ra[cč]un|"
    r"pošaljite\s+(mi\s+)?ra[cč]un|"
    r"poslati\s+ra[cč]un|"
    r"envoyer\s+(une\s+)?facture|"
    r"facture\s+(par\s+)?e-?mail|"
    r"rechnung\s+(per\s+)?e-?mail|"
    r"bitte\s+(eine\s+)?rechnung"
    r")",
    re.IGNORECASE | re.UNICODE,
)


def guest_message_requests_invoice(text: str) -> bool:
    return bool(_INVOICE_INTENT.search((text or "").strip()))
