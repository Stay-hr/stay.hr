"""Dispatcher — claim → middleware → skip → render-once → providers (ADR 0010).

Transaction rule (Phase 3 review):
  claim (atomic) → COMMIT → provider I/O (no open SQL txn) → short atomic writes.

Never hold ``select_for_update`` / ``atomic`` open while waiting on SMTP / Channex / Meta.
"""

from __future__ import annotations

import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.communications.messaging import metrics
from apps.communications.messaging.alerts import alert_all_providers_failed
from apps.communications.messaging.context import TriggerContext
from apps.communications.messaging.definitions import (
    MessageDefinition,
    definition_registry,
)
from apps.communications.messaging.middleware import middleware_registry
from apps.communications.messaging.models import (
    MessageDeliveryAttempt,
    MessageDispatch,
    MessageDispatchEvent,
    MessageDispatchEventType,
    MessageDispatchStatus,
    MessageErrorCategory,
)
from apps.communications.messaging.providers.base import (
    DEFAULT_PROVIDER_TIMEOUTS,
    DEFAULT_TIMEOUT_SECONDS,
    MessageProvider,
)
from apps.communications.messaging.providers.registry import provider_registry
from apps.communications.messaging.results import DeliveryResult
from apps.communications.messaging.skip_rules import SkipDecision, skip_rule_engine
from apps.communications.messaging.templates import template_registry
from apps.communications.messaging.triggers import Trigger

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DispatchOutcome:
    dispatch_id: int
    status: str
    results: tuple[DeliveryResult, ...] = ()
    skip_reason: str = ""


def context_from_dispatch(
    dispatch: MessageDispatch,
    *,
    now: datetime | None = None,
    extras: dict[str, Any] | None = None,
) -> TriggerContext:
    """Rebuild TriggerContext from a persisted outbox row."""
    reservation = getattr(dispatch, "reservation", None)
    property_id = None
    if reservation is not None:
        property_id = getattr(reservation, "property_id", None)
    return TriggerContext(
        reservation_id=dispatch.reservation_id,
        tenant_id=dispatch.tenant_id,
        property_id=property_id,
        trigger=Trigger(
            kind=dispatch.trigger,
            source=dispatch.plan_key or "",
        ),
        correlation_id=dispatch.correlation_id or uuid.uuid4(),
        now=now or timezone.now(),
        extras=extras or {},
    )


def _record_event(
    dispatch: MessageDispatch,
    event_type: str,
    *,
    payload: dict[str, Any] | None = None,
    attempt: MessageDeliveryAttempt | None = None,
) -> None:
    MessageDispatchEvent.objects.create(
        tenant_id=dispatch.tenant_id,
        dispatch=dispatch,
        event_type=event_type,
        payload=payload or {},
        attempt=attempt,
    )


def _provider_timeout(provider: MessageProvider) -> float:
    configured = getattr(provider, "timeout_seconds", None)
    if configured is not None:
        try:
            value = float(configured)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return float(
        DEFAULT_PROVIDER_TIMEOUTS.get(provider.name, DEFAULT_TIMEOUT_SECONDS)
    )


def _coerce_delivery_result(
    raw: Any,
    *,
    provider: MessageProvider,
) -> DeliveryResult:
    """Providers must return DeliveryResult; anything else becomes a controlled fail."""
    if isinstance(raw, DeliveryResult):
        return raw
    logger.error(
        "messaging_provider_invalid_return provider=%s type=%s",
        provider.name,
        type(raw).__name__,
    )
    return DeliveryResult.fail(
        provider=provider.name,
        channel=provider.channel,
        error_category=MessageErrorCategory.VALIDATION,
        error_code="invalid_delivery_result",
        error_message=(
            f"Provider {provider.name!r} must return DeliveryResult, "
            f"got {type(raw).__name__}"
        ),
        retryable=False,
    )


