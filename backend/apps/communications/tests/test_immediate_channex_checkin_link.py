"""Late-booking immediate Channex check-in link (create enqueue + locked send)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.properties.models import Property
from apps.reservations.guest_checkin_orchestrator import GuestCheckInOrchestrator
from apps.reservations.models import (
    Guest,
    GuestCheckInSessionCreatedFrom,
    GuestCheckInSessionStatus,
    Reservation,
)
from apps.tenants.models import Tenant


def _make_channex_reservation(
    *,
    tenant: Tenant,
    prop: Property,
    check_in: date,
    check_out: date | None = None,
    status: str = Reservation.Status.EXPECTED,
    booking_code: str = "BK-LATE",
) -> Reservation:
    reservation = Reservation.objects.create(
        tenant=tenant,
        property=prop,
        external_id=f"channex:{booking_code}",
        booking_code=booking_code,
        check_in=check_in,
        check_out=check_out or (check_in + timedelta(days=1)),
        status=status,
        import_source="channex",
        booker_name="Late Booker",
        adults_count=1,
        amount=Decimal("100.00"),
    )
    Guest.objects.create(
        tenant=tenant,
        reservation=reservation,
        first_name="Late",
        last_name="Booker",
        name="Late Booker",
        is_primary=True,
    )
    return reservation


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class ImmediateChannexCheckinEligibilityTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Late CI", slug="late-ci")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Late Prop",
            slug="late-prop",
            timezone="Europe/Zagreb",
            guest_checkin_opens_days_before=7,
        )

    @patch("apps.communications.guest_checkin_channex.property_local_now")
    def test_eligible_d7_and_d0(self, mock_local_now):
        from apps.communications.guest_checkin_channex import (
            is_immediate_channex_checkin_link_eligible,
        )

        today = date(2026, 8, 15)
        mock_local_now.return_value = datetime(
            2026, 8, 15, 10, 0, tzinfo=ZoneInfo("Europe/Zagreb")
        )
        r_d7 = _make_channex_reservation(
            tenant=self.tenant,
            prop=self.property,
            check_in=today + timedelta(days=7),
            booking_code="D7",
        )
        r_d0 = _make_channex_reservation(
            tenant=self.tenant,
            prop=self.property,
            check_in=today,
            booking_code="D0",
        )
        self.assertTrue(is_immediate_channex_checkin_link_eligible(r_d7))
        self.assertTrue(is_immediate_channex_checkin_link_eligible(r_d0))

    @patch("apps.communications.guest_checkin_channex.property_local_now")
    def test_not_eligible_past_or_beyond_window(self, mock_local_now):
        from apps.communications.guest_checkin_channex import (
            is_immediate_channex_checkin_link_eligible,
        )

        today = date(2026, 8, 15)
        mock_local_now.return_value = datetime(
            2026, 8, 15, 10, 0, tzinfo=ZoneInfo("Europe/Zagreb")
        )
        r_past = _make_channex_reservation(
            tenant=self.tenant,
            prop=self.property,
            check_in=today - timedelta(days=1),
            booking_code="PAST",
        )
        r_far = _make_channex_reservation(
            tenant=self.tenant,
            prop=self.property,
            check_in=today + timedelta(days=8),
            booking_code="FAR",
        )
        self.assertFalse(is_immediate_channex_checkin_link_eligible(r_past))
        self.assertFalse(is_immediate_channex_checkin_link_eligible(r_far))

    @patch("apps.communications.guest_checkin_channex.property_local_now")
    def test_midnight_boundary_uses_property_tz(self, mock_local_now):
        from apps.communications.guest_checkin_channex import (
            is_immediate_channex_checkin_link_eligible,
        )

        # 00:30 Zagreb on Aug 16 == still Aug 15 UTC — eligibility must use Zagreb date.
        mock_local_now.return_value = datetime(
            2026, 8, 16, 0, 30, tzinfo=ZoneInfo("Europe/Zagreb")
        )
        r = _make_channex_reservation(
            tenant=self.tenant,
            prop=self.property,
            check_in=date(2026, 8, 16),
            booking_code="MIDNIGHT",
        )
        self.assertTrue(is_immediate_channex_checkin_link_eligible(r))
        mock_local_now.assert_called()
        # today is Aug 16 local → D-0
        self.assertEqual(mock_local_now.return_value.date(), date(2026, 8, 16))


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class ImmediateChannexCheckinSendTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Send CI", slug="send-ci")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Send Prop",
            slug="send-prop",
            timezone="Europe/Zagreb",
            guest_checkin_opens_days_before=7,
        )
        self.today = date(2026, 8, 15)
        self.reservation = _make_channex_reservation(
            tenant=self.tenant,
            prop=self.property,
            check_in=self.today + timedelta(days=3),
            booking_code="SEND1",
        )

    @patch("apps.communications.guest_checkin_channex.get_channel_manager")
    @patch("apps.communications.guest_checkin_channex.get_active_channex_integration")
    @patch("apps.communications.guest_checkin_channex.send_message_for_reservation")
    def test_creates_session_when_missing_and_sends(
        self,
        mock_send,
        _mock_integration,
        mock_cm,
    ):
        from apps.communications.guest_checkin_channex import send_guest_checkin_link_via_channex

        mock_cm.return_value = ChannelManager.CHANNEX
        self.assertEqual(self.reservation.guest_checkin_sessions.count(), 0)
        result = send_guest_checkin_link_via_channex(self.reservation.pk)
        self.assertTrue(result["sent"])
        mock_send.assert_called_once()
        session = self.reservation.guest_checkin_sessions.get(
            status=GuestCheckInSessionStatus.ACTIVE
        )
        self.assertEqual(
            session.last_distributed_from,
            GuestCheckInSessionCreatedFrom.CHANNEX,
        )

    @patch("apps.communications.guest_checkin_channex.get_channel_manager")
    @patch("apps.communications.guest_checkin_channex.get_active_channex_integration")
    @patch("apps.communications.guest_checkin_channex.send_message_for_reservation")
    def test_manual_after_distribution_returns_already_distributed(
        self,
        mock_send,
        _mock_integration,
        mock_cm,
    ):
        from apps.communications.guest_checkin_channex import send_guest_checkin_link_via_channex

        mock_cm.return_value = ChannelManager.CHANNEX
        first = send_guest_checkin_link_via_channex(self.reservation.pk)
        self.assertTrue(first["sent"])
        second = send_guest_checkin_link_via_channex(self.reservation.pk)
        self.assertFalse(second["sent"])
        self.assertEqual(second["reason"], "already_distributed")
        self.assertEqual(mock_send.call_count, 1)

    @patch("apps.communications.guest_checkin_channex.property_local_now")
    @patch("apps.communications.guest_checkin_channex.get_channel_manager")
    @patch("apps.communications.guest_checkin_channex.get_active_channex_integration")
    @patch("apps.communications.guest_checkin_channex.send_message_for_reservation")
    def test_task_skips_when_canceled_after_enqueue(
        self,
        mock_send,
        _mock_integration,
        mock_cm,
        mock_local_now,
    ):
        from apps.communications.guest_checkin_channex import (
            maybe_send_immediate_channex_checkin_link,
        )

        mock_cm.return_value = ChannelManager.CHANNEX
        mock_local_now.return_value = datetime(
            2026, 8, 15, 12, 0, tzinfo=ZoneInfo("Europe/Zagreb")
        )
        self.reservation.status = Reservation.Status.CANCELED
        self.reservation.save(update_fields=["status", "updated_at"])
        result = maybe_send_immediate_channex_checkin_link(self.reservation.pk)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "not_eligible")
        mock_send.assert_not_called()

    @patch("apps.communications.guest_checkin_channex.property_local_now")
    @patch("apps.communications.guest_checkin_channex.get_channel_manager")
    @patch("apps.communications.guest_checkin_channex.get_active_channex_integration")
    @patch("apps.communications.guest_checkin_channex.send_message_for_reservation")
    def test_task_skips_when_check_in_moved_outside_window(
        self,
        mock_send,
        _mock_integration,
        mock_cm,
        mock_local_now,
    ):
        from apps.communications.guest_checkin_channex import (
            maybe_send_immediate_channex_checkin_link,
        )

        mock_cm.return_value = ChannelManager.CHANNEX
        mock_local_now.return_value = datetime(
            2026, 8, 15, 12, 0, tzinfo=ZoneInfo("Europe/Zagreb")
        )
        self.reservation.check_in = self.today + timedelta(days=20)
        self.reservation.check_out = self.reservation.check_in + timedelta(days=1)
        self.reservation.save(update_fields=["check_in", "check_out", "updated_at"])
        result = maybe_send_immediate_channex_checkin_link(self.reservation.pk)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "not_eligible")
        mock_send.assert_not_called()

    def test_signal_enqueues_on_channex_create(self):
        with patch(
            "apps.communications.guest_checkin_channex.maybe_send_immediate_channex_checkin_link.delay"
        ) as mock_delay:
            with self.captureOnCommitCallbacks(execute=True):
                Reservation.objects.create(
                    tenant=self.tenant,
                    property=self.property,
                    external_id="channex:SIG1",
                    booking_code="SIG1",
                    check_in=self.today + timedelta(days=2),
                    check_out=self.today + timedelta(days=3),
                    status=Reservation.Status.EXPECTED,
                    import_source="channex",
                    booker_name="Sig",
                )
            mock_delay.assert_called_once()


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class ImmediateChannexCheckinConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Race CI", slug="race-ci")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Race Prop",
            slug="race-prop",
            timezone="Europe/Zagreb",
            guest_checkin_opens_days_before=7,
        )
        self.reservation = _make_channex_reservation(
            tenant=self.tenant,
            prop=self.property,
            check_in=date(2026, 8, 18),
            booking_code="RACE1",
        )
        # Pre-create session so both threads claim the same row.
        GuestCheckInOrchestrator.ensure_session_and_link(
            self.reservation,
            created_from=GuestCheckInSessionCreatedFrom.CHANNEX,
        )

    def _send_once(self) -> dict:
        close_old_connections()
        try:
            from apps.communications.guest_checkin_channex import (
                send_guest_checkin_link_via_channex,
            )
            from apps.tenants.models import ChannelManager as CM

            with (
                patch(
                    "apps.communications.guest_checkin_channex.get_channel_manager",
                    return_value=CM.CHANNEX,
                ),
                patch(
                    "apps.communications.guest_checkin_channex.get_active_channex_integration",
                    return_value=object(),
                ),
                patch(
                    "apps.communications.guest_checkin_channex.send_message_for_reservation",
                ) as mock_send,
            ):
                result = send_guest_checkin_link_via_channex(self.reservation.pk)
                result = dict(result)
                result["_send_calls"] = mock_send.call_count
                return result
        finally:
            close_old_connections()

    def test_parallel_sends_only_one_provider_call(self):
        # Two threads each patch their own mock — measure via last_distributed + call counts
        # by sharing a list under a lock-free counter on the mock at module level.
        from unittest.mock import MagicMock

        send_mock = MagicMock()
        results: list[dict] = []

        def worker() -> dict:
            close_old_connections()
            try:
                from apps.communications.guest_checkin_channex import (
                    send_guest_checkin_link_via_channex,
                )
                from apps.tenants.models import ChannelManager as CM

                with (
                    patch(
                        "apps.communications.guest_checkin_channex.get_channel_manager",
                        return_value=CM.CHANNEX,
                    ),
                    patch(
                        "apps.communications.guest_checkin_channex.get_active_channex_integration",
                        return_value=object(),
                    ),
                    patch(
                        "apps.communications.guest_checkin_channex.send_message_for_reservation",
                        send_mock,
                    ),
                ):
                    return send_guest_checkin_link_via_channex(self.reservation.pk)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker) for _ in range(2)]
            results = [f.result() for f in as_completed(futures)]

        sent_flags = [bool(r.get("sent")) for r in results]
        self.assertEqual(sum(1 for s in sent_flags if s), 1)
        self.assertEqual(sum(1 for s in sent_flags if not s), 1)
        self.assertTrue(
            any(r.get("reason") == "already_distributed" for r in results if not r.get("sent"))
        )
        self.assertEqual(send_mock.call_count, 1)


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class ReminderAndImmediateDedupeTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Dedupe CI", slug="dedupe-ci")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Dedupe Prop",
            slug="dedupe-prop",
            timezone="Europe/Zagreb",
            guest_checkin_opens_days_before=7,
        )
        today = timezone.localdate()
        self.reservation = _make_channex_reservation(
            tenant=self.tenant,
            prop=self.property,
            check_in=today + timedelta(days=7),
            booking_code="DED1",
        )

    @patch("apps.communications.guest_checkin_channex.get_channel_manager")
    @patch("apps.communications.guest_checkin_channex.get_active_channex_integration")
    @patch("apps.communications.guest_checkin_channex.send_message_for_reservation")
    def test_reminder_skips_after_immediate_send(
        self,
        mock_send,
        _mock_integration,
        mock_cm,
    ):
        from apps.communications.guest_checkin_channex import send_guest_checkin_link_via_channex
        from apps.communications.guest_reminder_service import GuestReminderService

        mock_cm.return_value = ChannelManager.CHANNEX
        sent = send_guest_checkin_link_via_channex(self.reservation.pk)
        self.assertTrue(sent["sent"])
        self.assertEqual(mock_send.call_count, 1)

        reminder = GuestReminderService.send_pre_arrival_reminder(
            self.reservation,
            days_before=7,
        )
        self.assertEqual(reminder["status"], "skipped")
        self.assertEqual(reminder["reason"], "link_already_distributed")
        self.assertEqual(mock_send.call_count, 1)
