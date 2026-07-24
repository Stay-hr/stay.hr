"""Trigger kinds for the Messaging Orchestration Engine (ADR 0010)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from apps.communications.messaging.models import MessageTriggerKind


@dataclass(frozen=True)
class Trigger:
    """v1: TIME, CRON, MANUAL. Reserved kinds appear as domain events land."""

    kind: str
    source: str = ""
    metadata: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        valid = {c.value for c in MessageTriggerKind}
        if self.kind not in valid:
            raise ValueError(
                f"Unsupported trigger kind {self.kind!r}; "
                f"v1 allows {sorted(valid)}"
            )
        if self.metadata is None:
            object.__setattr__(self, "metadata", {})

    @classmethod
    def time(cls, *, source: str = "", **metadata: Any) -> Trigger:
        return cls(kind=MessageTriggerKind.TIME, source=source, metadata=metadata)

    @classmethod
    def cron(cls, *, source: str = "", **metadata: Any) -> Trigger:
        return cls(kind=MessageTriggerKind.CRON, source=source, metadata=metadata)

    @classmethod
    def manual(cls, *, source: str = "", **metadata: Any) -> Trigger:
        return cls(kind=MessageTriggerKind.MANUAL, source=source, metadata=metadata)
