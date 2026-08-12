from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.properties.models import Property
from apps.reservations.guest_checkin_orchestrator import GuestCheckInOrchestrator
from apps.reservations.guest_checkin_progress import checkin_progress_for_reservation
from apps.reservations.guest_checkin_version import maybe_touch_checkin_version_debounced
from apps.reservations.models import (
    Guest,
    GuestCheckInSessionCreatedFrom,
    GuestCheckInSessionStatus,
    Reservation,
    ReservationVersion,
    ReservationVersionScope,
)
from apps.tenants.models import RECEPTION_DEVICE_SCOPES, ApiApplication, Tenant


@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        }
    }
)
class GuestCheckInReceptionTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Reception CI", slug="reception-ci")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Reception Property",
            slug="reception-property",
            guest_checkin_opens_days_before=0,
        )
        self.app, self.raw_token = ApiApplication.create_with_token(
            tenant=self.tenant,
            name="Reception tablet",
            scopes=RECEPTION_DEVICE_SCOPES,
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="RC-001",
            check_in=timezone.localdate(),
            check_out=timezone.localdate() + timedelta(days=2),
            adults_count=1,
            booker_name="Ana Anić",
            amount=Decimal("100.00"),
            status=Reservation.Status.EXPECTED,
        )
        Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="Ana",
            last_name="Anić",
            name="Ana Anić",
            is_primary=True,
        )
        self.client = APIClient()
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.raw_token}"}

    def test_checkin_progress_without_session(self):
        progress = checkin_progress_for_reservation(self.reservation)
        self.assertEqual(progress["required_slots"], 1)
        self.assertEqual(progress["ready_slots"], 0)
        self.assertIsNone(progress["session_status"])
        self.assertIsNone(progress["checkin_url"])

    def test_checkin_progress_with_active_session(self):
        GuestCheckInOrchestrator.ensure_session_and_link(
            self.reservation,
            created_from=GuestCheckInSessionCreatedFrom.EMAIL,
        )
        progress = checkin_progress_for_reservation(self.reservation)
        self.assertEqual(progress["session_status"], GuestCheckInSessionStatus.ACTIVE)
        self.assertIn("/check-in/", progress["checkin_url"] or "")

    def test_reservation_detail_includes_checkin_progress(self):
        GuestCheckInOrchestrator.ensure_session_and_link(
            self.reservation,
            created_from=GuestCheckInSessionCreatedFrom.EMAIL,
        )
        response = self.client.get(
            f"/api/v1/reception/reservations/{self.reservation.pk}/",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        progress = response.json()["checkin_progress"]
        self.assertEqual(progress["required_slots"], 1)
        self.assertEqual(progress["session_status"], GuestCheckInSessionStatus.ACTIVE)

    def test_regenerate_link_revokes_previous_session(self):
        first = GuestCheckInOrchestrator.ensure_session_and_link(
            self.reservation,
            created_from=GuestCheckInSessionCreatedFrom.EMAIL,
        )
        response = self.client.post(
            f"/api/v1/reception/reservations/{self.reservation.pk}/guest-checkin/regenerate/",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertNotEqual(payload["token"], str(first.session.token))
        self.assertIn("/check-in/", payload["url"])
        first.session.refresh_from_db()
        self.assertEqual(first.session.status, GuestCheckInSessionStatus.REVOKED)

    def test_debounced_checkin_version_touch_rate_limited(self):
        maybe_touch_checkin_version_debounced(self.reservation.pk)
        row = ReservationVersion.objects.get(
            reservation=self.reservation,
            scope=ReservationVersionScope.CHECKIN,
        )
        self.assertEqual(row.version, 1)
        maybe_touch_checkin_version_debounced(self.reservation.pk)
        row.refresh_from_db()
        self.assertEqual(row.version, 1)


class GuestCheckInChannexLinkTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Channex CI", slug="channex-ci")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Channex Property",
            slug="channex-property",
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            external_id="channex:test-booking",
            booking_code="BK-CHX",
            check_in=date(2026, 8, 1),
            check_out=date(2026, 8, 4),
            status=Reservation.Status.EXPECTED,
            import_source="channex",
            booker_name="OTA Guest",
        )

    @patch("apps.communications.guest_checkin_channex.get_channel_manager")
    @patch("apps.communications.guest_checkin_channex.get_active_channex_integration")
    @patch("apps.communications.guest_checkin_channex.send_message_for_reservation")
    def test_send_guest_checkin_link_via_channex(
        self,
        mock_send,
        _mock_integration,
        mock_channel_manager,
    ):
        from apps.tenants.models import ChannelManager

        mock_channel_manager.return_value = ChannelManager.CHANNEX
        from apps.communications.guest_checkin_channex import send_guest_checkin_link_via_channex

        result = send_guest_checkin_link_via_channex(self.reservation.pk)
        self.assertTrue(result["sent"])
        mock_send.assert_called_once()
        body = mock_send.call_args.args[2]
        self.assertIn("/check-in/", body)
        session = self.reservation.guest_checkin_sessions.get(
            status=GuestCheckInSessionStatus.ACTIVE
        )
        self.assertEqual(session.created_from, GuestCheckInSessionCreatedFrom.CHANNEX)
        self.assertEqual(
            session.last_distributed_from,
            GuestCheckInSessionCreatedFrom.CHANNEX,
        )

    @patch("apps.communications.guest_checkin_channex.get_channel_manager")
    @patch("apps.communications.guest_checkin_channex.get_active_channex_integration")
    @patch("apps.communications.guest_checkin_channex.send_message_for_reservation")
    def test_channex_send_failure_does_not_mark_distributed(
        self,
        mock_send,
        _mock_integration,
        mock_channel_manager,
    ):
        from apps.integrations.channex.exceptions import ChannexBookingIngestError
        from apps.tenants.models import ChannelManager

        mock_channel_manager.return_value = ChannelManager.CHANNEX
        mock_send.side_effect = ChannexBookingIngestError("boom")
        from apps.communications.guest_checkin_channex import send_guest_checkin_link_via_channex

        result = send_guest_checkin_link_via_channex(self.reservation.pk)
        self.assertFalse(result["sent"])
        session = self.reservation.guest_checkin_sessions.get(
            status=GuestCheckInSessionStatus.ACTIVE
        )
        self.assertEqual(session.created_from, GuestCheckInSessionCreatedFrom.CHANNEX)
        self.assertIsNone(session.last_distributed_from)

    @patch("apps.communications.guest_checkin_channex.get_channel_manager")
    def test_send_guest_checkin_link_skips_non_channex(self, mock_channel_manager):
        from apps.tenants.models import ChannelManager

        mock_channel_manager.return_value = ChannelManager.NONE
        from apps.communications.guest_checkin_channex import send_guest_checkin_link_via_channex

        result = send_guest_checkin_link_via_channex(self.reservation.pk)
        self.assertFalse(result["sent"])
        self.assertEqual(result["reason"], "channel_manager_not_channex")