def _call_provider(
    provider: MessageProvider,
    dispatch: MessageDispatch,
    ctx: TriggerContext,
) -> DeliveryResult:
    """Invoke provider.send with a hard timeout; always return DeliveryResult.

    Runs ``send`` in a worker thread so a hung SMTP/HTTP call cannot block the
    Celery worker forever. Adapters must still set their own HTTP client
    timeouts; this is the hard upper bound. DB connections are closed in the
    worker thread (Django is not thread-safe for shared connections).
    """
    from django.db import close_old_connections

    timeout = _provider_timeout(provider)
    started = time.perf_counter()

    def _run() -> Any:
        close_old_connections()
        try:
            return provider.send(dispatch, ctx)
        finally:
            close_old_connections()

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_run)
            raw = future.result(timeout=timeout)
        result = _coerce_delivery_result(raw, provider=provider)
    except FuturesTimeoutError:
        logger.error(
            "messaging_provider_timeout provider=%s dispatch_id=%s timeout_s=%s",
            provider.name,
            dispatch.pk,
            timeout,
        )
        result = DeliveryResult.fail(
            provider=provider.name,
            channel=provider.channel,
            error_category=MessageErrorCategory.NETWORK,
            error_code="provider_timeout",
            error_message=f"Provider {provider.name!r} exceeded {timeout}s timeout",
            retryable=True,
        )
    except Exception as exc:  # noqa: BLE001 — provider boundary
        logger.exception(
            "messaging_provider_raised provider=%s dispatch_id=%s",
            provider.name,
            dispatch.pk,
        )
        result = DeliveryResult.fail(
            provider=provider.name,
            channel=provider.channel,
            error_category=MessageErrorCategory.UNKNOWN,
            error_code="provider_exception",
            error_message=str(exc)[:500],
            retryable=True,
        )

    duration_ms = int((time.perf_counter() - started) * 1000)
    if result.duration_ms is None:
        result = DeliveryResult(
            success=result.success,
            provider=result.provider,
            channel=result.channel,
            provider_message_id=result.provider_message_id,
            error_category=result.error_category,
            error_code=result.error_code,
            error_message=result.error_message,
            retryable=result.retryable,
            duration_ms=duration_ms,
            outbound_message_id=result.outbound_message_id,
        )
    return result


@transaction.atomic
def _prepare_dispatch(
    dispatch_id: int,
    *,
    ctx: TriggerContext | None = None,
) -> tuple[MessageDispatch, TriggerContext, MessageDefinition] | DispatchOutcome:
    """Short DB txn: lock, skip/render, commit before any provider I/O.

    Returns either a ready ``(dispatch, ctx, definition)`` tuple or a terminal
    ``DispatchOutcome`` (skipped / already terminal).
    """
    locked = (
        MessageDispatch.objects.select_for_update()
        .select_related("reservation")
        .get(pk=dispatch_id)
    )
    if locked.status in (
        MessageDispatchStatus.DELIVERED,
        MessageDispatchStatus.SKIPPED,
        MessageDispatchStatus.CANCELLED,
    ):
        return DispatchOutcome(dispatch_id=locked.pk, status=locked.status)

    if locked.status in (
        MessageDispatchStatus.PLANNED,
        MessageDispatchStatus.QUEUED,
    ):
        locked.status = MessageDispatchStatus.DISPATCHING
        locked.save(update_fields=["status", "updated_at"])

    trigger_ctx = ctx or context_from_dispatch(locked)
    definition = definition_registry.get(locked.definition_key)

    if not definition.delivery_window.allows(locked, trigger_ctx):
        locked.status = MessageDispatchStatus.SKIPPED
        locked.save(update_fields=["status", "updated_at"])
        _record_event(
            locked,
            MessageDispatchEventType.SKIPPED,
            payload={"reason": "delivery_window"},
        )
        return DispatchOutcome(
            dispatch_id=locked.pk,
            status=MessageDispatchStatus.SKIPPED,
            skip_reason="delivery_window",
        )

    decision: SkipDecision = skip_rule_engine.can_send(
        locked,
        trigger_ctx,
        rule_names=definition.skip_rule_names,
    )
    if not decision.can_send:
        locked.status = MessageDispatchStatus.SKIPPED
        locked.save(update_fields=["status", "updated_at"])
        _record_event(
            locked,
            MessageDispatchEventType.SKIPPED,
            payload={"reason": decision.reason},
        )
        metrics.incr(
            "messaging_dispatch_skipped",
            definition=locked.definition_key,
            reason=decision.reason,
        )
        return DispatchOutcome(
            dispatch_id=locked.pk,
            status=MessageDispatchStatus.SKIPPED,
            skip_reason=decision.reason,
        )

    if not (locked.render_checksum and locked.rendered_body):
        snapshot = template_registry.render(
            renderer_key=definition.renderer_key,
            template_version=definition.template_version,
            dispatch=locked,
            ctx=trigger_ctx,
            language=locked.language,
        )
        locked.rendered_body = snapshot.body
        locked.rendered_subject = snapshot.subject
        locked.language = snapshot.language or locked.language
        locked.template_version = snapshot.template_version
        locked.render_context = snapshot.render_context
        locked.render_checksum = snapshot.checksum
        if not locked.policy_version:
            locked.policy_version = definition.bound_policy_version()
        locked.save(
            update_fields=[
                "rendered_body",
                "rendered_subject",
                "language",
                "template_version",
                "render_context",
                "render_checksum",
                "policy_version",
                "updated_at",
            ]
        )
        _record_event(
            locked,
            MessageDispatchEventType.RENDERED,
            payload={
                "template_version": snapshot.template_version,
                "render_checksum": snapshot.checksum,
                "policy_version": locked.policy_version,
            },
        )
    elif not locked.policy_version:
        locked.policy_version = definition.bound_policy_version()
        locked.save(update_fields=["policy_version", "updated_at"])

    return locked, trigger_ctx, definition


