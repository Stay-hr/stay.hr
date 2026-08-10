from datetime import timedelta
from unittest.mock import MagicMock, patch
from django.test import TestCase
from django.utils import timezone

from apps.integrations.channex.booking_service import channex_external_id
from apps.integrations.channex.booking_tasks import (
    process_channex_booking_revisions_feed_periodic,
    reconcile_channex_cancelled_bookings_daily,
)
from apps.integrations.channex.cancel_reconcile import (
    LOOKAHEAD_DAYS,
    ZAGREB,
    reconcile_channex_cancelled_bookings,
)
from apps.integrations.channex.cancel_service import (
    OPERATOR_NOTE_PREFIX,
    heal_channex_cancel_locally,
)
from apps.integrations.channex.exceptions import ChannexApiError
from apps.integrations.models import IntegrationConfig
from apps.properties.models import Property, Unit
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant


class ChannexCancelReconcileTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="uzorita", name="Uzorita")
        self.property = Property.objects.create(
            tenant=self.tenant,
            slug="uzorita",
            name="Uzorita",
            timezone="Europe/Zagreb",
        )
        self.unit = Unit.objects.create(
            tenant=self.tenant,
            property=self.property,
            code="R1",
            name="R1",
            capacity_max_guests=2,
            capacity_adults=2,
        )
        self.integration = IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.CHANNEX,
            is_active=True,
        )
        self.integration.set_config_dict(
            {
                "environment": "production",
                "base_url": "https://channex.io/api/v1",
                "property_id": "prop-1",
                "api_key": "test-key",
                "sync_property_slug": "uzorita",
            }
        )
        self.integration.save()

        self.booking_id = "164f2183-454c-40ea-905d-d79859136236"
        today = timezone.now().astimezone(ZAGREB).date()
        self.check_in = today + timedelta(days=7)
        self.check_out = today + timedelta(days=10)

    def _make_reservation(self, **kwargs) -> Reservation:
        defaults = {
            "tenant": self.tenant,
            "property": self.property,
            "external_id": channex_external_id(self.booking_id),
            "booking_code": "BCODE-1",
            "booker_name": "Test Guest",
            "check_in": self.check_in,
            "check_out": self.check_out,
            "status": Reservation.Status.EXPECTED,
            "import_source": "channex",
            "source": "Booking.com",
        }
        defaults.update(kwargs)
        return Reservation.objects.create(**defaults)

    def _booking_payload(self, status: str = "cancelled", **attr_overrides) -> dict:
        attrs = {
            "status": status,
            "arrival_date": self.check_in.isoformat(),
            "departure_date": self.check_out.isoformat(),
        }
        attrs.update(attr_overrides)
        return {"id": self.booking_id, "attributes": attrs}

    @patch("apps.integrations.channel_manager.tasks.sync_reservation_outbound_task.delay")
    def test_heal_expected_sets_fields_and_one_outbound_remove(self, mock_delay):
        reservation = self._make_reservation()
        with self.captureOnCommitCallbacks(execute=True):
            result = heal_channex_cancel_locally(reservation.pk)
        reservation.refresh_from_db()

        self.assertEqual(result, "healed")
        self.assertEqual(reservation.status, Reservation.Status.CANCELED)
        self.assertEqual(reservation.booking_status, "cancelled")
        self.assertIsNotNone(reservation.canceled_at)
        self.assertIn(OPERATOR_NOTE_PREFIX, reservation.notes)
        mock_delay.assert_called_once_with(reservation.pk, "remove")

    @patch("apps.integrations.channel_manager.tasks.sync_reservation_outbound_task.delay")
    def test_heal_checked_in(self, mock_delay):
        reservation = self._make_reservation(status=Reservation.Status.CHECKED_IN)
        with self.captureOnCommitCallbacks(execute=True):
            result = heal_channex_cancel_locally(reservation.pk)
        reservation.refresh_from_db()
        self.assertEqual(result, "healed")
        self.assertEqual(reservation.status, Reservation.Status.CANCELED)
        mock_delay.assert_called_once_with(reservation.pk, "remove")

    @patch("apps.integrations.channel_manager.tasks.sync_reservation_outbound_task.delay")
    def test_reheal_idempotent_no_second_outbound(self, mock_delay):
        reservation = self._make_reservation()
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(heal_channex_cancel_locally(reservation.pk), "healed")
        reservation.refresh_from_db()
        canceled_at = reservation.canceled_at
        notes = reservation.notes
        mock_delay.reset_mock()

        with self.captureOnCommitCallbacks(execute=True):
            result = heal_channex_cancel_locally(reservation.pk)
        reservation.refresh_from_db()

        self.assertEqual(result, "already_canceled")
        self.assertEqual(reservation.canceled_at, canceled_at)
        self.assertEqual(reservation.notes, notes)
        mock_delay.assert_not_called()

    def test_heal_skips_no_show(self):
        reservation = self._make_reservation(status=Reservation.Status.NO_SHOW)
        result = heal_channex_cancel_locally(reservation.pk)
        reservation.refresh_from_db()
        self.assertEqual(result, "skipped_no_show")
        self.assertEqual(reservation.status, Reservation.Status.NO_SHOW)

    def test_heal_skips_checked_out(self):
        reservation = self._make_reservation(status=Reservation.Status.CHECKED_OUT)
        result = heal_channex_cancel_locally(reservation.pk)
        reservation.refresh_from_db()
        self.assertEqual(result, "skipped_ineligible")
        self.assertEqual(reservation.status, Reservation.Status.CHECKED_OUT)

    def test_reconcile_heals_remote_cancelled(self):
        reservation = self._make_reservation()
        mock_client = MagicMock()
        mock_client.get_booking.return_value = self._booking_payload("cancelled")

        with self.captureOnCommitCallbacks(execute=True):
            stats = reconcile_channex_cancelled_bookings(
                self.integration,
                client=mock_client,
            )

        reservation.refresh_from_db()
        self.assertEqual(stats["candidates"], 1)
        self.assertEqual(stats["api_checked"], 1)
        self.assertEqual(stats["healed"], 1)
        self.assertEqual(stats["healed_ids"], [reservation.pk])
        self.assertEqual(reservation.status, Reservation.Status.CANCELED)
        mock_client.get_booking.assert_called_once_with(self.booking_id)

    def test_reconcile_noop_active_remote(self):
        reservation = self._make_reservation()
        mock_client = MagicMock()
        mock_client.get_booking.return_value = self._booking_payload("modified")

        stats = reconcile_channex_cancelled_bookings(
            self.integration,
            client=mock_client,
        )
        reservation.refresh_from_db()
        self.assertEqual(stats["noop_active"], 1)
        self.assertEqual(stats["healed"], 0)
        self.assertEqual(reservation.status, Reservation.Status.EXPECTED)

    def test_reconcile_remote_not_found_fail_closed(self):
        reservation = self._make_reservation()
        mock_client = MagicMock()
        mock_client.get_booking.side_effect = ChannexApiError("missing", status_code=404)

        stats = reconcile_channex_cancelled_bookings(
            self.integration,
            client=mock_client,
        )
        reservation.refresh_from_db()
        self.assertEqual(stats["remote_not_found"], 1)
        self.assertEqual(stats["healed"], 0)
        self.assertEqual(stats["errors"], 0)
        self.assertEqual(reservation.status, Reservation.Status.EXPECTED)

    def test_reconcile_invalid_remote_payload(self):
        reservation = self._make_reservation()
        mock_client = MagicMock()
        mock_client.get_booking.return_value = {"id": self.booking_id, "attributes": {}}

        stats = reconcile_channex_cancelled_bookings(
            self.integration,
            client=mock_client,
        )
        reservation.refresh_from_db()
        self.assertEqual(stats["invalid_remote_payload"], 1)
        self.assertEqual(stats["noop_active"], 0)
        self.assertEqual(reservation.status, Reservation.Status.EXPECTED)

    @patch("apps.integrations.channex.cancel_reconcile.heal_channex_cancel_locally")
    @patch("apps.integrations.channel_manager.tasks.sync_reservation_outbound_task.delay")
    def test_dry_run_no_heal_no_outbound(self, mock_delay, mock_heal):
        reservation = self._make_reservation()
        mock_client = MagicMock()
        mock_client.get_booking.return_value = self._booking_payload("cancelled")

        with self.captureOnCommitCallbacks(execute=True):
            stats = reconcile_channex_cancelled_bookings(
                self.integration,
                client=mock_client,
                dry_run=True,
            )

        reservation.refresh_from_db()
        self.assertEqual(stats["would_heal"], 1)
        self.assertEqual(stats["healed"], 0)
        self.assertEqual(reservation.status, Reservation.Status.EXPECTED)
        mock_heal.assert_not_called()
        mock_delay.assert_not_called()
        mock_client.get_booking.assert_called_once()

    def test_api_error_continues_batch(self):
        r1 = self._make_reservation(booking_code="A", external_id=channex_external_id("b1"))
        r2 = self._make_reservation(
            booking_code="B",
            external_id=channex_external_id("b2"),
            check_in=self.check_in + timedelta(days=1),
            check_out=self.check_out + timedelta(days=1),
        )
        mock_client = MagicMock()

        def get_booking(booking_id):
            if booking_id == "b1":
                raise ChannexApiError("boom", status_code=500)
            return self._booking_payload("cancelled")

        mock_client.get_booking.side_effect = get_booking

        with self.captureOnCommitCallbacks(execute=True):
            stats = reconcile_channex_cancelled_bookings(
                self.integration,
                client=mock_client,
            )

        r1.refresh_from_db()
        r2.refresh_from_db()
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(stats["healed"], 1)
        self.assertEqual(r1.status, Reservation.Status.EXPECTED)
        self.assertEqual(r2.status, Reservation.Status.CANCELED)

    def test_other_tenant_same_external_id_not_checked(self):
        other = Tenant.objects.create(slug="other", name="Other")
        other_prop = Property.objects.create(
            tenant=other,
            slug="other",
            name="Other",
            timezone="Europe/Zagreb",
        )
        self._make_reservation(
            tenant=other,
            property=other_prop,
            booking_code="OTHER",
        )
        local = self._make_reservation()
        mock_client = MagicMock()
        mock_client.get_booking.return_value = self._booking_payload("new")

        stats = reconcile_channex_cancelled_bookings(
            self.integration,
            client=mock_client,
        )
        self.assertEqual(stats["candidates"], 1)
        self.assertEqual(stats["api_checked"], 1)
        mock_client.get_booking.assert_called_once_with(self.booking_id)
        local.refresh_from_db()
        self.assertEqual(local.status, Reservation.Status.EXPECTED)

    def test_unparseable_external_id_skipped(self):
        self._make_reservation(external_id="legacy-no-prefix")
        mock_client = MagicMock()

        stats = reconcile_channex_cancelled_bookings(
            self.integration,
            client=mock_client,
        )
        self.assertEqual(stats["candidates"], 1)
        self.assertEqual(stats["skipped_unparseable_id"], 1)
        self.assertEqual(stats["api_checked"], 0)
        mock_client.get_booking.assert_not_called()

    def test_outside_lookahead_not_candidate(self):
        today = timezone.now().astimezone(ZAGREB).date()
        far_in = today + timedelta(days=LOOKAHEAD_DAYS + 10)
        self._make_reservation(
            check_in=far_in,
            check_out=far_in + timedelta(days=2),
        )
        mock_client = MagicMock()
        stats = reconcile_channex_cancelled_bookings(
            self.integration,
            client=mock_client,
        )
        self.assertEqual(stats["candidates"], 0)
        mock_client.get_booking.assert_not_called()

    def test_past_check_out_window_not_candidate(self):
        today = timezone.now().astimezone(ZAGREB).date()
        self._make_reservation(
            check_in=today - timedelta(days=10),
            check_out=today - timedelta(days=3),
        )
        mock_client = MagicMock()
        stats = reconcile_channex_cancelled_bookings(
            self.integration,
            client=mock_client,
        )
        self.assertEqual(stats["candidates"], 0)
        mock_client.get_booking.assert_not_called()

    def test_limit_caps_candidates(self):
        for i in range(3):
            self._make_reservation(
                booking_code=f"C{i}",
                external_id=channex_external_id(f"book-{i}"),
                check_in=self.check_in + timedelta(days=i),
                check_out=self.check_out + timedelta(days=i),
            )
        mock_client = MagicMock()
        mock_client.get_booking.return_value = self._booking_payload("new")
        stats = reconcile_channex_cancelled_bookings(
            self.integration,
            client=mock_client,
            limit=2,
        )
        self.assertEqual(stats["candidates"], 2)
        self.assertEqual(mock_client.get_booking.call_count, 2)

    def test_status_normalization_strip_lower(self):
        reservation = self._make_reservation()
        mock_client = MagicMock()
        mock_client.get_booking.return_value = self._booking_payload("  Cancelled ")

        with self.captureOnCommitCallbacks(execute=True):
            stats = reconcile_channex_cancelled_bookings(
                self.integration,
                client=mock_client,
            )
        reservation.refresh_from_db()
        self.assertEqual(stats["healed"], 1)
        self.assertEqual(reservation.status, Reservation.Status.CANCELED)

    def test_daily_task_no_integration(self):
        IntegrationConfig.objects.filter(pk=self.integration.pk).delete()
        stats = reconcile_channex_cancelled_bookings_daily(tenant_slug="uzorita")
        self.assertEqual(stats["error"], "no_integration")

    def test_daily_task_ambiguous_integration(self):
        IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.CHANNEX,
            is_active=True,
        )
        stats = reconcile_channex_cancelled_bookings_daily(tenant_slug="uzorita")
        self.assertEqual(stats["error"], "ambiguous_integration")
        self.assertEqual(stats["integration_count"], 2)

    @patch("apps.integrations.channex.booking_tasks.process_channex_booking_revisions_feed")
    def test_feed_periodic_logs_when_errors_only(self, mock_feed):
        mock_feed.return_value = {"ingested": [], "ack_only": 0, "errors": 1}
        with self.assertLogs(
            "apps.integrations.channex.booking_tasks",
            level="INFO",
        ) as captured:
            result = process_channex_booking_revisions_feed_periodic(tenant_slug="uzorita")
        self.assertEqual(result["errors"], 1)
        self.assertTrue(
            any(
                "channex booking revisions feed periodic processed" in line
                for line in captured.output
            )
        )
