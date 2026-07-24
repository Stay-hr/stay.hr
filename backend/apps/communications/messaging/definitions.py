"""MessageDefinition + in-memory registry (ADR 0010)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Callable, Iterable

from apps.communications.messaging.models import (
    MessageRecipientType,
    MessageScheduleStrategy,
)


@dataclass(frozen=True)
class ChannelPolicy:
    """Ordered provider fallback list for a definition."""

    providers: tuple[str, ...]
    version: str = ""

    def __post_init__(self) -> None:
        if not self.providers:
            raise ValueError("ChannelPolicy.providers must be non-empty")
        if not self.version:
            object.__setattr__(
                self,
                "version",
                compute_policy_version_for_providers(self.providers),
            )


def compute_policy_version(*, definition_key: str, providers: tuple[str, ...]) -> str:
    """Frozen policy_version: hash of ordered providers + definition key."""
    payload = f"{definition_key}:{','.join(providers)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def compute_policy_version_for_providers(providers: tuple[str, ...]) -> str:
    payload = ",".join(providers)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class DedupePolicy:
    """Prevent duplicate planned/queued dispatches per key fields."""

    enabled: bool = True
    # Logical key components relative to reservation + definition.
    include_plan_key: bool = False


@dataclass(frozen=True)
class RetryPolicy:
    max_provider_attempts: int = 1
    # Reserved: backoff; v1 tries each channel_policy provider once per dispatch.
    retryable_categories: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeliveryWindow:
    """v1 stub: always allow."""

    kind: str = "always"

    def allows(self, *_args, **_kwargs) -> bool:
        return self.kind == "always"


@dataclass(frozen=True)
class MessageAttachment:
    """Reserved attachment descriptor; v1 definitions use empty tuple."""

    kind: str = ""
    ref: str = ""


RendererFn = Callable[..., tuple[str, str, dict]]
"""Returns (body, subject, render_context_updates)."""


@dataclass(frozen=True)
class MessageDefinition:
    """Canonical send definition bound at materialization / dispatch."""

    key: str
    template_version: str
    channel_policy: ChannelPolicy
    skip_rule_names: tuple[str, ...] = ()
    audience: str = MessageRecipientType.BOOKER
    attachments: tuple[MessageAttachment, ...] = ()
    dedupe: DedupePolicy = field(default_factory=DedupePolicy)
    expires_after: timedelta | None = None
    delivery_window: DeliveryWindow = field(default_factory=DeliveryWindow)
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    schedule_strategy: str = MessageScheduleStrategy.FIXED_TIME
    renderer_key: str = ""

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("MessageDefinition.key is required")
        object.__setattr__(self, "key", str(self.key))
        if not self.template_version:
            raise ValueError(
                f"MessageDefinition {self.key!r} requires template_version"
            )
        object.__setattr__(self, "template_version", str(self.template_version))
        if not self.renderer_key:
            object.__setattr__(self, "renderer_key", self.key)
        else:
            object.__setattr__(self, "renderer_key", str(self.renderer_key))
        valid_strategies = {c.value for c in MessageScheduleStrategy}
        strategy = str(self.schedule_strategy)
        if strategy not in valid_strategies:
            raise ValueError(
                f"Invalid schedule_strategy {self.schedule_strategy!r} "
                f"on definition {self.key!r}"
            )
        object.__setattr__(self, "schedule_strategy", strategy)

    def bound_policy_version(self) -> str:
        return compute_policy_version(
            definition_key=self.key,
            providers=self.channel_policy.providers,
        )


class DefinitionRegistry:
    """Process-wide MessageDefinition catalog."""

    def __init__(self) -> None:
        self._by_key: dict[str, MessageDefinition] = {}

    def clear(self) -> None:
        self._by_key.clear()

    def register(self, definition: MessageDefinition) -> None:
        if definition.key in self._by_key:
            raise ValueError(
                f"Duplicate MessageDefinition key: {definition.key!r}"
            )
        self._by_key[definition.key] = definition

    def get(self, key: str) -> MessageDefinition:
        try:
            return self._by_key[key]
        except KeyError as exc:
            raise KeyError(f"Unknown MessageDefinition: {key!r}") from exc

    def has(self, key: str) -> bool:
        return key in self._by_key

    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_key))

    def all(self) -> tuple[MessageDefinition, ...]:
        return tuple(self._by_key[k] for k in sorted(self._by_key))

    def __len__(self) -> int:
        return len(self._by_key)

    def __iter__(self) -> Iterable[MessageDefinition]:
        return iter(self.all())


definition_registry = DefinitionRegistry()
