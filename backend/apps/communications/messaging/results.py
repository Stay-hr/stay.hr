"""DeliveryResult — provider outcome + attempt telemetry (ADR 0010 §4.F)."""

from __future__ import annotations

from dataclasses import dataclass

from apps.communications.messaging.models import MessageErrorCategory


@dataclass(frozen=True)
class DeliveryResult:
    """Outcome of one provider send attempt.

    ``duration_ms`` may be omitted by the provider; the dispatcher fills
    wall-clock duration when missing.
    """

    success: bool
    provider: str
    channel: str
    provider_message_id: str = ""
    error_category: str = ""
    error_code: str = ""
    error_message: str = ""
    retryable: bool = False
    duration_ms: int | None = None
    outbound_message_id: int | None = None

    def __post_init__(self) -> None:
        if self.error_category:
            valid = {c.value for c in MessageErrorCategory}
            if self.error_category not in valid:
                raise ValueError(
                    f"Invalid error_category {self.error_category!r}; "
                    f"expected one of {sorted(valid)}"
                )

    @classmethod
    def ok(
        cls,
        *,
        provider: str,
        channel: str,
        provider_message_id: str = "",
        duration_ms: int | None = None,
        outbound_message_id: int | None = None,
    ) -> DeliveryResult:
        return cls(
            success=True,
            provider=provider,
            channel=channel,
            provider_message_id=provider_message_id,
            duration_ms=duration_ms,
            outbound_message_id=outbound_message_id,
        )

    @classmethod
    def fail(
        cls,
        *,
        provider: str,
        channel: str,
        error_category: str = MessageErrorCategory.UNKNOWN,
        error_code: str = "",
        error_message: str = "",
        retryable: bool = False,
        duration_ms: int | None = None,
        provider_message_id: str = "",
        outbound_message_id: int | None = None,
    ) -> DeliveryResult:
        return cls(
            success=False,
            provider=provider,
            channel=channel,
            error_category=error_category or MessageErrorCategory.UNKNOWN,
            error_code=error_code,
            error_message=error_message,
            retryable=retryable,
            duration_ms=duration_ms,
            provider_message_id=provider_message_id,
            outbound_message_id=outbound_message_id,
        )
