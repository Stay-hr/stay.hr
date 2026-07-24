"""Frozen TriggerContext — dispatcher must not inspect the caller (ADR 0010)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from django.utils import timezone

from apps.communications.messaging.triggers import Trigger


@dataclass(frozen=True)
class TriggerContext:
    """Immutable boundary context for materialization and dispatch.

    Built at the system edge (scheduler, Celery task, replay API). The
    dispatcher operates only on this context + MessageDispatch — never on
    caller identity or request objects.
    """

    reservation_id: int
    tenant_id: int
    trigger: Trigger
    correlation_id: uuid.UUID = field(default_factory=uuid.uuid4)
    property_id: int | None = None
    now: datetime | None = None
    extras: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.now is None:
            object.__setattr__(self, "now", timezone.now())
        if not isinstance(self.extras, Mapping):
            object.__setattr__(self, "extras", dict(self.extras or {}))

    @property
    def effective_now(self) -> datetime:
        assert self.now is not None
        return self.now
