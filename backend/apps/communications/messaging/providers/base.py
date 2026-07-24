"""Provider ABC + capabilities (ADR 0010)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import FrozenSet

from apps.communications.messaging.context import TriggerContext
from apps.communications.messaging.models import MessageDispatch
from apps.communications.messaging.results import DeliveryResult

# Dispatcher-enforced defaults (seconds). Adapters may set lower HTTP timeouts.
DEFAULT_PROVIDER_TIMEOUTS: dict[str, float] = {
    "booking": 15.0,
    "email": 30.0,
    "whatsapp": 20.0,
}
DEFAULT_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class ProviderCapabilities:
    """Declared provider capabilities for health / validation."""

    channels: FrozenSet[str]
    supports_attachments: bool = False
    supports_templates: bool = False

    def __post_init__(self) -> None:
        if not self.channels:
            raise ValueError("ProviderCapabilities.channels must be non-empty")


class MessageProvider(ABC):
    """Adapter over an existing send primitive (Phase 4 wires real sends).

    Contract:
    - ``send`` MUST return a ``DeliveryResult`` (never bare bool / None).
    - Prefer returning ``DeliveryResult.fail(...)`` over raising; the dispatcher
      still converts unexpected exceptions into a failed DeliveryResult.
    - ``timeout_seconds`` is enforced by the dispatcher around ``send``.
    - Never mutate ``MessageDispatch`` status, attempts, or dispatch events
      from the adapter — only return ``DeliveryResult``; the Dispatcher is the
      sole outbox state machine (ADR 0010 §2a). Timeline rows owned by the
      channel (e.g. ``GuestOutboundMessage``) may still be created by send
      primitives the adapter wraps.
    """

    name: str
    channel: str
    capabilities: ProviderCapabilities
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @abstractmethod
    def send(
        self,
        dispatch: MessageDispatch,
        ctx: TriggerContext,
    ) -> DeliveryResult:
        """Send using the frozen render snapshot on ``dispatch``.

        Read snapshots from ``dispatch``; do not update dispatch status here.
        """
