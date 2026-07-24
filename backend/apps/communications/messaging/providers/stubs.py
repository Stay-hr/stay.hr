"""Phase-3 provider stubs — retained for tests; bootstrap uses live adapters."""

from __future__ import annotations

from apps.communications.messaging.context import TriggerContext
from apps.communications.messaging.models import MessageDispatch, MessageErrorCategory
from apps.communications.messaging.providers.base import (
    DEFAULT_PROVIDER_TIMEOUTS,
    DEFAULT_TIMEOUT_SECONDS,
    MessageProvider,
    ProviderCapabilities,
)
from apps.communications.messaging.providers.common import (
    PROVIDER_BOOKING,
    PROVIDER_EMAIL,
    PROVIDER_WHATSAPP,
)
from apps.communications.messaging.results import DeliveryResult

__all__ = [
    "PROVIDER_BOOKING",
    "PROVIDER_EMAIL",
    "PROVIDER_WHATSAPP",
    "StubProvider",
    "build_stub_providers",
]


class StubProvider(MessageProvider):
    """Test double that fails closed without touching send primitives."""

    def __init__(self, *, name: str, channel: str) -> None:
        self.name = name
        self.channel = channel
        self.timeout_seconds = float(
            DEFAULT_PROVIDER_TIMEOUTS.get(name, DEFAULT_TIMEOUT_SECONDS)
        )
        self.capabilities = ProviderCapabilities(
            channels=frozenset({channel}),
            supports_attachments=False,
            supports_templates=(channel == "whatsapp"),
        )

    def send(
        self,
        dispatch: MessageDispatch,
        ctx: TriggerContext,
    ) -> DeliveryResult:
        return DeliveryResult.fail(
            provider=self.name,
            channel=self.channel,
            error_category=MessageErrorCategory.PROVIDER,
            error_code="provider_stub",
            error_message=(
                f"Provider {self.name!r} stub is not wired; "
                "use live Booking/Email/WhatsApp adapters."
            ),
            retryable=False,
        )


def build_stub_providers() -> tuple[StubProvider, ...]:
    return (
        StubProvider(name=PROVIDER_BOOKING, channel="booking"),
        StubProvider(name=PROVIDER_EMAIL, channel="email"),
        StubProvider(name=PROVIDER_WHATSAPP, channel="whatsapp"),
    )
