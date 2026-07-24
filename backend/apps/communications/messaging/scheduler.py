"""Outbox claim (SKIP LOCKED) + TIME materialization + expire (ADR 0010 Phase 5).

TIME materialization reads Property → Tenant → Platform schedule settings
(days_before / send_time / schedule_strategy) and creates MessageDispatch rows
with frozen timezone snapshots.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Iterable

from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from apps.communications.messaging import metrics
from apps.communications.messaging.context import TriggerContext
from apps.communications.messaging.dedupe import find_duplicate_dispatch
from apps.communications.messaging.definitions import (
    MessageDefinition,
    definition_registry,
)
from apps.communications.messaging.models import (
    MessageDispatch,
    MessageDispatchEvent,
    MessageDispatchEventType,
    MessageDispatchStatus,
    MessageRecipientType,
    MessageTriggerKind,
)
from apps.communications.messaging.plans import (
    PLAN_WELCOME,
    ReminderPlan,
    plan_registry,
)
from apps.communications.messaging.schedule_settings import (
    ComputedDue,
    ResolvedSchedule,
    compute_due,
    property_timezone_name,
    resolve_schedule_for_plan,
)
from apps.communications.messaging.triggers import Trigger
from apps.properties.models import Property
from apps.reservations.models import Reservation

logger = logging.getLogger(__name__)

_CLAIMABLE = (
    MessageDispatchStatus.PLANNED,
    MessageDispatchStatus.QUEUED,
)

# How far ahead to materialize planned TIME dispatches.
DEFAULT_MATERIALIZE_HORIZON_DAYS = 14
# Catch-up window for missed FIXED_TIME sends still worth planning.
DEFAULT_MATERIALIZE_LOOKBACK_DAYS = 1


@transaction.atomic
def claim_due_dispatches(
    *,
    limit: int = 50,
    now: datetime | None = None,
    tenant_id: int | None = None,
    property_id: int | None = None,
) -> list[MessageDispatch]:
    """Claim due outbox rows with ``SELECT … FOR UPDATE SKIP LOCKED``.

    Transitions ``planned`` / ``queued`` → ``dispatching``. Concurrent workers
    skip locked rows so each dispatch is processed once.
    """
    if limit < 1:
        return []
    clock = now or timezone.now()
    qs = (
        MessageDispatch.objects.select_for_update(skip_locked=True)
        .filter(
            status__in=_CLAIMABLE,
            due_at__lte=clock,
            archived_at__isnull=True,
        )
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=clock))
        .order_by("due_at", "id")
    )
    if tenant_id is not None:
        qs = qs.filter(tenant_id=tenant_id)
    if property_id is not None:
        qs = qs.filter(reservation__property_id=property_id)

    claimed = list(qs[:limit])
    if not claimed:
        return []

    for dispatch in claimed:
        dispatch.status = MessageDispatchStatus.DISPATCHING
        dispatch.save(update_fields=["status", "updated_at"])

    metrics.incr("messaging_dispatch_claimed", value=len(claimed))
    return claimed


@transaction.atomic
def expire_overdue_dispatches(
    *,
    now: datetime | None = None,
    limit: int = 200,
    tenant_id: int | None = None,
) -> int:
    """Mark planned/queued rows past ``expires_at`` as cancelled."""
    clock = now or timezone.now()
    qs = (
        MessageDispatch.objects.select_for_update(skip_locked=True)
        .filter(
            status__in=_CLAIMABLE,
            expires_at__isnull=False,
            expires_at__lte=clock,
            archived_at__isnull=True,
        )
        .order_by("expires_at", "id")
    )
    if tenant_id is not None:
        qs = qs.filter(tenant_id=tenant_id)

    rows = list(qs[:limit])
    for dispatch in rows:
        dispatch.status = MessageDispatchStatus.CANCELLED
        dispatch.save(update_fields=["status", "updated_at"])
        MessageDispatchEvent.objects.create(
            tenant_id=dispatch.tenant_id,
            dispatch=dispatch,
            event_type=MessageDispatchEventType.CANCELLED,
            payload={"reason": "expired"},
        )
    if rows:
        metrics.incr("messaging_dispatch_expired", value=len(rows))
    return len(rows)


def _recipient_snapshot(reservation: Reservation) -> dict[str, str]:
    email = (reservation.booker_email or "").strip()
    phone = (reservation.booker_phone or "").strip()
    thread_id = ""
    external = (reservation.external_id or "").strip()
    if external:
        thread_id = external[:128]
    return {
        "recipient_type": MessageRecipientType.BOOKER,
        "recipient_email": email,
        "recipient_phone": phone,
        "recipient_booking_thread_id": thread_id,
    }


def _expires_at_for(
    definition: MessageDefinition,
    *,
    due_at: datetime,
    now: datetime,
) -> datetime | None:
    if definition.expires_after is None:
        return None
    base = max(due_at, now)
    return base + definition.expires_after


def _candidate_reservations(
    *,
    now: datetime,
    horizon_days: int,
    lookback_days: int,
    tenant_id: int | None,
    property_id: int | None,
    max_days_before: int,
) -> QuerySet[Reservation]:
    """EXPECTED reservations whose check-in could yield a due within the window."""
    # check_in − days_before ≈ today ⇒ check_in ≈ today + days_before
    # Include a buffer so property-specific days_before still match.
    today = now.date()
    earliest = today - timedelta(days=max(lookback_days, 0) + max_days_before)
    latest = today + timedelta(days=max(horizon_days, 0) + max_days_before)
    qs = (
        Reservation.objects.filter(
            status=Reservation.Status.EXPECTED,
            check_in__gte=earliest,
            check_in__lte=latest,
        )
        .select_related("property", "tenant", "tenant__reception_settings")
        .order_by("check_in", "id")
    )
    if tenant_id is not None:
        qs = qs.filter(tenant_id=tenant_id)
    if property_id is not None:
        qs = qs.filter(property_id=property_id)
    return qs


def _should_materialize_welcome(prop: Property) -> bool:
    """WELCOME stays gated on the existing autocheck-in enable flag."""
    return bool(prop.whatsapp_autocheckin_enabled)


def _due_within_window(
    computed: ComputedDue,
    *,
    now: datetime,
    horizon_days: int,
    lookback_days: int,
) -> bool:
    from datetime import timezone as dt_timezone

    clock = now
    if clock.tzinfo is None:
        clock = timezone.make_aware(clock, dt_timezone.utc)
    now_utc = clock.astimezone(dt_timezone.utc)
    earliest = now_utc - timedelta(days=max(lookback_days, 0))
    latest = now_utc + timedelta(days=max(horizon_days, 0))
    return earliest <= computed.due_at <= latest


def create_planned_dispatch(
    *,
    reservation: Reservation,
    definition: MessageDefinition,
    plan: ReminderPlan,
    schedule: ResolvedSchedule,
    computed: ComputedDue,
    now: datetime,
    correlation_id: uuid.UUID | None = None,
) -> MessageDispatch | None:
    """Insert one planned TIME dispatch if not deduped. Returns None on skip."""
    duplicate = find_duplicate_dispatch(
        tenant_id=reservation.tenant_id,
        reservation_id=reservation.pk,
        definition=definition,
        plan_key=plan.key,
    )
    if duplicate is not None:
        metrics.incr(
            "messaging_materialize_deduped",
            definition=definition.key,
            plan=plan.key,
        )
        return None

    recipient = _recipient_snapshot(reservation)
    expires_at = _expires_at_for(definition, due_at=computed.due_at, now=now)
    corr = correlation_id or uuid.uuid4()

    dispatch = MessageDispatch.objects.create(
        tenant_id=reservation.tenant_id,
        reservation=reservation,
        definition_key=definition.key,
        plan_key=plan.key,
        trigger=MessageTriggerKind.TIME,
        correlation_id=corr,
        due_at=computed.due_at,
        timezone=computed.timezone,
        local_due_at=computed.local_due_at,
        expires_at=expires_at,
        schedule_strategy=computed.schedule_strategy,
        status=MessageDispatchStatus.PLANNED,
        policy_version="",
        recipient_type=recipient["recipient_type"],
        recipient_email=recipient["recipient_email"],
        recipient_phone=recipient["recipient_phone"],
        recipient_booking_thread_id=recipient["recipient_booking_thread_id"],
    )
    MessageDispatchEvent.objects.create(
        tenant_id=reservation.tenant_id,
        dispatch=dispatch,
        event_type=MessageDispatchEventType.DISPATCH_CREATED,
        payload={
            "via": "time_materialize",
            "plan_key": plan.key,
            "schedule_strategy": computed.schedule_strategy,
            "days_before": schedule.days_before,
            "send_time": schedule.send_time.strftime("%H:%M"),
            "schedule_source": schedule.source_summary,
            "timezone": computed.timezone,
            "target_local_date": computed.target_local_date.isoformat(),
        },
    )
    metrics.incr(
        "messaging_materialize_created",
        definition=definition.key,
        plan=plan.key,
    )
    return dispatch


def materialize_plan_for_reservation(
    reservation: Reservation,
    plan: ReminderPlan,
    *,
    now: datetime | None = None,
    horizon_days: int = DEFAULT_MATERIALIZE_HORIZON_DAYS,
    lookback_days: int = DEFAULT_MATERIALIZE_LOOKBACK_DAYS,
) -> list[MessageDispatch]:
    """Materialize all definition keys for one plan × reservation."""
    clock = now or timezone.now()
    prop = reservation.property
    if plan.key == PLAN_WELCOME and not _should_materialize_welcome(prop):
        return []

    schedule = resolve_schedule_for_plan(
        plan.schedule_prefix,
        property=prop,
        tenant=reservation.tenant,
    )
    tz_name = property_timezone_name(prop, reservation.tenant)
    computed = compute_due(
        check_in=reservation.check_in,
        schedule=schedule,
        timezone_name=tz_name,
        now=clock,
    )
    if not _due_within_window(
        computed,
        now=clock,
        horizon_days=horizon_days,
        lookback_days=lookback_days,
    ):
        return []

    created: list[MessageDispatch] = []
    batch_correlation = uuid.uuid4()
    for def_key in plan.definition_keys:
        try:
            definition = definition_registry.get(def_key)
        except KeyError:
            logger.warning(
                "messaging_materialize_unknown_definition plan=%s key=%s",
                plan.key,
                def_key,
            )
            continue
        # Prefer resolved schedule strategy over definition default for TIME plans.
        row = create_planned_dispatch(
            reservation=reservation,
            definition=definition,
            plan=plan,
            schedule=schedule,
            computed=computed,
            now=clock,
            correlation_id=batch_correlation
            if len(plan.definition_keys) > 1
            else uuid.uuid4(),
        )
        if row is not None:
            created.append(row)
    return created


def cancel_stale_time_dispatches(
    *,
    now: datetime | None = None,
    limit: int = 500,
    tenant_id: int | None = None,
) -> int:
    """Cancel planned/queued TIME rows whose reservation is no longer EXPECTED."""
    clock = now or timezone.now()
    qs = (
        MessageDispatch.objects.select_related("reservation")
        .filter(
            status__in=_CLAIMABLE,
            trigger=MessageTriggerKind.TIME,
            archived_at__isnull=True,
        )
        .exclude(reservation__status=Reservation.Status.EXPECTED)
        .order_by("id")
    )
    if tenant_id is not None:
        qs = qs.filter(tenant_id=tenant_id)

    rows = list(qs[:limit])
    cancelled = 0
    for dispatch in rows:
        with transaction.atomic():
            locked = (
                MessageDispatch.objects.select_for_update(skip_locked=True)
                .filter(pk=dispatch.pk, status__in=_CLAIMABLE)
                .first()
            )
            if locked is None:
                continue
            locked.status = MessageDispatchStatus.CANCELLED
            locked.save(update_fields=["status", "updated_at"])
            MessageDispatchEvent.objects.create(
                tenant_id=locked.tenant_id,
                dispatch=locked,
                event_type=MessageDispatchEventType.CANCELLED,
                payload={
                    "reason": "reservation_not_expected",
                    "cancelled_at": clock.isoformat(),
                },
            )
            cancelled += 1
    if cancelled:
        metrics.incr("messaging_dispatch_cancelled_stale", value=cancelled)
    return cancelled


def materialize_time_triggers(
    *,
    now: datetime | None = None,
    tenant_id: int | None = None,
    property_id: int | None = None,
    horizon_days: int = DEFAULT_MATERIALIZE_HORIZON_DAYS,
    lookback_days: int = DEFAULT_MATERIALIZE_LOOKBACK_DAYS,
    plans: Iterable[ReminderPlan] | None = None,
) -> int:
    """Create planned TIME dispatches from ReminderPlan + resolved schedules.

    Returns the number of newly created ``MessageDispatch`` rows.
    """
    clock = now or timezone.now()
    plan_list = list(plans) if plans is not None else list(plan_registry.all())
    if not plan_list:
        return 0

    # Conservative max days_before so candidate query is wide enough.
    max_days_before = 30
    for plan in plan_list:
        # Platform defaults are small; property overrides rarely exceed this.
        max_days_before = max(max_days_before, 30)

    reservations = _candidate_reservations(
        now=clock,
        horizon_days=horizon_days,
        lookback_days=lookback_days,
        tenant_id=tenant_id,
        property_id=property_id,
        max_days_before=max_days_before,
    )

    created_total = 0
    for reservation in reservations.iterator(chunk_size=100):
        for plan in plan_list:
            try:
                created = materialize_plan_for_reservation(
                    reservation,
                    plan,
                    now=clock,
                    horizon_days=horizon_days,
                    lookback_days=lookback_days,
                )
            except Exception:
                logger.exception(
                    "messaging_materialize_failed reservation_id=%s plan=%s",
                    reservation.pk,
                    plan.key,
                )
                continue
            created_total += len(created)

    if created_total:
        logger.info(
            "messaging_materialize_time_triggers created=%s tenant_id=%s property_id=%s",
            created_total,
            tenant_id,
            property_id,
        )
    return created_total


def run_scheduler_cycle(
    *,
    now: datetime | None = None,
    tenant_id: int | None = None,
    property_id: int | None = None,
    claim_limit: int = 50,
    claim: bool = True,
) -> dict[str, int]:
    """Expire → cancel stale → materialize → (optional) claim due.

    Provider send / ``dispatch_one`` is Celery
    ``communications.run_message_orchestration`` (Phase 6) — this cycle
    never calls the dispatcher.

    Ops dry-run / production probe (materialize → planned rows → STOP)::

        run_scheduler_cycle(claim=False, property_id=…)

    With ``claim=False``, due rows stay ``planned`` / ``queued`` so nothing
    enters ``dispatching``. With ``claim=True``, rows are claimed only
    (status → ``dispatching``); callers must still run the dispatcher.
    """
    clock = now or timezone.now()
    expired = expire_overdue_dispatches(now=clock, tenant_id=tenant_id)
    cancelled = cancel_stale_time_dispatches(now=clock, tenant_id=tenant_id)
    created = materialize_time_triggers(
        now=clock,
        tenant_id=tenant_id,
        property_id=property_id,
    )
    claimed_count = 0
    if claim:
        claimed = claim_due_dispatches(
            now=clock,
            limit=claim_limit,
            tenant_id=tenant_id,
            property_id=property_id,
        )
        claimed_count = len(claimed)

    summary = {
        "expired": expired,
        "cancelled": cancelled,
        "materialized": created,
        "claimed": claimed_count,
    }
    # Cycle rollup for ops (individual steps also emit their own counters).
    metrics.incr(
        "messaging_scheduler_cycle",
        value=1,
        expired=expired,
        cancelled=cancelled,
        materialized=created,
        claimed=claimed_count,
        claim_enabled=int(claim),
        tenant_id=tenant_id,
        property_id=property_id,
    )
    logger.info(
        "messaging_scheduler_cycle expired=%s cancelled=%s materialized=%s "
        "claimed=%s claim=%s tenant_id=%s property_id=%s",
        expired,
        cancelled,
        created,
        claimed_count,
        claim,
        tenant_id,
        property_id,
    )
    return summary


def build_time_trigger_context(
    reservation: Reservation,
    *,
    plan_key: str,
    now: datetime | None = None,
    correlation_id: uuid.UUID | None = None,
) -> TriggerContext:
    """Helper for tests / callers that need a TIME TriggerContext."""
    return TriggerContext(
        reservation_id=reservation.pk,
        tenant_id=reservation.tenant_id,
        property_id=reservation.property_id,
        trigger=Trigger.time(source=plan_key),
        correlation_id=correlation_id or uuid.uuid4(),
        now=now or timezone.now(),
    )
