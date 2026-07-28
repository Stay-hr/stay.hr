"""DispatchPolicy / DeliveryWindowPolicy / quiet-hours defer (ADR 0010)."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.communications.messaging.bootstrap import (
    bootstrap_messaging_engine,
    reset_messaging_engine_for_tests,
)
from apps.communications.messaging.context import TriggerContext
from apps.communications.messaging.definitions import definition_registry
from apps.communications.messaging.dispatch_policy import (
    ChannelQuietWindow,
    DispatchPolicy,
    PolicyDecisionKind,
    is_in_quiet_window,
    next_window_end_local,
)
from apps.communications.messaging.dispatcher import dispatch_one
from apps.communications.messaging.intents import MessageDefinitionKey
from apps.communications.messaging.models import (
    MessageDeliveryAttempt,
    MessageDispatch,
    MessageDispatchEvent,
    MessageDispatchEventType,
    MessageDispatchStatus,
    MessageErrorCategory,
    MessageScheduleStrategy,
    MessageTriggerKind,
)
from apps.communications.messaging.providers.base import (
    MessageProvider,
    ProviderCapabilities,
)
from apps.communications.messaging.providers.registry import provider_registry
from apps.communications.messaging.results import DeliveryResult
from apps.communications.messaging.triggers import Trigger
from apps.communications.models import GuestMessageChannel
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant


ZAGREB = ZoneInfo("Europe/Zagreb")


class QuietWindowUnitTests(SimpleTestCase):
    def setUp(self):
        self.window = ChannelQuietWindow(
            start=time(21, 0),
            end=time(8, 0),
        )

    def test_edges(self):
        cases = [
            (time(20, 59), False),
            (time(21, 0), True),
            (time(23, 30), True),
            (time(0, 30), True),
            (time(7, 59), True),
            (time(8, 0), False),
            (time(12, 0), False),
        ]
        for clock, expect in cases:
            local = datetime(2026, 7, 27, clock.hour, clock.minute, tzinfo=ZAGREB)
            self.assertEqual(
                is_in_quiet_window(local, self.window),
                expect,
                msg=f"clock={clock}",
            )

    def test_next_end_same_morning(self):
        local = datetime(2026, 7, 27, 0, 30, tzinfo=ZAGREB)
        nxt = next_window_end_local(local, self.window)
        self.assertEqual(nxt, datetime(2026, 7, 27, 8, 0, tzinfo=ZAGREB))

    def test_next_end_after_evening_start(self):
        local = datetime(2026, 7, 27, 22, 15, tzinfo=ZAGREB)
        nxt = next_window_end_local(local, self.window)
        self.assertEqual(nxt, datetime(2026, 7, 28, 8, 0, tzinfo=ZAGREB))

    def test_dst_summer_cest(self):
        # CEST (UTC+2): 2026-07-27
        local = datetime(2026, 7, 27, 0, 30, tzinfo=ZAGREB)
        nxt = next_window_end_local(local, self.window)
        self.assertEqual(nxt.hour, 8)
        self.assertEqual(nxt.utcoffset(), timedelta(hours=2))

    def test_dst_winter_cet(self):
        # CET (UTC+1): 2026-01-15
        local = datetime(2026, 1, 15, 0, 30, tzinfo=ZAGREB)
        nxt = next_window_end_local(local, self.window)
        self.assertEqual(nxt, datetime(2026, 1, 15, 8, 0, tzinfo=ZAGREB))
        self.assertEqual(nxt.hour, 8)
        self.assertEqual(nxt.utcoffset(), timedelta(hours=1))


class DispatchPolicyEvaluateTests(SimpleTestCase):
    def test_email_always_allow_at_midnight(self):
        policy = DispatchPolicy()
        dispatch = MessageDispatch(
            timezone="Europe/Zagreb",
        )
        now = datetime(2026, 7, 27, 0, 30, tzinfo=ZAGREB).astimezone(
            ZoneInfo("UTC")
        )
        decision = policy.evaluate(
            dispatch, GuestMessageChannel.EMAIL, now=now
        )
        self.assertEqual(decision.kind, PolicyDecisionKind.ALLOW)

    def test_whatsapp_defers_at_midnight(self):
        policy = DispatchPolicy()
        dispatch = MessageDispatch(timezone="Europe/Zagreb")
        now = datetime(2026, 7, 27, 0, 30, tzinfo=ZAGREB).astimezone(
            ZoneInfo("UTC")
        )
        decision = policy.evaluate(
            dispatch, GuestMessageChannel.WHATSAPP, now=now
        )
        self.assertEqual(decision.kind, PolicyDecisionKind.DEFER)
        self.assertEqual(decision.reason, "quiet_hours")
        self.assertIsNotNone(decision.next_attempt_at)
        local = decision.next_attempt_at.astimezone(ZAGREB)
        self.assertEqual(local.hour, 8)
        self.assertEqual(local.minute, 0)

    def test_whatsapp_allow_at_nine(self):
        policy = DispatchPolicy()
        dispatch = MessageDispatch(timezone="Europe/Zagreb")
        now = datetime(2026, 7, 27, 9, 0, tzinfo=ZAGREB).astimezone(
            ZoneInfo("UTC")
        )
        decision = policy.evaluate(
            dispatch, GuestMessageChannel.WHATSAPP, now=now
        )
        self.assertEqual(decision.kind, PolicyDecisionKind.ALLOW)


class _CountingProvider(MessageProvider):
    def __init__(self, name: str, *, succeed: bool = True):
        self.name = name
        self.channel = name
        self.timeout_seconds = 5.0
        self.capabilities = ProviderCapabilities(channels=frozenset({name}))
        self.calls = 0
        self.succeed = succeed

    def send(self, dispatch, ctx) -> DeliveryResult:
        self.calls += 1
        if self.succeed:
            return DeliveryResult.ok(
                provider=self.name,
                channel=self.channel,
                provider_message_id=f"{self.name}-ok",
            )
        return DeliveryResult.fail(
            provider=self.name,
            channel=self.channel,
            error_category=MessageErrorCategory.PROVIDER,
            error_code="unavailable",
            error_message="fail",
        )


class QuietHoursDispatcherTests(TestCase):
    def setUp(self):
        reset_messaging_engine_for_tests()
        bootstrap_messaging_engine(force=True, validate=True)
        self.tenant = Tenant.objects.create(
            name="Quiet Tenant",
            slug="quiet-tenant",
            default_language="en",
        )
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Villa Quiet",
            slug="villa-quiet",
            timezone="Europe/Zagreb",
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booker_name="Night Guest",
            status=Reservation.Status.EXPECTED,
            check_in=timezone.localdate() + timedelta(days=1),
            check_out=timezone.localdate() + timedelta(days=3),
        )
        self.booking = _CountingProvider("booking", succeed=True)
        self.email = _CountingProvider("email", succeed=True)
        self.whatsapp = _CountingProvider("whatsapp", succeed=True)
        provider_registry.clear()
        for p in (self.booking, self.email, self.whatsapp):
            provider_registry.register(p)

    def tearDown(self):
        reset_messaging_engine_for_tests()
        bootstrap_messaging_engine(force=True, validate=True)

    def _dispatch(self, *, definition_key: str = "CHECKIN_LINK", **kw):
        now = timezone.now()
        defaults = dict(
            tenant=self.tenant,
            reservation=self.reservation,
            definition_key=definition_key,
            plan_key="pre_arrival",
            trigger=MessageTriggerKind.TIME,
            due_at=now - timedelta(minutes=1),
            timezone="Europe/Zagreb",
            local_due_at=now - timedelta(minutes=1),
            schedule_strategy=MessageScheduleStrategy.FIXED_TIME,
            status=MessageDispatchStatus.QUEUED,
            rendered_body="Please complete check-in",
            render_checksum="abc",
            policy_version="test",
        )
        defaults.update(kw)
        return MessageDispatch.objects.create(**defaults)

    def _ctx(self, local_dt: datetime) -> TriggerContext:
        return TriggerContext(
            reservation_id=self.reservation.pk,
            tenant_id=self.tenant.pk,
            property_id=self.property.pk,
            trigger=Trigger.time(source="test"),
            now=local_dt.astimezone(ZoneInfo("UTC")),
        )

    def test_bootstrap_checkin_includes_whatsapp(self):
        info = definition_registry.get(MessageDefinitionKey.CHECKIN_INFO)
        link = definition_registry.get(MessageDefinitionKey.CHECKIN_LINK)
        self.assertEqual(
            info.channel_policy.providers, ("booking", "email", "whatsapp")
        )
        self.assertEqual(
            link.channel_policy.providers, ("booking", "email", "whatsapp")
        )

    def test_booking_wins_at_midnight_no_whatsapp(self):
        row = self._dispatch()
        midnight = datetime(2026, 7, 27, 0, 30, tzinfo=ZAGREB)
        outcome = dispatch_one(row, ctx=self._ctx(midnight))
        self.assertEqual(outcome.status, MessageDispatchStatus.DELIVERED)
        self.assertEqual(self.booking.calls, 1)
        self.assertEqual(self.email.calls, 0)
        self.assertEqual(self.whatsapp.calls, 0)

    def test_defer_when_only_whatsapp_left_in_quiet_hours(self):
        self.booking.succeed = False
        self.email.succeed = False
        row = self._dispatch()
        midnight = datetime(2026, 7, 27, 0, 30, tzinfo=ZAGREB)
        outcome = dispatch_one(row, ctx=self._ctx(midnight))

        self.assertEqual(outcome.status, MessageDispatchStatus.PLANNED)
        self.assertEqual(outcome.defer_reason, "quiet_hours")
        self.assertEqual(self.whatsapp.calls, 0)

        row.refresh_from_db()
        self.assertEqual(row.status, MessageDispatchStatus.PLANNED)
        local_due = row.local_due_at.astimezone(ZAGREB)
        self.assertEqual(local_due.hour, 8)
        self.assertEqual(local_due.minute, 0)

        evt = MessageDispatchEvent.objects.filter(
            dispatch=row, event_type=MessageDispatchEventType.DEFERRED
        ).latest("id")
        self.assertEqual(evt.payload.get("reason"), "quiet_hours")
        self.assertEqual(evt.payload.get("channel"), "whatsapp")
        self.assertEqual(evt.payload.get("timezone"), "Europe/Zagreb")
        self.assertIn("08:00", evt.payload.get("next_attempt_at", ""))

        # Failed attempts recorded for booking+email; not FAILED dispatch.
        fails = MessageDeliveryAttempt.objects.filter(
            dispatch=row, success=False
        )
        self.assertEqual(fails.count(), 2)
        self.assertFalse(
            MessageDispatch.objects.filter(
                pk=row.pk, status=MessageDispatchStatus.FAILED
            ).exists()
        )

    def test_resume_skips_booking_and_email(self):
        self.booking.succeed = False
        self.email.succeed = False
        row = self._dispatch()
        midnight = datetime(2026, 7, 27, 0, 30, tzinfo=ZAGREB)
        dispatch_one(row, ctx=self._ctx(midnight))
        self.assertEqual(self.booking.calls, 1)
        self.assertEqual(self.email.calls, 1)
        self.assertEqual(self.whatsapp.calls, 0)

        row.refresh_from_db()
        # Morning reclaim
        morning = datetime(2026, 7, 27, 8, 5, tzinfo=ZAGREB)
        row.status = MessageDispatchStatus.QUEUED
        row.due_at = morning.astimezone(ZoneInfo("UTC")) - timedelta(minutes=1)
        row.save(update_fields=["status", "due_at", "updated_at"])

        outcome = dispatch_one(row, ctx=self._ctx(morning))
        self.assertEqual(outcome.status, MessageDispatchStatus.DELIVERED)
        # No second booking/email attempts
        self.assertEqual(self.booking.calls, 1)
        self.assertEqual(self.email.calls, 1)
        self.assertEqual(self.whatsapp.calls, 1)

    def test_welcome_defers_in_quiet_hours(self):
        row = self._dispatch(definition_key="WELCOME")
        midnight = datetime(2026, 7, 27, 0, 30, tzinfo=ZAGREB)
        outcome = dispatch_one(row, ctx=self._ctx(midnight))
        self.assertEqual(outcome.status, MessageDispatchStatus.PLANNED)
        self.assertEqual(outcome.defer_reason, "quiet_hours")
        self.assertEqual(self.whatsapp.calls, 0)

    def test_whatsapp_sends_at_nine(self):
        self.booking.succeed = False
        self.email.succeed = False
        row = self._dispatch()
        nine = datetime(2026, 7, 27, 9, 0, tzinfo=ZAGREB)
        outcome = dispatch_one(row, ctx=self._ctx(nine))
        self.assertEqual(outcome.status, MessageDispatchStatus.DELIVERED)
        self.assertEqual(self.whatsapp.calls, 1)
