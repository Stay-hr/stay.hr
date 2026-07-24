"""Build live Booking / Email / WhatsApp providers (ADR 0010 Phase 4)."""

from __future__ import annotations

from apps.communications.messaging.providers.base import MessageProvider
from apps.communications.messaging.providers.booking import BookingProvider
from apps.communications.messaging.providers.email import EmailProvider
from apps.communications.messaging.providers.whatsapp import WhatsAppProvider


def build_live_providers() -> tuple[MessageProvider, ...]:
    return (
        BookingProvider(),
        EmailProvider(),
        WhatsAppProvider(),
    )
