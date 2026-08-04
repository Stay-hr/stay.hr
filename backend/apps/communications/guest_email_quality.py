"""Shared email quality helpers (OTA relay vs usable recipient)."""

from __future__ import annotations

import re

# Guest-facing OTA relay / proxy mailboxes — not usable for invoices.
_OTA_RELAY_SUFFIXES: tuple[str, ...] = (
    "@guest.booking.com",
    "@m.airbnb.com",
    "@reply.airbnb.com",
    "@guest.airbnb.com",
    "@messages.airbnb.com",
    "@expediapartnercentral.com",
    "@guest.expedia.com",
)

_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
    re.UNICODE,
)


def normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


def is_ota_relay_email(email: str | None) -> bool:
    normalized = normalize_email(email)
    if not normalized or "@" not in normalized:
        return False
    return any(normalized.endswith(suffix) for suffix in _OTA_RELAY_SUFFIXES)


def is_usable_invoice_email(email: str | None) -> bool:
    """True when email is present, well-formed, and not an OTA relay."""
    normalized = normalize_email(email)
    if not normalized or "@" not in normalized:
        return False
    local, _, domain = normalized.partition("@")
    if not local or not domain or "." not in domain:
        return False
    if is_ota_relay_email(normalized):
        return False
    return True


def extract_emails_from_text(text: str) -> list[str]:
    """Return unique emails in appearance order (original casing preserved for first hit)."""
    seen: set[str] = set()
    ordered: list[str] = []
    for match in _EMAIL_RE.finditer(text or ""):
        raw = match.group(0).strip()
        key = raw.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(raw)
    return ordered


def extract_usable_invoice_emails(text: str) -> list[str]:
    return [e for e in extract_emails_from_text(text) if is_usable_invoice_email(e)]