@transaction.atomic
def _persist_attempt_and_maybe_deliver(
    *,
    dispatch_id: int,
    attempt_number: int,
    result: DeliveryResult,
    is_fallback: bool,
) -> tuple[MessageDispatch, MessageDeliveryAttempt]:
    """Short DB txn for one attempt row (+ delivered status on success)."""
    locked = MessageDispatch.objects.select_for_update().get(pk=dispatch_id)
    if is_fallback and not locked.fallback_used:
        locked.fallback_used = True
        locked.save(update_fields=["fallback_used", "updated_at"])

    attempt = MessageDeliveryAttempt.objects.create(
        tenant_id=locked.tenant_id,
        dispatch=locked,
        channel=result.channel,
        provider=result.provider,
        attempt_number=attempt_number,
        success=result.success,
        duration_ms=result.duration_ms,
        error_category=result.error_category if not result.success else "",
        retryable=result.retryable,
        error_code=result.error_code,
        error_message=result.error_message,
        provider_message_id=result.provider_message_id,
        outbound_message_id=result.outbound_message_id,
    )
    metrics.observe_ms(
        "messaging_provider_attempt",
        duration_ms=attempt.duration_ms or 0,
        provider=result.provider,
        success=result.success,
        definition=locked.definition_key,
    )
    if result.success:
        locked.status = MessageDispatchStatus.DELIVERED
        locked.save(update_fields=["status", "updated_at"])
        _record_event(
            locked,
            MessageDispatchEventType.DELIVERED,
            payload={
                "provider": result.provider,
                "channel": result.channel,
                "provider_message_id": result.provider_message_id,
            },
            attempt=attempt,
        )
        metrics.incr(
            "messaging_dispatch_delivered",
            definition=locked.definition_key,
            provider=result.provider,
        )
    return locked, attempt


@transaction.atomic
def _mark_failed(
    dispatch_id: int,
    *,
    results: tuple[DeliveryResult, ...],
) -> MessageDispatch:
    locked = MessageDispatch.objects.select_for_update().get(pk=dispatch_id)
    locked.status = MessageDispatchStatus.FAILED
    locked.save(update_fields=["status", "updated_at"])
    _record_event(
        locked,
        MessageDispatchEventType.FAILED,
        payload={
            "providers_tried": [r.provider for r in results],
            "fallback_used": locked.fallback_used,
        },
    )
    metrics.incr(
        "messaging_dispatch_failed",
        definition=locked.definition_key,
    )
    return locked


