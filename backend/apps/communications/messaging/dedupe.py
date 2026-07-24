"""Deduplication for MessageDispatch outbox rows (ADR 0010)."""

from __future__ import annotations

from apps.communications.messaging.definitions import MessageDefinition
from apps.communications.messaging.models import (
    MessageDispatch,
    MessageDispatchStatus,
)

# Statuses that block creating another planned/queued row for the same key.
_ACTIVE_STATUSES = (
    MessageDispatchStatus.PLANNED,
    MessageDispatchStatus.QUEUED,
    MessageDispatchStatus.DISPATCHING,
    MessageDispatchStatus.DELIVERED,
)


def find_duplicate_dispatch(
    *,
    tenant_id: int,
    reservation_id: int,
    definition: MessageDefinition,
    plan_key: str = "",
) -> MessageDispatch | None:
    """Return an existing active dispatch that would collide, if any."""
    if not definition.dedupe.enabled:
        return None

    qs = MessageDispatch.objects.filter(
        tenant_id=tenant_id,
        reservation_id=reservation_id,
        definition_key=definition.key,
        status__in=_ACTIVE_STATUSES,
        archived_at__isnull=True,
    )
    if definition.dedupe.include_plan_key:
        qs = qs.filter(plan_key=plan_key or "")
    return qs.order_by("id").first()


def is_duplicate(
    *,
    tenant_id: int,
    reservation_id: int,
    definition: MessageDefinition,
    plan_key: str = "",
) -> bool:
    return (
        find_duplicate_dispatch(
            tenant_id=tenant_id,
            reservation_id=reservation_id,
            definition=definition,
            plan_key=plan_key,
        )
        is not None
    )


class RateLimiter:
    """v1 no-op stub (ADR reserved interface)."""

    def allow(self, *_args, **_kwargs) -> bool:
        return True


rate_limiter = RateLimiter()
