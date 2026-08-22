"""SMTP helpers for per-tenant guest email."""

from django.conf import settings


def smtp_host_for_email(email: str) -> str:
    """Return SMTP host for tenant mailbox authentication.

    Tenant From addresses stay on their own domain (e.g. room_reservations@uzorita.hr),
    but outbound mail is sent via the shared Stay mail server.
    """
    address = (email or "").strip().lower()
    if "@" not in address:
        return ""
    configured = (getattr(settings, "STAY_TENANT_SMTP_HOST", None) or "").strip()
    if configured:
        return configured
    domain = address.split("@", 1)[1]
    return f"mail.{domain}" if domain else ""


def imap_host_for_email(email: str) -> str:
    """IMAP host for tenant guest mailbox (same server as SMTP)."""
    return smtp_host_for_email(email)