def _next_attempt_number(dispatch_id: int) -> int:
    last = (
        MessageDeliveryAttempt.objects.filter(dispatch_id=dispatch_id)
        .order_by("-attempt_number")
        .values_list("attempt_number", flat=True)
        .first()
    )
    return int(last or 0) + 1


def dispatch_one(
    dispatch: MessageDispatch,
    *,
    ctx: TriggerContext | None = None,
) -> DispatchOutcome:
    """Process one outbox row.

    Flow: prepare (atomic, COMMIT) → middleware → provider calls (no txn) →
    short atomic attempt/status writes. Claim must already be committed (or
    prepare will promote planned/queued → dispatching inside its short txn).
    """
    if dispatch.status in (
        MessageDispatchStatus.DELIVERED,
        MessageDispatchStatus.SKIPPED,
        MessageDispatchStatus.CANCELLED,
    ):
        return DispatchOutcome(dispatch_id=dispatch.pk, status=dispatch.status)

    prepared = _prepare_dispatch(dispatch.pk, ctx=ctx)
    if isinstance(prepared, DispatchOutcome):
        # Terminal during prepare (skip / already done) — still run after hooks.
        trigger_ctx = ctx or context_from_dispatch(dispatch)
        middleware_registry.before_dispatch(dispatch, trigger_ctx)
        middleware_registry.after_dispatch(dispatch, trigger_ctx, prepared)
        return prepared

    locked, trigger_ctx, definition = prepared
    middleware_registry.before_dispatch(locked, trigger_ctx)

    results: list[DeliveryResult] = []
    attempt_number = _next_attempt_number(locked.pk)
    providers = definition.channel_policy.providers
    outcome: DispatchOutcome | None = None

    try:
        for index, provider_name in enumerate(providers):
            provider = provider_registry.get(provider_name)
            event_type = (
                MessageDispatchEventType.CHANNEL_SELECTED
                if index == 0
                else MessageDispatchEventType.FALLBACK
            )
            # Event write is a short implicit autocommit (no surrounding atomic).
            _record_event(
                locked,
                event_type,
                payload={
                    "provider": provider_name,
                    "channel": provider.channel,
                    "attempt_number": attempt_number,
                    "timeout_seconds": _provider_timeout(provider),
                },
            )

            # Provider I/O — intentionally outside any SQL transaction.
            result = _call_provider(provider, locked, trigger_ctx)
            locked, attempt = _persist_attempt_and_maybe_deliver(
                dispatch_id=locked.pk,
                attempt_number=attempt_number,
                result=result,
                is_fallback=index > 0,
            )
            results.append(result)
            attempt_number += 1

            if result.success:
                outcome = DispatchOutcome(
                    dispatch_id=locked.pk,
                    status=MessageDispatchStatus.DELIVERED,
                    results=tuple(results),
                )
                return outcome

        locked = _mark_failed(locked.pk, results=tuple(results))
        alert_all_providers_failed(locked, results)
        outcome = DispatchOutcome(
            dispatch_id=locked.pk,
            status=MessageDispatchStatus.FAILED,
            results=tuple(results),
        )
        return outcome
    finally:
        final = outcome or DispatchOutcome(
            dispatch_id=locked.pk,
            status=locked.status,
            results=tuple(results),
        )
        middleware_registry.after_dispatch(locked, trigger_ctx, final)


def process_due_dispatches(
    *,
    limit: int = 50,
    now: datetime | None = None,
    tenant_id: int | None = None,
    property_id: int | None = None,
) -> list[DispatchOutcome]:
    """Claim due rows (SKIP LOCKED, COMMIT) then dispatch each without holding locks."""
    from apps.communications.messaging.scheduler import claim_due_dispatches

    claimed = claim_due_dispatches(
        limit=limit,
        now=now,
        tenant_id=tenant_id,
        property_id=property_id,
    )
    # claim_due_dispatches atomic has committed — provider I/O starts after this.
    outcomes: list[DispatchOutcome] = []
    for row in claimed:
        try:
            outcomes.append(dispatch_one(row))
        except Exception:
            logger.exception(
                "messaging_dispatch_one_failed dispatch_id=%s",
                row.pk,
            )
            metrics.incr("messaging_dispatch_error", dispatch_id=row.pk)
    return outcomes
