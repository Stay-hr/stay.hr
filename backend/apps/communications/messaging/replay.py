"""Manual replay with lineage + required reason (ADR 0010 §4.J)."""

from __future__ import annotations

import uuid
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from apps.communications.messaging.context import TriggerContext
from apps.communications.messaging.definitions import definition_registry
from apps.communications.messaging.models import (
    MessageDispatch,
    MessageDispatchEvent,
    MessageDispatchEventType,
    MessageDispatchStatus,
    MessageReplayReason,
    MessageScheduleStrategy,
    MessageTriggerKind,
)
from apps.communications.messaging.triggers import Trigger


class ReplayError(ValueError):
    """Invalid replay request."""


@transaction.atomic
def replay_dispatch(
    parent: MessageDispatch,
    *,
    reason: str,
    due_at: datetime | None = None,
    correlation_id: uuid.UUID | None = None,
    ctx: TriggerContext | None = None,
) -> MessageDispatch:
    """Create a child dispatch linked to ``parent`` with frozen snapshots reset.

    Render/recipient snapshots are cleared so the dispatcher re-binds against
    current definition + recipient resolution at send time (lineage preserved).
    """
    # ADR §4.J: replay_reason required on MANUAL replay (all engine replays are MANUAL trigger).
    if not reason:
        raise ReplayError("replay_reason is required on MANUAL replay")
    valid_reasons = {c.value for c in MessageReplayReason}
    if reason not in valid_reasons:
        raise ReplayError(
            f"Invalid replay_reason {reason!r}; expected one of {sorted(valid_reasons)}"
        )

    now = (ctx.effective_now if ctx is not None else None) or timezone.now()
    due = due_at or now
    child_correlation = correlation_id or (
        ctx.correlation_id if ctx is not None else uuid.uuid4()
    )

    # Ensure definition still exists (fail fast).
    definition_registry.get(parent.definition_key)

    child = MessageDispatch.objects.create(
        tenant_id=parent.tenant_id,
        reservation_id=parent.reservation_id,
        definition_key=parent.definition_key,
        plan_key=parent.plan_key,
        trigger=MessageTriggerKind.MANUAL,
        correlation_id=child_correlation,
        parent_dispatch=parent,
        replay_reason=reason,
        due_at=due,
        timezone=parent.timezone,
        local_due_at=parent.local_due_at if due_at is None else due,
        expires_at=parent.expires_at,
        schedule_strategy=MessageScheduleStrategy.IMMEDIATE,
        status=MessageDispatchStatus.QUEUED,
        policy_version="",
        rendered_body="",
        rendered_subject="",
        language="",
        template_version="",
        render_context=dict(parent.render_context or {}),
        render_checksum="",
        recipient_type=parent.recipient_type,
        recipient_email=parent.recipient_email,
        recipient_phone=parent.recipient_phone,
        recipient_booking_thread_id=parent.recipient_booking_thread_id,
        fallback_used=False,
    )
    MessageDispatchEvent.objects.create(
        tenant_id=parent.tenant_id,
        dispatch=child,
        event_type=MessageDispatchEventType.REPLAYED,
        payload={
            "parent_dispatch_id": parent.pk,
            "replay_reason": reason,
            "trigger": Trigger.manual(source="replay").kind,
        },
    )
    MessageDispatchEvent.objects.create(
        tenant_id=parent.tenant_id,
        dispatch=child,
        event_type=MessageDispatchEventType.DISPATCH_CREATED,
        payload={"via": "replay", "parent_dispatch_id": parent.pk},
    )
    return child
