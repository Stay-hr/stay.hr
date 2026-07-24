"""Phase 5: schedule resolve + TIME materialization (ADR 0010)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase, TestCase

from apps.communications.messaging.bootstrap import (
    bootstrap_messaging_engine,
    reset_messaging_engine_for_tests,
)
from apps.communications.messaging.models import (
    MessageDispatch,
    MessageDispatchEventType,
    MessageDispatchStatus,
    MessageScheduleStrategy,
    MessageTriggerKind,
)
from apps.communications.messaging.plans import PLAN_PRE_ARRIVAL, PLAN_WELCOME, plan_registry
from apps.communications.messaging.schedule_settings import (
    PLATFORM_SCHEDULE_DEFAULTS,
    PREFIX_PRE_ARRIVAL,
    ResolvedSchedule,
    compute_due,
    resolve_schedule,
    resolve_schedule_for_plan,
)
from apps.communications.messaging.scheduler import (
    cancel_stale_time_dispatches,
    claim_due_dispatches,
    materialize_time_triggers,
    run_scheduler_cycle,
)
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant, TenantReceptionSettings


ZAGREB = ZoneInfo("Europe/Zagreb")


class ScheduleResolveTests(SimpleTestCase):
    def test_platform_defaults_pre_arrival(self):
        resolved = resolve_schedule(prefixes=(PREFIX_PRE_ARRIVAL,))
        defaults = PLATFORM_SCHEDULE_DEFAULTS[PREFIX_PRE_ARRIVAL]
        self.assertEqual(resolved.days_before, defaults.days_before)
        self.assertEqual(resolved.send_time, defaults.send_time)
        self.assertEqual(resolved.schedule_strategy, defaults.schedule_strategy)
        self.assertTrue(resolved.days_before_source.startswith("platform:"))

    def test_property_overrides_tenant_and_platform(self):
        tenant = Tenant(name="T", slug="t-sched")
        settings_row = TenantReceptionSettings(
            tenant=tenant,
            pre_arrival_days_before=3,
            pre_arrival_send_time=time(10, 0),
        )
        prop = Property(
            tenant=tenant,
            name="P",
            slug="p",
            pre_arrival_days_before=5,
            pre_arrival_send_time=time(8, 30),
            pre_arrival_schedule_strategy=MessageScheduleStrategy.FIRST_AFTER,
        )
        resolved = resolve_schedule(
            prefixes=(PREFIX_PRE_ARRIVAL,),
            property=prop,
            tenant=tenant,
            reception_settings=settings_row,
        )
        self.assertEqual(resolved.days_before, 5)
        self.assertEqual(resolved.send_time, time(8, 30))
        self.assertEqual(resolved.schedule_strategy, MessageScheduleStrategy.FIRST_AFTER)
        self.assertEqual(resolved.days_before_source, "property:pre_arrival")
        self.assertEqual(resolved.send_time_source, "property:pre_arrival")

    def test_tenant_overrides_platform_when_property_null(self):
        tenant = Tenant(name="T", slug="t-sched2")
        settings_row = TenantReceptionSettings(
            tenant=tenant,
            pre_arrival_days_before=2,
            pre_arrival_send_time=time(7, 0),
            pre_arrival_schedule_strategy=MessageScheduleStrategy.FIXED_TIME,
        )
        prop = Property(tenant=tenant, name="P", slug="p2")
        resolved = resolve_schedule(
            prefixes=(PREFIX_PRE_ARRIVAL,),
            property=prop,
            tenant=tenant,
            reception_settings=settings_row,
        )
        self.assertEqual(resolved.days_before, 2)
        self.assertEqual(resolved.send_time, time(7, 0))
        self.assertEqual(resolved.days_before_source, "tenant:pre_arrival")

    def test_welcome_ignores_legacy_welcome_fields_uses_platform(self):
        """Only tenant welcome_* set and whatsapp_welcome_* null → platform 0d @ 11:15."""
        tenant = Tenant(name="T", slug="t-wel")
        settings_row = TenantReceptionSettings(
            tenant=tenant,
            welcome_days_before=1,
            welcome_send_time=time(12, 0),
        )
        prop = Property(tenant=tenant, name="P", slug="p-wel")
        resolved = resolve_schedule_for_plan(
            "whatsapp_welcome",
            property=prop,
            tenant=tenant,
            reception_settings=settings_row,
        )
        defaults = PLATFORM_SCHEDULE_DEFAULTS["whatsapp_welcome"]
        self.assertEqual(resolved.days_before, defaults.days_before)
        self.assertEqual(resolved.send_time, defaults.send_time)
        self.assertEqual(resolved.days_before_source, "platform:whatsapp_welcome")
        self.assertEqual(resolved.send_time_source, "platform:whatsapp_welcome")
        self.assertEqual(list(resolved.resolve_prefixes), ["whatsapp_welcome"])

    def test_whatsapp_welcome_tenant_wins_over_platform(self):
        tenant = Tenant(name="T", slug="t-wa-wel")
        settings_row = TenantReceptionSettings(
            tenant=tenant,
            whatsapp_welcome_days_before=0,
            whatsapp_welcome_send_time=time(10, 30),
        )
        prop = Property(tenant=tenant, name="P", slug="p-wa-wel")
        resolved = resolve_schedule_for_plan(
            "whatsapp_welcome",
            property=prop,
            tenant=tenant,
            reception_settings=settings_row,
        )
        self.assertEqual(resolved.days_before, 0)
        self.assertEqual(resolved.send_time, time(10, 30))
        self.assertEqual(resolved.days_before_source, "tenant:whatsapp_welcome")
        self.assertEqual(resolved.send_time_source, "tenant:whatsapp_welcome")

    def test_legacy_autocheckin_time_when_welcome_enabled(self):
        tenant = Tenant(name="T", slug="t-leg")
        prop = Property(
            tenant=tenant,
            name="P",
            slug="p-leg",
            whatsapp_autocheckin_enabled=True,
            whatsapp_autocheckin_time=time(11, 15),
            whatsapp_welcome_send_time=None,
        )
        resolved = resolve_schedule_for_plan(
            "whatsapp_welcome",
            property=prop,
            tenant=tenant,
        )
        self.assertEqual(resolved.send_time, time(11, 15))
        self.assertEqual(resolved.send_time_source, "property:whatsapp_autocheckin_time")


class ComputeDueStrategyTests(SimpleTestCase):
    def _schedule(self, **overrides) -> ResolvedSchedule:
        base = dict(
            days_before=7,
            send_time=time(9, 0),
            schedule_strategy=MessageScheduleStrategy.FIXED_TIME,
            days_before_source="platform",
            send_time_source="platform",
            schedule_strategy_source="platform",
            resolve_prefixes=(PREFIX_PRE_ARRIVAL,),
        )
        base.update(overrides)
        return ResolvedSchedule(**base)

    def test_fixed_time_uses_local_wall_clock(self):
        check_in = date(2026, 8, 10)
        now = datetime(2026, 7, 24, 12, 0, tzinfo=ZAGREB)
        computed = compute_due(
            check_in=check_in,
            schedule=self._schedule(),
            timezone_name="Europe/Zagreb",
            now=now,
        )
        self.assertEqual(computed.target_local_date, date(2026, 8, 3))
        self.assertEqual(computed.local_due_at.hour, 9)
        self.assertEqual(computed.local_due_at.minute, 0)
        self.assertEqual(computed.timezone, "Europe/Zagreb")
        self.assertEqual(
            computed.due_at,
            computed.local_due_at.astimezone(ZoneInfo("UTC")),
        )

    def test_first_after_uses_now_when_past_floor(self):
        check_in = date(2026, 8, 10)
        now = datetime(2026, 8, 3, 10, 30, tzinfo=ZAGREB)
        computed = compute_due(
            check_in=check_in,
            schedule=self._schedule(
                schedule_strategy=MessageScheduleStrategy.FIRST_AFTER,
            ),
            timezone_name="Europe/Zagreb",
            now=now,
        )
        self.assertEqual(computed.schedule_strategy, MessageScheduleStrategy.FIRST_AFTER)
        self.assertEqual(computed.local_due_at, now)

    def test_first_after_uses_floor_when_before_send_time(self):
        check_in = date(2026, 8, 10)
        now = datetime(2026, 8, 3, 7, 0, tzinfo=ZAGREB)
        computed = compute_due(
            check_in=check_in,
            schedule=self._schedule(
                schedule_strategy=MessageScheduleStrategy.FIRST_AFTER,
            ),
            timezone_name="Europe/Zagreb",
            now=now,
        )
        self.assertEqual(computed.local_due_at.hour, 9)
        self.assertEqual(computed.local_due_at.date(), date(2026, 8, 3))

    def test_immediate_is_now(self):
        check_in = date(2026, 8, 10)
        now = datetime(2026, 7, 24, 15, 45, tzinfo=ZAGREB)
        computed = compute_due(
            check_in=check_in,
            schedule=self._schedule(
                schedule_strategy=MessageScheduleStrategy.IMMEDIATE,
            ),
            timezone_name="Europe/Zagreb",
            now=now,
        )
        self.assertEqual(computed.local_due_at, now)

    def test_timezone_snapshot_differs_across_zones(self):
        check_in = date(2026, 8, 10)
        now = datetime(2026, 7, 24, 12, 0, tzinfo=ZAGREB)
        computed = compute_due(
            check_in=check_in,
            schedule=self._schedule(),
            timezone_name="Europe/Zagreb",
            now=now,
        )
        other = compute_due(
            check_in=check_in,
            schedule=self._schedule(),
            timezone_name="America/New_York",
            now=now,
        )
        self.assertNotEqual(computed.due_at, other.due_at)
        self.assertEqual(computed.timezone, "Europe/Zagreb")


class MaterializeTimeTriggersTests(TestCase):
    def setUp(self):
        reset_messaging_engine_for_tests()
        bootstrap_messaging_engine(force=True, validate=True)
        self.tenant = Tenant.objects.create(
            name="Sched Tenant",
            slug="sched-tenant",
            default_language="en",
            timezone="Europe/Zagreb",
        )
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Villa Sched",
            slug="villa-sched",
            timezone="Europe/Zagreb",
            whatsapp_autocheckin_enabled=True,
            whatsapp_autocheckin_time=time(11, 15),
            whatsapp_welcome_send_time=time(11, 15),
        )

    def tearDown(self):
        reset_messaging_engine_for_tests()
        bootstrap_messaging_engine(force=True, validate=True)

    def _make_reservation(self, *, check_in: date, **overrides) -> Reservation:
        defaults = dict(
            tenant=self.tenant,
            property=self.property,
            booker_name="Guest",
            booker_email="guest@example.com",
            booker_phone="+38591111222",
            status=Reservation.Status.EXPECTED,
            check_in=check_in,
            check_out=check_in + timedelta(days=3),
        )
        defaults.update(overrides)
        return Reservation.objects.create(**defaults)

    def test_materialize_pre_arrival_creates_info_and_link(self):
        # Platform default: 7d @ 09:00 FIXED_TIME.
        # Freeze "now" to 2026-08-01 10:00 Zagreb → target check_in 2026-08-08.
        now = datetime(2026, 8, 1, 10, 0, tzinfo=ZAGREB)
        reservation = self._make_reservation(check_in=date(2026, 8, 8))

        created = materialize_time_triggers(
            now=now,
            property_id=self.property.pk,
            plans=[plan_registry.get(PLAN_PRE_ARRIVAL)],
        )
        self.assertEqual(created, 2)
        rows = list(
            MessageDispatch.objects.filter(reservation=reservation).order_by(
                "definition_key"
            )
        )
        self.assertEqual(
            [r.definition_key for r in rows],
            ["CHECKIN_INFO", "CHECKIN_LINK"],
        )
        for row in rows:
            self.assertEqual(row.trigger, MessageTriggerKind.TIME)
            self.assertEqual(row.plan_key, PLAN_PRE_ARRIVAL)
            self.assertEqual(row.status, MessageDispatchStatus.PLANNED)
            self.assertEqual(row.timezone, "Europe/Zagreb")
            self.assertEqual(row.schedule_strategy, MessageScheduleStrategy.FIXED_TIME)
            self.assertEqual(row.recipient_email, "guest@example.com")
            self.assertEqual(row.recipient_phone, "+38591111222")
            local = row.local_due_at.astimezone(ZAGREB)
            self.assertEqual(local.date(), date(2026, 8, 1))
            self.assertEqual(local.hour, 9)

        created_again = materialize_time_triggers(
            now=now,
            property_id=self.property.pk,
            plans=[plan_registry.get(PLAN_PRE_ARRIVAL)],
        )
        self.assertEqual(created_again, 0)
        self.assertEqual(
            MessageDispatch.objects.filter(reservation=reservation).count(),
            2,
        )

    def test_materialize_welcome_requires_autocheckin_enabled(self):
        now = datetime(2026, 8, 1, 12, 0, tzinfo=ZAGREB)
        reservation = self._make_reservation(check_in=date(2026, 8, 1))
        self.property.whatsapp_autocheckin_enabled = False
        self.property.save(update_fields=["whatsapp_autocheckin_enabled"])

        created = materialize_time_triggers(
            now=now,
            property_id=self.property.pk,
            plans=[plan_registry.get(PLAN_WELCOME)],
        )
        self.assertEqual(created, 0)

        self.property.whatsapp_autocheckin_enabled = True
        self.property.save(update_fields=["whatsapp_autocheckin_enabled"])
        created = materialize_time_triggers(
            now=now,
            property_id=self.property.pk,
            plans=[plan_registry.get(PLAN_WELCOME)],
        )
        self.assertEqual(created, 1)
        row = MessageDispatch.objects.get(reservation=reservation)
        self.assertEqual(row.definition_key, "WELCOME")
        self.assertEqual(row.plan_key, PLAN_WELCOME)

    def test_timezone_snapshot_zagreb_to_london_keeps_due_at(self):
        """Property TZ change must not rewrite frozen dispatch timing (ADR §4.B)."""
        now = datetime(2026, 8, 1, 10, 0, tzinfo=ZAGREB)
        reservation = self._make_reservation(check_in=date(2026, 8, 8))
        materialize_time_triggers(
            now=now,
            property_id=self.property.pk,
            plans=[plan_registry.get(PLAN_PRE_ARRIVAL)],
        )
        row = MessageDispatch.objects.filter(
            reservation=reservation, definition_key="CHECKIN_INFO"
        ).get()
        frozen_due = row.due_at
        frozen_local = row.local_due_at
        frozen_tz = row.timezone
        self.assertEqual(frozen_tz, "Europe/Zagreb")
        # Pre-arrival FIXED_TIME: 2026-08-01 09:00 Europe/Zagreb.
        local = frozen_local.astimezone(ZAGREB)
        self.assertEqual(local.hour, 9)
        self.assertEqual(local.minute, 0)

        self.property.timezone = "Europe/London"
        self.property.save(update_fields=["timezone"])
        row.refresh_from_db()
        self.assertEqual(row.due_at, frozen_due)
        self.assertEqual(row.local_due_at, frozen_local)
        self.assertEqual(row.timezone, "Europe/Zagreb")

        # Rematerialize must dedupe — must not create a London-recomputed twin.
        created_again = materialize_time_triggers(
            now=now,
            property_id=self.property.pk,
            plans=[plan_registry.get(PLAN_PRE_ARRIVAL)],
        )
        self.assertEqual(created_again, 0)
        self.assertEqual(
            MessageDispatch.objects.filter(
                reservation=reservation, definition_key="CHECKIN_INFO"
            ).count(),
            1,
        )

    def test_clock_skew_late_worker_still_claims_fixed_time(self):
        """Worker 17 min late: FIXED_TIME 09:00 is still due and claimable."""
        # Materialize at 08:50 — due_at = 09:00 same day.
        materialize_at = datetime(2026, 8, 1, 8, 50, tzinfo=ZAGREB)
        reservation = self._make_reservation(check_in=date(2026, 8, 8))
        created = materialize_time_triggers(
            now=materialize_at,
            property_id=self.property.pk,
            plans=[plan_registry.get(PLAN_PRE_ARRIVAL)],
        )
        self.assertEqual(created, 2)
        row = MessageDispatch.objects.filter(
            reservation=reservation, definition_key="CHECKIN_INFO"
        ).get()
        local_due = row.local_due_at.astimezone(ZAGREB)
        self.assertEqual(local_due.hour, 9)
        self.assertEqual(local_due.minute, 0)

        # Too early — not claimable yet.
        early = claim_due_dispatches(
            now=datetime(2026, 8, 1, 8, 59, tzinfo=ZAGREB),
            tenant_id=self.tenant.pk,
        )
        self.assertEqual(early, [])

        # Worker late (09:17): due_at <= now → claim succeeds.
        late = claim_due_dispatches(
            now=datetime(2026, 8, 1, 9, 17, tzinfo=ZAGREB),
            tenant_id=self.tenant.pk,
        )
        self.assertEqual(len(late), 2)
        row.refresh_from_db()
        self.assertEqual(row.status, MessageDispatchStatus.DISPATCHING)
        # Frozen due instant unchanged by late claim.
        self.assertEqual(
            row.local_due_at.astimezone(ZAGREB).replace(tzinfo=None),
            datetime(2026, 8, 1, 9, 0),
        )

    def test_scheduler_cycle_materialize_only_does_not_claim(self):
        now = datetime(2026, 8, 1, 10, 0, tzinfo=ZAGREB)
        # Disable WELCOME so the cycle only plans pre-arrival (2 rows).
        self.property.whatsapp_autocheckin_enabled = False
        self.property.save(update_fields=["whatsapp_autocheckin_enabled"])
        self._make_reservation(check_in=date(2026, 8, 8))
        summary = run_scheduler_cycle(
            now=now,
            property_id=self.property.pk,
            claim=False,
        )
        self.assertEqual(summary["materialized"], 2)
        self.assertEqual(summary["claimed"], 0)
        self.assertEqual(summary["cancelled"], 0)
        statuses = set(
            MessageDispatch.objects.filter(
                reservation__property_id=self.property.pk
            ).values_list("status", flat=True)
        )
        self.assertEqual(statuses, {MessageDispatchStatus.PLANNED})
        self.assertNotIn(MessageDispatchStatus.DISPATCHING, statuses)

    def test_cancel_stale_when_reservation_not_expected(self):
        now = datetime(2026, 8, 1, 10, 0, tzinfo=ZAGREB)
        reservation = self._make_reservation(check_in=date(2026, 8, 8))
        materialize_time_triggers(
            now=now,
            property_id=self.property.pk,
            plans=[plan_registry.get(PLAN_PRE_ARRIVAL)],
        )
        reservation.status = Reservation.Status.CANCELED
        reservation.save(update_fields=["status", "updated_at"])

        cancelled = cancel_stale_time_dispatches(now=now, tenant_id=self.tenant.pk)
        self.assertEqual(cancelled, 2)
        self.assertEqual(
            MessageDispatch.objects.filter(
                reservation=reservation,
                status=MessageDispatchStatus.CANCELLED,
            ).count(),
            2,
        )
        event_types = set(
            MessageDispatch.objects.get(
                reservation=reservation, definition_key="CHECKIN_INFO"
            ).events.values_list("event_type", flat=True)
        )
        self.assertIn(MessageDispatchEventType.DISPATCH_CREATED, event_types)
        self.assertIn(MessageDispatchEventType.CANCELLED, event_types)

    def test_recipient_snapshot_frozen_after_contact_change(self):
        now = datetime(2026, 8, 1, 10, 0, tzinfo=ZAGREB)
        reservation = self._make_reservation(check_in=date(2026, 8, 8))
        materialize_time_triggers(
            now=now,
            property_id=self.property.pk,
            plans=[plan_registry.get(PLAN_PRE_ARRIVAL)],
        )
        row = MessageDispatch.objects.filter(
            reservation=reservation, definition_key="CHECKIN_INFO"
        ).get()
        reservation.booker_email = "changed@example.com"
        reservation.booker_phone = "+38590000000"
        reservation.save(update_fields=["booker_email", "booker_phone", "updated_at"])
        row.refresh_from_db()
        self.assertEqual(row.recipient_email, "guest@example.com")
        self.assertEqual(row.recipient_phone, "+38591111222")
