"""Messaging provider package (ADR 0010)."""

from apps.communications.messaging.providers.base import (
    MessageProvider,
    ProviderCapabilities,
)
from apps.communications.messaging.providers.booking import BookingProvider
from apps.communications.messaging.providers.common import (
    PROVIDER_BOOKING,
    PROVIDER_EMAIL,
    PROVIDER_WHATSAPP,
)
from apps.communications.messaging.providers.email import EmailProvider
from apps.communications.messaging.providers.factory import build_live_providers
from apps.communications.messaging.providers.registry import (
    ProviderRegistry,
    provider_registry,
)
from apps.communications.messaging.providers.stubs import (
    StubProvider,
    build_stub_providers,
)
from apps.communications.messaging.providers.whatsapp import WhatsAppProvider

__all__ = [
    "PROVIDER_BOOKING",
    "PROVIDER_EMAIL",
    "PROVIDER_WHATSAPP",
    "BookingProvider",
    "EmailProvider",
    "MessageProvider",
    "ProviderCapabilities",
    "ProviderRegistry",
    "StubProvider",
    "WhatsAppProvider",
    "build_live_providers",
    "build_stub_providers",
    "provider_registry",
]
