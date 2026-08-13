"""Phase 8: PostGIS contract tests for Messaging Engine (ADR 0010).

Covers: idempotency, dedupe, fallback, replay, SKIP LOCKED claims, health.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta

from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.communications.messaging.bootstrap import (
    bootstrap_messaging_engine,
    reset_messaging_engine_for_tests,
)
from apps.communications.messaging.dedupe import find_duplicate_dispatch, is_duplicate
from apps.communications.messaging.definitions import (
    DedupePolicy,
    definition_registry,
)
from apps.communications.messaging.dispatcher import dispatch_one
from apps.communications.messaging.health import messaging_health_snapshot
from apps.communications.messaging.models import (
    MessageDeliveryAttempt,
    MessageDispatch,
    MessageDispatchEvent,
    MessageDispatchEventType,
    MessageDispatchStatus,
    MessageErrorCategory,
    MessageReplayReason,
    MessageScheduleStrategy,
    MessageTriggerKind,
)
from apps.communications.messaging.providers.base import (
    MessageProvider,
    ProviderCapabilities,
)
from apps.communications.messaging.providers.registry import provider_registry
from apps.communications.messaging.providers.stubs import StubProvider
from apps.communications.messaging.replay import ReplayError, replay_dispatch
from apps.communications.messaging.results import DeliveryResult
from apps.communications.messaging.scheduler import (
    claim_due_dispatches,
    create_planned_dispatch,
    materialize_time_triggers,
)
from apps.communications.messaging.plans import PLAN_PRE_ARRIVAL, plan_registry
from apps.communications.messaging.schedule_settings import (
    compute_due,
    resolve_schedule_for_plan,
)
from apps.core.system_status import build_system_status_payload
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant


class _OkProvider(MessageProvider):
    def __init__(self, name: str = "booking"):
        self.name = name
        self.channel = name
        self.timeout_seconds = 5.0
        self.capabilities = ProviderCapabilities(channels=frozenset({name}))

    def send(self, dispatch, ctx) -> DeliveryResult:
        return DeliveryResult.ok(
            provider=self.name,
            channel=self.channel,
            provider_message_id=f"{self.name}-ok",
            duration_ms=3,
        )


class _FailProvider(MessageProvider):
    def __init__(self, name: str, channel: str | None = None):
        self.name = name
        self.channel = channel or name
        self.timeout_seconds = 5.0
        self.capabilities = ProviderCapabilities(
            channels=frozenset({self.channel})
        )

    def send(self, dispatch, ctx) -> DeliveryResult:
        return DeliveryResult.fail(
            provider=self.name,
            channel=self.channel,
            error_category=MessageErrorCategory.PROVIDER,
            error_code="provider_down",
            error_message="simulated failure",
            retryable=True,
            duration_ms=7,
        )


class _MessagingFixturesMixin:
    def _bootstrap(self) -> None:
        reset_messaging_engine_for_tests()
        bootstrap_messaging_engine(force=True, validate=True)

    def _teardown_engine(self) -> None:
        reset_messaging_engine_for_tests()
        bootstrap_messaging_engine(force=True, validate=True)

    def _seed_tenant_property_reservation(self, *, slug_prefix: str = "p8"):
        self.tenant = Tenant.objects.create(
            name=f"Phase8 {slug_prefix}",
            slug=f"{slug_prefix}-tenant",
            default_language="en",
            timezone="Europe/Zagreb",
        )
        self.property = Property.objects.create(
            tenant=self.tenant,
            name=f"Villa {slug_prefix}",
            slug=f"{slug_prefix}-villa",
            timezone="Europe/Zagreb",
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booker_name="Phase8 Guest",
            booker_email="phase8@example.com",
            booker_phone="+38591111333",
            external_id="ext-phase8",
            status=Reservation.Status.EXPECTED,
            check_in=timezone.localdate() + timedelta(days=7),
            check_out=timezone.localdate() + timedelta(days=10),
        )

    def _make_dispatch(self, **overrides) -> MessageDispatch:
        now = timezone.now()
        defaults = dict(
            tenant=self.tenant,
            reservation=self.reservation,
            definition_key="CHECKIN_INFO",
            plan_key=PLAN_PRE_ARRIVAL,
            trigger=MessageTriggerKind.TIME,
            due_at=now - timedelta(minutes=1),
            timezone="Europe/Zagreb",
            local_due_at=now - timedelta(minutes=1),
            schedule_strategy=MessageScheduleStrategy.FIXED_TIME,
            status=MessageDispatchStatus.QUEUED,
            recipient_email="phase8@example.com",
            recipient_phone="+38591111333",
            recipient_booking_thread_id="ext-phase8",
        )
        defaults.update(overrides)
        return MessageDispatch.objects.create(**defaults)

    def _register_providers(self, *providers: MessageProvider) -> None:
        provider_registry.clear()
        for provider in providers:
            provider_registry.register(provider)
        if not provider_registry.has("whatsapp"):
            provider_registry.register(
                StubProvider(name="whatsapp", channel="whatsapp")
            )
        if not provider_registry.has("email"):
            provider_registry.register(_OkProvider("email"))
        if not provider_registry.has("booking"):
            provider_registry.register(_OkProvider("booking"))


class MessagingDedupeTests(_MessagingFixturesMixin, TestCase):
    def setUp(self):
        self._bootstrap()
        self._seed_tenant_property_reservation(slug_prefix="dedupe")

    def tearDown(self):
        self._teardown_engine()

    def test_find_duplicate_blocks_active_statuses(self):
        definition = definition_registry.get("CHECKIN_INFO")
        self._make_dispatch(status=MessageDispatchStatus.PLANNED)
        self.assertTrue(
            is_duplicate(
                tenant_id=self.tenant.pk,
                reservation_id=self.reservation.pk,
                definition=definition,
                plan_key=PLAN_PRE_ARRIVAL,
            )
        )
        dup = find_duplicate_dispatch(
            tenant_id=self.tenant.pk,
            reservation_id=self.reservation.pk,
            definition=definition,
            plan_key=PLAN_PRE_ARRIVAL,
        )
        self.assertIsNotNone(dup)
        self.assertEqual(dup.status, MessageDispatchStatus.PLANNED)

    def test_failed_does_not_dedupe_allows_rematerialize(self):
        definition = definition_registry.get("CHECKIN_INFO")
        self._make_dispatch(status=MessageDispatchStatus.FAILED)
        self.assertFalse(
            is_duplicate(
                tenant_id=self.tenant.pk,
                reservation_id=self.reservation.pk,
                definition=definition,
                plan_key=PLAN_PRE_ARRIVAL,
            )
        )

    def test_delivered_dedupes(self):
        definition = definition_registry.get("CHECKIN_INFO")
        self._make_dispatch(status=MessageDispatchStatus.DELIVERED)
        self.assertTrue(
            is_duplicate(
                tenant_id=self.tenant.pk,
                reservation_id=self.reservation.pk,
                definition=definition,
            )
        )

    def test_include_plan_key_scopes_dedupe(self):
        from dataclasses import replace

        base = definition_registry.get("CHECKIN_INFO")
        definition = replace(
            base,
            dedupe=DedupePolicy(enabled=True, include_plan_key=True),
        )
        self._make_dispatch(
            plan_key="pre_arrival",
            status=MessageDispatchStatus.PLANNED,
        )
        self.assertTrue(
            is_duplicate(
                tenant_id=self.tenant.pk,
                reservation_id=self.reservation.pk,
                definition=definition,
                plan_key="pre_arrival",
            )
        )
        self.assertFalse(
            is_duplicate(
                tenant_id=self.tenant.pk,
                reservation_id=self.reservation.pk,
                definition=definition,
                plan_key="other_plan",
            )
        )


class MessagingIdempotencyTests(_MessagingFixturesMixin, TestCase):
    def setUp(self):
        self._bootstrap()
        self._seed_tenant_property_reservation(slug_prefix="idem")
        self._register_providers(_OkProvider("booking"), _OkProvider("email"))

    def tearDown(self):
        self._teardown_engine()

    def test_dispatch_one_on_delivered_is_noop(self):
        dispatch = self._make_dispatch(status=MessageDispatchStatus.QUEUED)
        first = dispatch_one(dispatch)
        self.assertEqual(first.status, MessageDispatchStatus.DELIVERED)
        attempts_after_first = MessageDeliveryAttempt.objects.filter(
            dispatch=dispatch
        ).count()
        self.assertGreaterEqual(attempts_after_first, 1)

        dispatch.refresh_from_db()
        second = dispatch_one(dispatch)
        self.assertEqual(second.status, MessageDispatchStatus.DELIVERED)
        self.assertEqual(
            MessageDeliveryAttempt.objects.filter(dispatch=dispatch).count(),
            attempts_after_first,
        )

    def test_materialize_twice_creates_once(self):
        from datetime import date, datetime
        from zoneinfo import ZoneInfo

        zagreb = ZoneInfo("Europe/Zagreb")
        now = datetime(2026, 8, 1, 10, 0, tzinfo=zagreb)
        self.reservation.check_in = date(2026, 8, 8)
        self.reservation.check_out = date(2026, 8, 11)
        self.reservation.save(update_fields=["check_in", "check_out", "updated_at"])

        created = materialize_time_triggers(
            now=now,
            property_id=self.property.pk,
            plans=[plan_registry.get(PLAN_PRE_ARRIVAL)],
        )
        self.assertEqual(created, 2)
        again = materialize_time_triggers(
            now=now,
            property_id=self.property.pk,
            plans=[plan_registry.get(PLAN_PRE_ARRIVAL)],
        )
        self.assertEqual(again, 0)
        self.assertEqual(
            MessageDispatch.objects.filter(reservation=self.reservation).count(),
            2,
        )


class MessagingFallbackTests(_MessagingFixturesMixin, TestCase):
    def setUp(self):
        self._bootstrap()
        self._seed_tenant_property_reservation(slug_prefix="fb")

    def tearDown(self):
        self._teardown_engine()

    def test_fallback_keeps_checksum_and_policy_version(self):
        self._register_providers(
            _FailProvider("booking"),
            _OkProvider("email"),
        )
        dispatch = self._make_dispatch()
        outcome = dispatch_one(dispatch)
        self.assertEqual(outcome.status, MessageDispatchStatus.DELIVERED)
        dispatch.refresh_from_db()
        self.assertTrue(dispatch.fallback_used)
        checksum = dispatch.render_checksum
        policy = dispatch.policy_version
        self.assertTrue(checksum)
        self.assertTrue(policy)

        attempts = list(
            MessageDeliveryAttempt.objects.filter(dispatch=dispatch).order_by(
                "attempt_number"
            )
        )
        self.assertEqual(len(attempts), 2)
        self.assertFalse(attempts[0].success)
        self.assertEqual(attempts[0].error_category, MessageErrorCategory.PROVIDER)
        self.assertEqual(attempts[0].duration_ms, 7)
        self.assertTrue(attempts[1].success)

        # Re-render must not have happened — checksum frozen after first render.
        dispatch.refresh_from_db()
        self.assertEqual(dispatch.render_checksum, checksum)
        self.assertEqual(dispatch.policy_version, policy)
        events = set(
            MessageDispatchEvent.objects.filter(dispatch=dispatch).values_list(
                "event_type", flat=True
            )
        )
        self.assertIn(MessageDispatchEventType.FALLBACK, events)
        self.assertEqual(
            MessageDispatchEvent.objects.filter(
                dispatch=dispatch,
                event_type=MessageDispatchEventType.RENDERED,
            ).count(),
            1,
        )


class MessagingReplayTests(_MessagingFixturesMixin, TestCase):
    def setUp(self):
        self._bootstrap()
        self._seed_tenant_property_reservation(slug_prefix="replay")

    def tearDown(self):
        self._teardown_engine()

    def test_replay_requires_valid_reason_and_lineage(self):
        parent = self._make_dispatch(
            status=MessageDispatchStatus.FAILED,
            rendered_body="old body",
            render_checksum="abc",
            policy_version="pol",
        )
        with self.assertRaises(ReplayError):
            replay_dispatch(parent, reason="")
        with self.assertRaises(ReplayError):
            replay_dispatch(parent, reason="NOT_A_REASON")

        child = replay_dispatch(parent, reason=MessageReplayReason.BUGFIX)
        self.assertEqual(child.parent_dispatch_id, parent.pk)
        self.assertEqual(child.replay_reason, MessageReplayReason.BUGFIX)
        self.assertEqual(child.trigger, MessageTriggerKind.MANUAL)
        self.assertEqual(child.schedule_strategy, MessageScheduleStrategy.IMMEDIATE)
        self.assertEqual(child.status, MessageDispatchStatus.QUEUED)
        self.assertEqual(child.render_checksum, "")
        self.assertEqual(child.rendered_body, "")
        self.assertEqual(child.recipient_email, parent.recipient_email)
        event_types = set(
            child.events.values_list("event_type", flat=True)
        )
        self.assertIn(MessageDispatchEventType.REPLAYED, event_types)
        self.assertIn(MessageDispatchEventType.DISPATCH_CREATED, event_types)


class MessagingSkipExpireTests(_MessagingFixturesMixin, TestCase):
    def setUp(self):
        self._bootstrap()
        self._seed_tenant_property_reservation(slug_prefix="skip")
        self._register_providers(_OkProvider("booking"), _OkProvider("email"))

    def tearDown(self):
        self._teardown_engine()

    def test_expired_dispatch_is_skipped_not_sent(self):
        dispatch = self._make_dispatch(
            expires_at=timezone.now() - timedelta(minutes=5),
        )
        outcome = dispatch_one(dispatch)
        self.assertEqual(outcome.status, MessageDispatchStatus.SKIPPED)
        self.assertEqual(outcome.skip_reason, "expired")
        dispatch.refresh_from_db()
        self.assertEqual(dispatch.status, MessageDispatchStatus.SKIPPED)
        self.assertEqual(
            MessageDeliveryAttempt.objects.filter(dispatch=dispatch).count(),
            0,
        )


class MessagingHealthTests(_MessagingFixturesMixin, TestCase):
    def setUp(self):
        self._bootstrap()
        self._seed_tenant_property_reservation(slug_prefix="health")

    def tearDown(self):
        self._teardown_engine()

    @override_settings(
        MESSAGE_ORCHESTRATION_ENABLED=True,
        MESSAGE_ORCHESTRATION_SHADOW=False,
        MESSAGE_ORCHESTRATION_TENANTS=["health-tenant"],
        MESSAGE_ORCHESTRATION_PROPERTIES=[],
    )
    def test_health_snapshot_inventory_and_outbox(self):
        planned = self._make_dispatch(status=MessageDispatchStatus.PLANNED)
        queued = self._make_dispatch(
            definition_key="CHECKIN_LINK",
            status=MessageDispatchStatus.QUEUED,
        )
        MessageDeliveryAttempt.objects.create(
            tenant=self.tenant,
            dispatch=planned,
            channel="booking",
            provider="booking",
            attempt_number=1,
            success=True,
            duration_ms=4,
        )
        MessageDeliveryAttempt.objects.create(
            tenant=self.tenant,
            dispatch=queued,
            channel="email",
            provider="email",
            attempt_number=1,
            success=False,
            duration_ms=9,
            error_category=MessageErrorCategory.NETWORK,
            error_code="timeout",
            error_message="boom",
            retryable=True,
        )

        snap = messaging_health_snapshot(include_queue=True)
        self.assertGreaterEqual(snap["definitions"]["count"], 3)
        self.assertIn("CHECKIN_INFO", snap["definitions"]["keys"])
        self.assertGreaterEqual(snap["templates"]["count"], 1)
        self.assertGreaterEqual(snap["providers"]["count"], 1)
        self.assertTrue(snap["providers"]["items"][0]["capabilities"]["channels"])
        self.assertGreaterEqual(snap["plans"]["count"], 1)
        self.assertEqual(snap["outbox"]["planned"], 1)
        self.assertEqual(snap["outbox"]["queued"], 1)
        self.assertEqual(snap["outbox"]["depth"], 2)
        self.assertIsNotNone(snap["last_success_at"])
        self.assertIsNotNone(snap["last_failure_at"])
        self.assertTrue(snap["flags"]["enabled"])
        self.assertFalse(snap["flags"]["shadow"])
        self.assertIn("welcome_templates", snap)
        wt = snap["welcome_templates"]
        self.assertIn("configured", wt)
        self.assertIn("missing_in_config", wt)
        self.assertIn("status", wt)
        self.assertIn(wt["status"], ("healthy", "warning", "critical"))

    def test_system_status_includes_messaging_block(self):
        self._make_dispatch(status=MessageDispatchStatus.PLANNED)
        payload = build_system_status_payload()
        self.assertEqual(payload["schema_version"], 4)
        messaging = payload["messaging"]
        self.assertIn("conversation", payload)
        self.assertNotIn("last_webhook_at", messaging)
        self.assertIn("definitions", messaging)
        self.assertIn("outbox", messaging)
        self.assertIn("welcome_templates", messaging)
        self.assertGreaterEqual(messaging["outbox"]["planned"], 1)


class MessagingSkipLockedClaimTests(_MessagingFixturesMixin, TransactionTestCase):
    """SKIP LOCKED concurrency requires TransactionTestCase (real commits)."""

    def setUp(self):
        self._bootstrap()
        self._seed_tenant_property_reservation(slug_prefix="lock")

    def tearDown(self):
        self._teardown_engine()

    def _claim_ids(self, *, limit: int = 50) -> list[int]:
        close_old_connections()
        try:
            claimed = claim_due_dispatches(
                limit=limit,
                tenant_id=self.tenant.pk,
            )
            return [row.pk for row in claimed]
        finally:
            close_old_connections()

    def test_concurrent_claim_each_dispatch_once(self):
        rows = [self._make_dispatch() for _ in range(8)]
        expected_ids = {row.pk for row in rows}

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(self._claim_ids, limit=8) for _ in range(4)]
            results = [f.result() for f in as_completed(futures)]

        combined = [pk for batch in results for pk in batch]
        self.assertEqual(len(combined), len(set(combined)))
        self.assertEqual(set(combined), expected_ids)
        statuses = set(
            MessageDispatch.objects.filter(pk__in=expected_ids).values_list(
                "status", flat=True
            )
        )
        self.assertEqual(statuses, {MessageDispatchStatus.DISPATCHING})

    def test_claim_respects_limit_under_contention(self):
        for _ in range(6):
            self._make_dispatch()

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(self._claim_ids, limit=2)
            f2 = pool.submit(self._claim_ids, limit=2)
            a, b = f1.result(), f2.result()

        self.assertLessEqual(len(a), 2)
        self.assertLessEqual(len(b), 2)
        self.assertEqual(len(set(a) & set(b)), 0)
        self.assertEqual(len(a) + len(b), 4)


class MessagingCreatePlannedDedupeTests(_MessagingFixturesMixin, TestCase):
    def setUp(self):
        self._bootstrap()
        self._seed_tenant_property_reservation(slug_prefix="create")

    def tearDown(self):
        self._teardown_engine()

    def test_create_planned_dispatch_returns_none_when_duplicate(self):
        from datetime import date, datetime
        from zoneinfo import ZoneInfo

        zagreb = ZoneInfo("Europe/Zagreb")
        now = datetime(2026, 8, 1, 10, 0, tzinfo=zagreb)
        self.reservation.check_in = date(2026, 8, 8)
        self.reservation.save(update_fields=["check_in", "updated_at"])
        plan = plan_registry.get(PLAN_PRE_ARRIVAL)
        definition = definition_registry.get("CHECKIN_INFO")
        schedule = resolve_schedule_for_plan(
            plan.schedule_prefix,
            property=self.property,
            tenant=self.tenant,
        )
        computed = compute_due(
            check_in=self.reservation.check_in,
            schedule=schedule,
            timezone_name="Europe/Zagreb",
            now=now,
        )
        first = create_planned_dispatch(
            reservation=self.reservation,
            definition=definition,
            plan=plan,
            schedule=schedule,
            computed=computed,
            now=now,
        )
        self.assertIsNotNone(first)
        second = create_planned_dispatch(
            reservation=self.reservation,
            definition=definition,
            plan=plan,
            schedule=schedule,
            computed=computed,
            now=now,
        )
        self.assertIsNone(second)
