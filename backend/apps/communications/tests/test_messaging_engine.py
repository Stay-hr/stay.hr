"""Phase 3 messaging engine unit/integration tests (ADR 0010)."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.communications.messaging.bootstrap import (
    bootstrap_messaging_engine,
    reset_messaging_engine_for_tests,
)
from apps.communications.messaging.context import TriggerContext
from apps.communications.messaging.definitions import (
    ChannelPolicy,
    MessageDefinition,
    compute_policy_version,
    definition_registry,
)
from apps.communications.messaging.dispatcher import dispatch_one
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
from apps.communications.messaging.replay import ReplayError, replay_dispatch
from apps.communications.messaging.results import DeliveryResult
from apps.communications.messaging.scheduler import claim_due_dispatches
from apps.communications.messaging.skip_rules import skip_rule_engine
from apps.communications.messaging.templates import (
    compute_render_checksum,
)
from apps.communications.messaging.triggers import Trigger
from apps.communications.messaging.validation import (
    MessagingValidationError,
    validate_messaging_engine,
)
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant


class RenderChecksumTests(SimpleTestCase):
    def test_checksum_stable_across_newline_normalization(self):
        a = compute_render_checksum("hello\r\nworld", "Sub")
        b = compute_render_checksum("hello\nworld", "Sub")
        self.assertEqual(a, b)
        self.assertEqual(len(a), 64)

    def test_policy_version_includes_definition_and_order(self):
        v1 = compute_policy_version(
            definition_key="CHECKIN_INFO",
            providers=("booking", "email"),
        )
        v2 = compute_policy_version(
            definition_key="CHECKIN_INFO",
            providers=("email", "booking"),
        )
        v3 = compute_policy_version(
            definition_key="CHECKIN_LINK",
            providers=("booking", "email"),
        )
        self.assertNotEqual(v1, v2)
        self.assertNotEqual(v1, v3)


class StartupValidationTests(SimpleTestCase):
    def setUp(self):
        reset_messaging_engine_for_tests()

    def tearDown(self):
        reset_messaging_engine_for_tests()
        bootstrap_messaging_engine(force=True, validate=True)

    def test_bootstrap_passes_validation(self):
        bootstrap_messaging_engine(force=True, validate=True)
        report = validate_messaging_engine(raise_on_error=False)
        self.assertTrue(report.ok)
        self.assertIn("CHECKIN_INFO", definition_registry.keys())
        self.assertIn("booking", provider_registry.names())

    def test_rejects_missing_provider(self):
        bootstrap_messaging_engine(force=True, validate=True)
        definition_registry.clear()
        definition_registry.register(
            MessageDefinition(
                key="ORPHAN",
                template_version="orphan@v1",
                channel_policy=ChannelPolicy(providers=("sms",)),
                renderer_key="CHECKIN_INFO",
            )
        )
        with self.assertRaises(MessagingValidationError) as ctx:
            validate_messaging_engine(raise_on_error=True)
        self.assertIn("sms", str(ctx.exception))

    def test_rejects_duplicate_definition_on_register(self):
        bootstrap_messaging_engine(force=True, validate=True)
        with self.assertRaises(ValueError):
            definition_registry.register(
                MessageDefinition(
                    key="CHECKIN_INFO",
                    template_version="dup@v1",
                    channel_policy=ChannelPolicy(providers=("booking",)),
                    renderer_key="CHECKIN_INFO",
                )
            )


class SkipRuleTests(SimpleTestCase):
    def setUp(self):
        reset_messaging_engine_for_tests()
        bootstrap_messaging_engine(force=True, validate=True)

    def tearDown(self):
        reset_messaging_engine_for_tests()
        bootstrap_messaging_engine(force=True, validate=True)

    def test_expired_rule_blocks(self):
        dispatch = MagicMock()
        dispatch.expires_at = timezone.now() - timedelta(minutes=1)
        dispatch.archived_at = None
        ctx = TriggerContext(
            reservation_id=1,
            tenant_id=1,
            trigger=Trigger.time(source="test"),
            now=timezone.now(),
        )
        decision = skip_rule_engine.can_send(
            dispatch, ctx, rule_names=("expired", "archived")
        )
        self.assertFalse(decision.can_send)
        self.assertEqual(decision.reason, "expired")


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
            provider_message_id="msg-1",
            duration_ms=5,
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
            error_code="nope",
            error_message="failed",
            retryable=True,
        )


class _SlowProvider(MessageProvider):
    def __init__(self, name: str = "booking"):
        self.name = name
        self.channel = name
        self.timeout_seconds = 0.05
        self.capabilities = ProviderCapabilities(channels=frozenset({name}))

    def send(self, dispatch, ctx) -> DeliveryResult:
        import time as _time

        _time.sleep(2.0)
        return DeliveryResult.ok(
            provider=self.name,
            channel=self.channel,
            provider_message_id="too-late",
        )


class _BadReturnProvider(MessageProvider):
    def __init__(self, name: str = "booking"):
        self.name = name
        self.channel = name
        self.timeout_seconds = 5.0
        self.capabilities = ProviderCapabilities(channels=frozenset({name}))

    def send(self, dispatch, ctx):
        return True  # intentional contract violation


class _BoomMiddleware:
    def before_dispatch(self, dispatch, ctx):
        raise RuntimeError("before boom")

    def after_dispatch(self, dispatch, ctx, outcome):
        raise RuntimeError("after boom")


class MessagingDispatcherTests(TestCase):
    def setUp(self):
        reset_messaging_engine_for_tests()
        bootstrap_messaging_engine(force=True, validate=True)
        self.tenant = Tenant.objects.create(
            name="Msg Engine",
            slug="msg-engine",
            default_language="en",
        )
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Villa",
            slug="villa",
            timezone="Europe/Zagreb",
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booker_name="Test Guest",
            status=Reservation.Status.EXPECTED,
            check_in=timezone.localdate() + timedelta(days=7),
            check_out=timezone.localdate() + timedelta(days=10),
        )

    def tearDown(self):
        reset_messaging_engine_for_tests()
        bootstrap_messaging_engine(force=True, validate=True)

    def _make_dispatch(self, **overrides) -> MessageDispatch:
        now = timezone.now()
        defaults = dict(
            tenant=self.tenant,
            reservation=self.reservation,
            definition_key="CHECKIN_INFO",
            plan_key="pre_arrival",
            trigger=MessageTriggerKind.TIME,
            due_at=now - timedelta(minutes=1),
            timezone="Europe/Zagreb",
            local_due_at=now - timedelta(minutes=1),
            schedule_strategy=MessageScheduleStrategy.FIXED_TIME,
            status=MessageDispatchStatus.QUEUED,
        )
        defaults.update(overrides)
        return MessageDispatch.objects.create(**defaults)

    def test_claim_due_uses_dispatching_status(self):
        d = self._make_dispatch()
        claimed = claim_due_dispatches(limit=10)
        self.assertEqual(len(claimed), 1)
        d.refresh_from_db()
        self.assertEqual(d.status, MessageDispatchStatus.DISPATCHING)

    def test_dispatch_fallback_keeps_render_checksum(self):
        # Replace booking (fail) + email (ok) for CHECKIN_INFO policy.
        provider_registry.clear()
        provider_registry.register(_FailProvider("booking"))
        provider_registry.register(_OkProvider("email"))
        # WhatsApp stub still required for WELCOME definition validation.
        from apps.communications.messaging.providers.stubs import StubProvider

        provider_registry.register(
            StubProvider(name="whatsapp", channel="whatsapp")
        )

        dispatch = self._make_dispatch()
        outcome = dispatch_one(dispatch)
        self.assertEqual(outcome.status, MessageDispatchStatus.DELIVERED)
        dispatch.refresh_from_db()
        self.assertTrue(dispatch.fallback_used)
        self.assertTrue(dispatch.render_checksum)
        self.assertTrue(dispatch.policy_version)
        attempts = list(
            MessageDeliveryAttempt.objects.filter(dispatch=dispatch).order_by(
                "attempt_number"
            )
        )
        self.assertEqual(len(attempts), 2)
        self.assertFalse(attempts[0].success)
        self.assertEqual(attempts[0].error_category, MessageErrorCategory.PROVIDER)
        self.assertIsNotNone(attempts[0].duration_ms)
        self.assertTrue(attempts[1].success)
        events = list(
            MessageDispatchEvent.objects.filter(dispatch=dispatch).values_list(
                "event_type", flat=True
            )
        )
        self.assertIn(MessageDispatchEventType.RENDERED, events)
        self.assertIn(MessageDispatchEventType.FALLBACK, events)
        self.assertIn(MessageDispatchEventType.DELIVERED, events)

    def test_replay_requires_reason_and_sets_lineage(self):
        parent = self._make_dispatch(status=MessageDispatchStatus.FAILED)
        with self.assertRaises(ReplayError):
            replay_dispatch(parent, reason="")
        child = replay_dispatch(
            parent,
            reason=MessageReplayReason.SUPPORT,
        )
        self.assertEqual(child.parent_dispatch_id, parent.pk)
        self.assertEqual(child.replay_reason, MessageReplayReason.SUPPORT)
        self.assertEqual(child.trigger, MessageTriggerKind.MANUAL)
        self.assertEqual(child.schedule_strategy, MessageScheduleStrategy.IMMEDIATE)

    def _register_channel_providers(self, *providers: MessageProvider) -> None:
        from apps.communications.messaging.providers.stubs import StubProvider

        provider_registry.clear()
        for provider in providers:
            provider_registry.register(provider)
        if not provider_registry.has("whatsapp"):
            provider_registry.register(
                StubProvider(name="whatsapp", channel="whatsapp")
            )

    def test_middleware_exception_does_not_crash_dispatch(self):
        from apps.communications.messaging.middleware import middleware_registry

        self._register_channel_providers(_OkProvider("booking"), _OkProvider("email"))
        middleware_registry.register(_BoomMiddleware())
        dispatch = self._make_dispatch()
        outcome = dispatch_one(dispatch)
        self.assertEqual(outcome.status, MessageDispatchStatus.DELIVERED)

    def test_provider_timeout_becomes_failed_attempt(self):
        self._register_channel_providers(
            _SlowProvider("booking"),
            _OkProvider("email"),
        )
        dispatch = self._make_dispatch()
        outcome = dispatch_one(dispatch)
        self.assertEqual(outcome.status, MessageDispatchStatus.DELIVERED)
        first = (
            MessageDeliveryAttempt.objects.filter(dispatch=dispatch)
            .order_by("attempt_number")
            .first()
        )
        self.assertIsNotNone(first)
        self.assertFalse(first.success)
        self.assertEqual(first.error_code, "provider_timeout")
        self.assertEqual(first.error_category, MessageErrorCategory.NETWORK)

    def test_invalid_provider_return_becomes_delivery_result_fail(self):
        self._register_channel_providers(
            _BadReturnProvider("booking"),
            _OkProvider("email"),
        )
        dispatch = self._make_dispatch()
        outcome = dispatch_one(dispatch)
        self.assertEqual(outcome.status, MessageDispatchStatus.DELIVERED)
        first = (
            MessageDeliveryAttempt.objects.filter(dispatch=dispatch)
            .order_by("attempt_number")
            .first()
        )
        self.assertEqual(first.error_code, "invalid_delivery_result")

    def test_alert_throttle_dedupes_burst(self):
        from apps.communications.messaging.alerts import alert_all_providers_failed
        from django.test import override_settings

        self._register_channel_providers(
            _FailProvider("booking"),
            _FailProvider("email"),
        )
        dispatch = self._make_dispatch()
        with override_settings(MESSAGING_ALERT_THROTTLE_SECONDS=60):
            outcome = dispatch_one(dispatch)
            self.assertEqual(outcome.status, MessageDispatchStatus.FAILED)
            # Second alert with same key should be throttled.
            emitted = alert_all_providers_failed(dispatch, outcome.results)
            self.assertFalse(emitted)
