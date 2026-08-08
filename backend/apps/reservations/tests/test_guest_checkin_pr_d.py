from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.communications.guest_compose import guest_web_checkin_reminder_hint
from apps.communications.guest_reminder_service import GuestReminderService
from apps.communications.models import GuestMessageDraft, GuestMessageIntent
from apps.properties.models import Property
from apps.reservations.guest_checkin_analytics import (
    load_guest_checkin_kpis,
    reservations_due_for_checkin_reminder,
)
from apps.reservations.guest_checkin_events import emit_guest_session_ready
from apps.reservations.guest_checkin_orchestrator import GuestCheckInOrchestrator
from apps.reservations.guest_checkin_session import ensure_active_session, expire_stale_sessions
from apps.reservations.guest_checkin_tasks import (
    expire_guest_checkin_sessions,
    log_guest_checkin_metrics,
    send_pre_arrival_checkin_reminders,
)
from apps.reservations.models import (
    Guest,
    GuestCheckInSessionCreatedFrom,
    GuestCheckInSessionStatus,
    Reservation,
)
from apps.tenants.models import Tenant


def _relative_stay(*, offset_days: int = 0, nights: int = 3):
    """Stay dates relative to local today so sessions stay within the open window."""
    check_in = timezone.localdate() + timedelta(days=offset_days)
    return check_in, check_in + timedelta(days=nights)


_SLOT_FIELDS = {
    "first_name": "Iva",
    "last_name": "Ivić",
    "date_of_birth": "1992-03-04",
    "nationality": "HR",
    "sex": "female",
    "document_number": "99887766",
    "document_type": "identity_card",
    "address": "Zadar, Obala 1",
}


class GuestCheckInReadyAtTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="ReadyAt Tenant", slug="readyat-tenant")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="ReadyAt Property",
            slug="readyat-property",
            guest_checkin_opens_days_before=0,
        )
        check_in, check_out = _relative_stay()
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="RA-001",
            check_in=check_in,
            check_out=check_out,
            adults_count=1,
            booker_name="Test Guest",
            amount=Decimal("100.00"),
        )
        Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="Ana",
            last_name="Anić",
            name="Ana Anić",
            is_primary=True,
        )
        self.session = ensure_active_session(
            self.reservation,
            created_from=GuestCheckInSessionCreatedFrom.EMAIL,
        )

    def test_ready_at_set_once_on_session_ready(self):
        self.assertIsNone(self.session.ready_at)
        emit_guest_session_ready(session=self.session, reservation=self.reservation)
        self.session.refresh_from_db()
        first = self.session.ready_at
        self.assertIsNotNone(first)

        # Idempotent: second emit must not overwrite ready_at.
        emit_guest_session_ready(session=self.session, reservation=self.reservation)
        self.session.refresh_from_db()
        self.assertEqual(self.session.ready_at, first)

    def test_ready_at_not_overwritten(self):
        first = timezone.now() - timedelta(hours=2)
        self.session.ready_at = first
        self.session.save(update_fields=["ready_at"])
        emit_guest_session_ready(session=self.session, reservation=self.reservation)
        self.session.refresh_from_db()
        self.assertEqual(self.session.ready_at, first)

    def test_orchestrator_patch_sets_ready_at(self):
        GuestCheckInOrchestrator.patch_slot(
            self.session,
            self.reservation,
            position=1,
            fields=dict(_SLOT_FIELDS),
        )
        self.session.refresh_from_db()
        self.assertIsNotNone(self.session.ready_at)


class GuestCheckInExpiryTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Expiry Tenant", slug="expiry-tenant")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Expiry Property",
            slug="expiry-property",
            guest_checkin_opens_days_before=0,
        )
        check_in, check_out = _relative_stay()
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="EX-001",
            check_in=check_in,
            check_out=check_out,
            adults_count=1,
            booker_name="Expired Guest",
            amount=Decimal("50.00"),
        )

    def test_expire_stale_sessions_batch(self):
        session = ensure_active_session(
            self.reservation,
            created_from=GuestCheckInSessionCreatedFrom.EMAIL,
        )
        session.expires_at = timezone.now() - timedelta(hours=1)
        session.save(update_fields=["expires_at"])

        result = expire_stale_sessions()
        self.assertEqual(result["expired"], 1)
        session.refresh_from_db()
        self.assertEqual(session.status, GuestCheckInSessionStatus.EXPIRED)

    def test_expire_guest_checkin_sessions_task(self):
        session = ensure_active_session(
            self.reservation,
            created_from=GuestCheckInSessionCreatedFrom.EMAIL,
        )
        session.expires_at = timezone.now() - timedelta(minutes=5)
        session.save(update_fields=["expires_at"])

        result = expire_guest_checkin_sessions()
        self.assertEqual(result["expired"], 1)


class GuestReminderServiceTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Reminder Tenant", slug="reminder-tenant")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Reminder Property",
            slug="reminder-property",
            guest_checkin_opens_days_before=7,
        )
        # Arrival in 3 days → D-3 reminder + session already open (opens 7 days before).
        check_in, check_out = _relative_stay(offset_days=3, nights=3)
        self.check_in = check_in
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="RM-001",
            check_in=check_in,
            check_out=check_out,
            adults_count=1,
            booker_name="Reminder Guest",
            booker_email="guest@example.com",
            amount=Decimal("80.00"),
            status=Reservation.Status.EXPECTED,
        )
        Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="Novi",
            last_name="gost",
            name="Novi gost",
            is_primary=True,
        )
        ensure_active_session(
            self.reservation,
            created_from=GuestCheckInSessionCreatedFrom.EMAIL,
        )

    @patch("apps.communications.guest_reminder_service.send_guest_message")
    def test_send_pre_arrival_reminder_creates_draft(self, mock_send):
        mock_send.return_value = None
        with patch(
            "apps.communications.guest_reminder_service.build_message_channels",
            return_value={
                "email": {"available": True},
                "whatsapp": {"available": False},
                "booking": {"available": False},
            },
        ):
            result = GuestReminderService.send_pre_arrival_reminder(
                self.reservation,
                days_before=3,
            )

        # Delivery may be synchronous (sent) or outbox (queued).
        self.assertIn(result["status"], {"sent", "queued"})
        hint = guest_web_checkin_reminder_hint(days_before=3)
        draft = GuestMessageDraft.objects.get(reservation=self.reservation, hint=hint)
        self.assertEqual(draft.intent, GuestMessageIntent.CHECKIN)
        mock_send.assert_called_once()

    def test_reminder_skips_when_already_sent(self):
        hint = guest_web_checkin_reminder_hint(days_before=1)
        GuestMessageDraft.objects.create(
            tenant_id=self.tenant.pk,
            reservation=self.reservation,
            intent=GuestMessageIntent.CHECKIN,
            hint=hint,
            llm_body_text="Already sent",
            final_body_text="Already sent",
            language="en",
            language_source="tenant_default",
            language_reason="test",
            channel="email",
        )
        result = GuestReminderService.send_pre_arrival_reminder(
            self.reservation,
            days_before=1,
        )
        self.assertEqual(result["status"], "already_sent")

    def test_reminder_skips_when_checkin_complete(self):
        GuestCheckInOrchestrator.patch_slot(
            ensure_active_session(
                self.reservation,
                created_from=GuestCheckInSessionCreatedFrom.EMAIL,
            ),
            self.reservation,
            position=1,
            fields=dict(_SLOT_FIELDS),
        )
        result = GuestReminderService.send_pre_arrival_reminder(
            self.reservation,
            days_before=3,
        )
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "checkin_complete")


class GuestCheckInReminderSweepTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Sweep Tenant", slug="sweep-tenant")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Sweep Property",
            slug="sweep-property",
            guest_checkin_opens_days_before=7,
        )
        tz = ZoneInfo("Europe/Zagreb")
        self.now = datetime.now(tz)
        check_in = self.now.date() + timedelta(days=3)
        check_out = check_in + timedelta(days=3)
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="SW-001",
            check_in=check_in,
            check_out=check_out,
            adults_count=1,
            booker_name="Sweep Guest",
            booker_email="sweep@example.com",
            amount=Decimal("90.00"),
            status=Reservation.Status.EXPECTED,
        )
        Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="Novi",
            last_name="gost",
            name="Novi gost",
            is_primary=True,
        )
        ensure_active_session(
            self.reservation,
            created_from=GuestCheckInSessionCreatedFrom.EMAIL,
        )

    def test_reservations_due_for_checkin_reminder_matches_days_before(self):
        due = reservations_due_for_checkin_reminder(days_before=3, now=self.now)
        self.assertEqual([r.pk for r in due], [self.reservation.pk])

    @override_settings(GUEST_CHECKIN_REMINDER_ENABLED=True, GUEST_CHECKIN_REMINDER_DAYS_BEFORE="3")
    @patch(
        "apps.communications.guest_reminder_service.GuestReminderService.send_pre_arrival_reminder"
    )
    def test_send_pre_arrival_checkin_reminders_task(self, mock_send):
        mock_send.return_value = {"status": "sent"}
        with patch(
            "apps.reservations.guest_checkin_analytics.reservations_due_for_checkin_reminder",
            return_value=[self.reservation],
        ):
            result = send_pre_arrival_checkin_reminders()
        self.assertEqual(result["sent"], 1)
        mock_send.assert_called_once()


class GuestCheckInMetricsTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Metrics Tenant", slug="metrics-tenant")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Metrics Property",
            slug="metrics-property",
            guest_checkin_opens_days_before=0,
        )
        check_in, check_out = _relative_stay()
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="MT-001",
            check_in=check_in,
            check_out=check_out,
            adults_count=1,
            booker_name="Metrics Guest",
            amount=Decimal("100.00"),
        )
        Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="Ana",
            last_name="Anić",
            name="Ana Anić",
            is_primary=True,
        )

    def test_load_guest_checkin_kpis_counts_sessions(self):
        session = ensure_active_session(
            self.reservation,
            created_from=GuestCheckInSessionCreatedFrom.EMAIL,
        )
        ready_at = timezone.now() - timedelta(hours=1)
        completed_at = timezone.now()
        session.ready_at = ready_at
        session.completed_at = completed_at
        session.status = GuestCheckInSessionStatus.COMPLETED
        session.save(
            update_fields=["ready_at", "completed_at", "status", "updated_at"],
        )

        kpis = load_guest_checkin_kpis(days=30)
        self.assertEqual(kpis.sessions_created, 1)
        self.assertEqual(kpis.sessions_completed, 1)
        self.assertEqual(kpis.sessions_ready, 1)
        self.assertEqual(kpis.auto_complete_count, 1)
        self.assertIsNotNone(kpis.ready_to_complete_seconds_median)

    def test_log_guest_checkin_metrics_task(self):
        ensure_active_session(
            self.reservation,
            created_from=GuestCheckInSessionCreatedFrom.EMAIL,
        )
        result = log_guest_checkin_metrics()
        self.assertEqual(result["sessions_created"], 1)
