"""ProviderRegistry — named providers + capabilities (ADR 0010)."""

from __future__ import annotations

from typing import Iterable

from apps.communications.messaging.providers.base import MessageProvider


class ProviderRegistry:
    """Process-wide provider catalog keyed by provider name."""

    def __init__(self) -> None:
        self._by_name: dict[str, MessageProvider] = {}

    def clear(self) -> None:
        self._by_name.clear()

    def register(self, provider: MessageProvider) -> None:
        if not provider.name:
            raise ValueError("Provider name is required")
        if provider.name in self._by_name:
            raise ValueError(f"Duplicate provider name: {provider.name!r}")
        self._by_name[provider.name] = provider

    def get(self, name: str) -> MessageProvider:
        try:
            return self._by_name[name]
        except KeyError as exc:
            raise KeyError(f"Unknown provider: {name!r}") from exc

    def has(self, name: str) -> bool:
        return name in self._by_name

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_name))

    def all(self) -> tuple[MessageProvider, ...]:
        return tuple(self._by_name[n] for n in sorted(self._by_name))

    def __len__(self) -> int:
        return len(self._by_name)

    def __iter__(self) -> Iterable[MessageProvider]:
        return iter(self.all())


provider_registry = ProviderRegistry()
