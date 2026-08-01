import io
import os
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.properties.models import Property, Unit
from apps.reservations.models import (
    Guest,
    IdRecognitionSample,
    Reservation,
    ReservationUnit,
    ReservationVersionScope,
)
from apps.reservations.reservation_version import touch_reservation_version
from apps.reservations.reservation_version_events import reset_sse_connection_state_for_tests
from apps.tenants.models import RECEPTION_DEVICE_SCOPES, ApiApplication, Tenant


def _close_sse_streaming_content(response) -> None:
    """End the SSE generator without HttpResponse.close() (avoids request_finished→DB close).

    ``response.streaming_content`` is a ``map()`` wrapper; close the underlying
    ``_iterator`` so ``GeneratorExit`` runs the stream ``finally`` block.
    """
    iterator = getattr(response, "_iterator", None)
    if iterator is not None and hasattr(iterator, "close"):
        iterator.close()


class ReceptionAPITests(TestCase):
    def setUp(self):
        reset_sse_connection_state_for_tests()
        self.tenant = Tenant.objects.create(name="Uzorita", slug="uzorita")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Uzorita",
            slug="uzorita",
        )
        self.unit = Unit.objects.create(
            tenant=self.tenant,
            property=self.property,
            code="101",
            name="Soba 101",
        )
        self.app, self.raw_token = ApiApplication.create_with_token(
            tenant=self.tenant,
            name="Test tablet",
            scopes=RECEPTION_DEVICE_SCOPES,
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            external_id="ext-1",
            booking_code="BK-1",
            check_in=date(2026, 5, 10),
            check_out=date(2026, 5, 15),
            status=Reservation.Status.EXPECTED,
            booker_name="Ana Anić",
            amount=Decimal("120.00"),
        )
        ReservationUnit.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            unit=self.unit,
            room_name="Soba 101",
            sort_order=0,
        )
        self.guest = Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="Marko",
            last_name="Marković",
            is_primary=True,
        )
        self.client = APIClient()
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.raw_token}"}

    def test_health_unauthenticated(self):
        response = self.client.get("/api/v1/reception/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_system_status_requires_token(self):
        response = self.client.get("/api/v1/reception/system/status/")
        self.assertEqual(response.status_code, 403)

    def test_system_status_authenticated(self):
        response = self.client.get("/api/v1/reception/system/status/", **self.auth)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["schema_version"], 3)
        self.assertEqual(data["metrics_scope"], "worker_process")
        self.assertIn("build", data)
        self.assertIn("git_sha", data["build"])
        self.assertIn("started_at", data["build"])
        self.assertIn("hostname", data["build"])
        self.assertIn("gunicorn", data)
        self.assertIn("workers", data["gunicorn"])
        self.assertIn("worker_class", data["gunicorn"])
        self.assertIn("uptime_seconds", data["gunicorn"])
        self.assertIn("sse", data)
        self.assertIn("active_connections", data["sse"])
        self.assertIn("peak_connections", data["sse"])
        self.assertIn("connections_opened_total", data["sse"])
        self.assertIn("connections_closed_total", data["sse"])
        self.assertIn("closed_streams_sample_count", data["sse"])
        self.assertIn("average_duration_seconds", data["sse"])
        self.assertIn("active_stream_count", data["sse"])
        self.assertIn("active_streams", data["sse"])
        self.assertIn("invariant_ok", data["sse"])
        self.assertIn("invariant_delta", data["sse"])
        self.assertIsNone(data["sse"]["average_duration_seconds"])
        self.assertEqual(data["sse"]["closed_streams_sample_count"], 0)
        self.assertEqual(data["sse"]["invariant_delta"], 0)
        self.assertTrue(data["sse"]["invariant_ok"])
        self.assertEqual(data["sse"]["active_stream_count"], 0)
        self.assertEqual(data["sse"]["active_streams"], [])
        self.assertIn("event_bus", data)
        self.assertEqual(data["event_bus"]["backend"], "in_process")
        self.assertTrue(data["event_bus"]["available"])
        self.assertEqual(data["event_bus"]["redis_reconnect_count"], 0)
        self.assertEqual(data["event_bus"]["publish_count"], 0)
        self.assertEqual(data["event_bus"]["receive_count"], 0)
        self.assertEqual(data["event_bus"]["local_fallback_count"], 0)
        self.assertEqual(data["event_bus"]["dedupe_drop_count"], 0)
        self.assertIsNone(data["event_bus"]["last_publish_at"])
        self.assertIsNone(data["event_bus"]["last_receive_at"])
        self.assertIsNone(data["event_bus"]["last_fallback_at"])
        self.assertIsNone(data["event_bus"]["last_dedupe_drop_at"])
        self.assertIn("database", data)
        self.assertTrue(data["database"]["ok"])
        self.assertIsInstance(data["database"]["latency_ms"], (int, float))
        self.assertIn("messaging", data)
        self.assertIn("definitions", data["messaging"])
        self.assertIn("templates", data["messaging"])
        self.assertIn("providers", data["messaging"])
        self.assertIn("plans", data["messaging"])
        self.assertIn("outbox", data["messaging"])
        self.assertIn("flags", data["messaging"])
        self.assertIn("welcome_templates", data["messaging"])
        wt = data["messaging"]["welcome_templates"]
        self.assertIn("configured", wt)
        self.assertIn("missing_in_config", wt)
        self.assertIn("status", wt)
        self.assertIn(wt["status"], ("healthy", "warning", "critical"))
        self.assertGreaterEqual(data["messaging"]["definitions"]["count"], 1)
        self.assertIn("components", data)
        self.assertEqual(
            data["components"]["event_bus"],
            {"status": "healthy", "reason": None},
        )
        self.assertEqual(
            data["components"]["sse"],
            {"status": "healthy", "reason": None},
        )
        self.assertEqual(
            data["components"]["database"],
            {"status": "healthy", "reason": None},
        )

    def test_timeline_requires_token(self):
        response = self.client.get("/api/v1/reception/reservations/")
        self.assertEqual(response.status_code, 403)

    def test_timeline_list(self):
        response = self.client.get("/api/v1/reception/reservations/", **self.auth)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 1)
        row = data[0]
        self.assertEqual(row["id"], self.reservation.id)
        self.assertEqual(row["check_in_date"], "2026-05-10")
        self.assertEqual(row["check_out_date"], "2026-05-15")
        self.assertEqual(row["total_amount"], "120.00")
        self.assertEqual(row["room_name"], "Soba 101")
        self.assertEqual(len(row["guests"]), 1)

    def test_timeline_booked_today_filter(self):
        zagreb = ZoneInfo("Europe/Zagreb")
        now_local = datetime(2026, 6, 4, 14, 30, tzinfo=zagreb)
        booked_at = timezone.make_aware(
            datetime(2026, 6, 4, 10, 0),
            timezone=zagreb,
        )
        booked_today = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            external_id="ext-booked-today",
            booking_code="BK-2",
            check_in=date(2026, 7, 1),
            check_out=date(2026, 7, 5),
            status=Reservation.Status.EXPECTED,
            booker_name="Nova Rez",
            booked_at=booked_at,
        )
        ReservationUnit.objects.create(
            tenant=self.tenant,
            reservation=booked_today,
            unit=self.unit,
            room_name="Soba 101",
            sort_order=0,
        )

        period_today = now_local.date().isoformat()
        period_tomorrow = (now_local.date() + timedelta(days=1)).isoformat()

        period_resp = self.client.get(
            "/api/v1/reception/reservations/",
            {
                "period_from": period_today,
                "period_to": period_tomorrow,
            },
            **self.auth,
        )
        self.assertEqual(period_resp.status_code, 200)
        period_ids = {row["id"] for row in period_resp.json()}
        self.assertNotIn(booked_today.id, period_ids)

        booked_resp = self.client.get(
            "/api/v1/reception/reservations/",
            {
                "booked_from": period_today,
                "booked_to": period_tomorrow,
            },
            **self.auth,
        )
        self.assertEqual(booked_resp.status_code, 200)
        booked_ids = {row["id"] for row in booked_resp.json()}
        self.assertEqual(booked_ids, {booked_today.id})

    def test_timeline_canceled_today_filter(self):
        zagreb = ZoneInfo("Europe/Zagreb")
        today = date(2026, 6, 4)
        yesterday = today - timedelta(days=1)
        tomorrow = today + timedelta(days=1)

        canceled_today = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            external_id="ext-cancel-today",
            booking_code="BK-C1",
            check_in=date(2026, 8, 1),
            check_out=date(2026, 8, 3),
            status=Reservation.Status.CANCELED,
            booker_name="Cancel Today",
            canceled_at=timezone.make_aware(
                datetime(2026, 6, 4, 9, 0),
                timezone=zagreb,
            ),
        )
        Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            external_id="ext-cancel-yesterday",
            booking_code="BK-C0",
            check_in=date(2026, 8, 5),
            check_out=date(2026, 8, 7),
            status=Reservation.Status.CANCELED,
            booker_name="Cancel Yesterday",
            canceled_at=timezone.make_aware(
                datetime(2026, 6, 3, 18, 0),
                timezone=zagreb,
            ),
        )

        resp = self.client.get(
            "/api/v1/reception/reservations/",
            {
                "canceled_from": today.isoformat(),
                "canceled_to": tomorrow.isoformat(),
            },
            **self.auth,
        )
        self.assertEqual(resp.status_code, 200)
        ids = {row["id"] for row in resp.json()}
        self.assertEqual(ids, {canceled_today.id})
        self.assertIsNotNone(resp.json()[0].get("canceled_at"))

    def test_timeline_booked_today_excludes_canceled(self):
        zagreb = ZoneInfo("Europe/Zagreb")
        today = date(2026, 6, 4)
        tomorrow = today + timedelta(days=1)
        booked_at = timezone.make_aware(
            datetime(2026, 6, 4, 8, 0),
            timezone=zagreb,
        )

        Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            external_id="ext-booked-canceled-same-day",
            booking_code="BK-BC",
            check_in=date(2026, 8, 4),
            check_out=date(2026, 8, 5),
            status=Reservation.Status.CANCELED,
            booker_name="Booked Then Canceled",
            booked_at=booked_at,
            canceled_at=timezone.make_aware(
                datetime(2026, 6, 4, 12, 0),
                timezone=zagreb,
            ),
        )
        active_booked = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            external_id="ext-booked-active",
            booking_code="BK-BA",
            check_in=date(2026, 8, 10),
            check_out=date(2026, 8, 12),
            status=Reservation.Status.EXPECTED,
            booker_name="Active Booked",
            booked_at=booked_at,
        )

        resp = self.client.get(
            "/api/v1/reception/reservations/",
            {
                "booked_from": today.isoformat(),
                "booked_to": tomorrow.isoformat(),
            },
            **self.auth,
        )
        self.assertEqual(resp.status_code, 200)
        ids = {row["id"] for row in resp.json()}
        self.assertEqual(ids, {active_booked.id})

    def test_timeline_booked_filter_ignores_period_params(self):
        zagreb = ZoneInfo("Europe/Zagreb")
        booked_at = timezone.make_aware(
            datetime(2026, 6, 4, 11, 0),
            timezone=zagreb,
        )
        booked_today = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            external_id="ext-booked-only",
            booking_code="BK-3",
            check_in=date(2026, 9, 1),
            check_out=date(2026, 9, 3),
            status=Reservation.Status.EXPECTED,
            booked_at=booked_at,
        )

        resp = self.client.get(
            "/api/v1/reception/reservations/",
            {
                "booked_from": "2026-06-04",
                "booked_to": "2026-06-05",
                "period_from": "2026-05-10",
                "period_to": "2026-05-11",
            },
            **self.auth,
        )
        self.assertEqual(resp.status_code, 200)
        ids = {row["id"] for row in resp.json()}
        self.assertEqual(ids, {booked_today.id})

    def test_timeline_booked_today_with_include_canceled(self):
        zagreb = ZoneInfo("Europe/Zagreb")
        today = date(2026, 6, 4)
        tomorrow = today + timedelta(days=1)
        booked_at = timezone.make_aware(
            datetime(2026, 6, 4, 10, 0),
            timezone=zagreb,
        )

        active_booked = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            external_id="ext-combined-active",
            booking_code="BK-CA",
            check_in=date(2026, 8, 10),
            check_out=date(2026, 8, 12),
            status=Reservation.Status.EXPECTED,
            booker_name="Active Booked",
            booked_at=booked_at,
        )
        canceled_today = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            external_id="ext-combined-canceled",
            booking_code="BK-CC",
            check_in=date(2026, 8, 1),
            check_out=date(2026, 8, 3),
            status=Reservation.Status.CANCELED,
            booker_name="Cancel Today",
            booked_at=timezone.make_aware(
                datetime(2026, 6, 1, 12, 0),
                timezone=zagreb,
            ),
            canceled_at=timezone.make_aware(
                datetime(2026, 6, 4, 15, 0),
                timezone=zagreb,
            ),
        )

        resp = self.client.get(
            "/api/v1/reception/reservations/",
            {
                "booked_from": today.isoformat(),
                "booked_to": tomorrow.isoformat(),
                "include_canceled": "1",
            },
            **self.auth,
        )
        self.assertEqual(resp.status_code, 200)
        ids = {row["id"] for row in resp.json()}
        self.assertEqual(ids, {active_booked.id, canceled_today.id})

    def test_timeline_include_canceled_ilija_like(self):
        zagreb = ZoneInfo("Europe/Zagreb")
        june_4 = date(2026, 6, 4)
        june_5 = date(2026, 6, 5)
        june_6 = june_5 + timedelta(days=1)

        ilija_like = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            external_id="ext-ilija-like",
            booking_code="BK-832",
            check_in=date(2027, 8, 4),
            check_out=date(2027, 8, 5),
            status=Reservation.Status.CANCELED,
            booker_name="ilija saric",
            booked_at=timezone.make_aware(
                datetime(2026, 6, 4, 10, 0),
                timezone=zagreb,
            ),
            canceled_at=timezone.make_aware(
                datetime(2026, 6, 5, 0, 11, 56),
                timezone=zagreb,
            ),
        )

        resp_june_4 = self.client.get(
            "/api/v1/reception/reservations/",
            {
                "booked_from": june_4.isoformat(),
                "booked_to": june_5.isoformat(),
                "include_canceled": "1",
            },
            **self.auth,
        )
        self.assertEqual(resp_june_4.status_code, 200)
        ids_june_4 = {row["id"] for row in resp_june_4.json()}
        self.assertNotIn(ilija_like.id, ids_june_4)

        resp_june_5 = self.client.get(
            "/api/v1/reception/reservations/",
            {
                "booked_from": june_5.isoformat(),
                "booked_to": june_6.isoformat(),
                "include_canceled": "1",
            },
            **self.auth,
        )
        self.assertEqual(resp_june_5.status_code, 200)
        ids_june_5 = {row["id"] for row in resp_june_5.json()}
        self.assertEqual(ids_june_5, {ilija_like.id})

    def test_timeline_booked_and_canceled_params_implicit_combined(self):
        zagreb = ZoneInfo("Europe/Zagreb")
        today = date(2026, 6, 4)
        tomorrow = today + timedelta(days=1)

        canceled_today = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            external_id="ext-implicit-cancel",
            booking_code="BK-IC",
            check_in=date(2026, 9, 1),
            check_out=date(2026, 9, 3),
            status=Reservation.Status.CANCELED,
            booker_name="Implicit Cancel",
            canceled_at=timezone.make_aware(
                datetime(2026, 6, 4, 9, 0),
                timezone=zagreb,
            ),
        )

        resp = self.client.get(
            "/api/v1/reception/reservations/",
            {
                "booked_from": today.isoformat(),
                "booked_to": tomorrow.isoformat(),
                "canceled_from": today.isoformat(),
                "canceled_to": tomorrow.isoformat(),
            },
            **self.auth,
        )
        self.assertEqual(resp.status_code, 200)
        ids = {row["id"] for row in resp.json()}
        self.assertIn(canceled_today.id, ids)

    def test_timeline_booked_without_include_canceled_unchanged(self):
        zagreb = ZoneInfo("Europe/Zagreb")
        today = date(2026, 6, 4)
        tomorrow = today + timedelta(days=1)
        booked_at = timezone.make_aware(
            datetime(2026, 6, 4, 8, 0),
            timezone=zagreb,
        )

        canceled_same_day = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            external_id="ext-regression-canceled",
            booking_code="BK-RC",
            check_in=date(2026, 8, 4),
            check_out=date(2026, 8, 5),
            status=Reservation.Status.CANCELED,
            booker_name="Booked Then Canceled",
            booked_at=booked_at,
            canceled_at=timezone.make_aware(
                datetime(2026, 6, 4, 12, 0),
                timezone=zagreb,
            ),
        )
        active_booked = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            external_id="ext-regression-active",
            booking_code="BK-RA",
            check_in=date(2026, 8, 10),
            check_out=date(2026, 8, 12),
            status=Reservation.Status.EXPECTED,
            booker_name="Active Booked",
            booked_at=booked_at,
        )

        resp = self.client.get(
            "/api/v1/reception/reservations/",
            {
                "booked_from": today.isoformat(),
                "booked_to": tomorrow.isoformat(),
            },
            **self.auth,
        )
        self.assertEqual(resp.status_code, 200)
        ids = {row["id"] for row in resp.json()}
        self.assertEqual(ids, {active_booked.id})
        self.assertNotIn(canceled_same_day.id, ids)

    @patch("apps.reservations.checkin.property_local_now")
    @patch("apps.core.tasks.notify_reservation_status_changed.delay")
    def test_reservation_detail_and_patch_status(self, mock_notify_status, mock_local_now):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        mock_local_now.return_value = datetime(
            2026, 5, 10, 10, 0, tzinfo=ZoneInfo("Europe/Zagreb")
        )
        detail = self.client.get(
            f"/api/v1/reception/reservations/{self.reservation.id}/",
            **self.auth,
        )
        self.assertEqual(detail.status_code, 200)
        self.assertIn("booking_payout_received", detail.json())
        self.assertFalse(detail.json()["booking_payout_received"])

        patch = self.client.patch(
            f"/api/v1/reception/reservations/{self.reservation.id}/",
            {"status": Reservation.Status.CHECKED_IN},
            format="json",
            HTTP_AUTHORIZATION=self.auth["HTTP_AUTHORIZATION"],
            HTTP_X_INSTALLATION_ID="tablet-a-uuid",
        )
        self.assertEqual(patch.status_code, 200)
        self.assertEqual(patch.json()["status"], Reservation.Status.CHECKED_IN)
        mock_notify_status.assert_called_once_with(
            self.reservation.id,
            Reservation.Status.EXPECTED,
            Reservation.Status.CHECKED_IN,
            "tablet-a-uuid",
        )

    def test_reservation_detail_includes_booking_payout_fields(self):
        self.reservation.booking_payout_received_at = date(2026, 6, 11)
        self.reservation.booking_payout_id = "mmDVC1NdWECBgRWk"
        self.reservation.booking_payout_net = Decimal("74.77")
        self.reservation.booking_payout_service_fee = Decimal("1.20")
        self.reservation.save(
            update_fields=[
                "booking_payout_received_at",
                "booking_payout_id",
                "booking_payout_net",
                "booking_payout_service_fee",
            ]
        )
        response = self.client.get(
            f"/api/v1/reception/reservations/{self.reservation.id}/",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["booking_payout_received"])
        self.assertEqual(body["booking_payout_id"], "mmDVC1NdWECBgRWk")
        self.assertEqual(body["booking_payout_net"], "74.77")
        self.assertEqual(body["booking_payout_service_fee"], "1.20")
        self.assertEqual(body["booking_payout_received_at"], "2026-06-11")

    @patch("apps.reservations.checkin.property_local_now")
    def test_check_in_rejected_before_arrival_date(self, mock_local_now):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        mock_local_now.return_value = datetime(
            2026, 5, 9, 10, 0, tzinfo=ZoneInfo("Europe/Zagreb")
        )
        response = self.client.patch(
            f"/api/v1/reception/reservations/{self.reservation.id}/",
            {"status": Reservation.Status.CHECKED_IN},
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 400)
        body = response.json()
        error_text = body["status"] if isinstance(body["status"], str) else str(body["status"])
        self.assertIn("dan dolaska", error_text)

    @patch("apps.reservations.checkin.property_local_now")
    def test_check_in_rejected_when_room_occupied(self, mock_local_now):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        mock_local_now.return_value = datetime(
            2026, 5, 10, 10, 0, tzinfo=ZoneInfo("Europe/Zagreb")
        )
        other = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            check_in=date(2026, 5, 8),
            check_out=date(2026, 5, 12),
            status=Reservation.Status.CHECKED_IN,
            booker_name="Petra Petrić",
            amount=Decimal("90.00"),
        )
        ReservationUnit.objects.create(
            tenant=self.tenant,
            reservation=other,
            unit=self.unit,
            room_name="Soba 101",
            sort_order=0,
        )

        response = self.client.patch(
            f"/api/v1/reception/reservations/{self.reservation.id}/",
            {"status": Reservation.Status.CHECKED_IN},
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 400)
        body = response.json()
        error_text = body["status"] if isinstance(body["status"], str) else str(body["status"])
        self.assertIn("zauzeta", error_text)

    @patch("apps.reservations.checkin.property_local_now")
    @patch("apps.core.tasks.notify_reservation_status_changed.delay")
    def test_check_in_allowed_on_arrival_date_when_room_free(
        self,
        mock_notify_status,
        mock_local_now,
    ):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        mock_local_now.return_value = datetime(
            2026, 5, 10, 10, 0, tzinfo=ZoneInfo("Europe/Zagreb")
        )
        response = self.client.patch(
            f"/api/v1/reception/reservations/{self.reservation.id}/",
            {"status": Reservation.Status.CHECKED_IN},
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], Reservation.Status.CHECKED_IN)
        self.assertIsNotNone(body.get("evisitor_checkin"))
        mock_notify_status.assert_called_once()

    @patch("apps.reservations.checkin.property_local_now")
    @patch("apps.core.tasks.notify_reservation_status_changed.delay")
    @patch(
        "apps.reservations.reservation_checkin_complete.submit_guest_checkin",
    )
    def test_check_in_auto_evisitor_complete(
        self,
        mock_submit,
        mock_notify_status,
        mock_local_now,
    ):
        from datetime import datetime
        from types import SimpleNamespace
        from zoneinfo import ZoneInfo

        from apps.integrations.evisitor.metrics import (
            get_evisitor_checkin_auto_breakdown,
            reset_evisitor_checkin_auto_total,
        )

        reset_evisitor_checkin_auto_total()
        mock_local_now.return_value = datetime(
            2026, 5, 10, 10, 0, tzinfo=ZoneInfo("Europe/Zagreb")
        )
        mock_submit.return_value = SimpleNamespace(
            status="sent",
            registration_id="a01c2e9f-3839-4f0e-b39b-775e107d6f36",
        )

        response = self.client.patch(
            f"/api/v1/reception/reservations/{self.reservation.id}/",
            {"status": Reservation.Status.CHECKED_IN},
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], Reservation.Status.CHECKED_IN)
        ev = body["evisitor_checkin"]
        self.assertEqual(ev["overall"], "complete")
        self.assertEqual(ev["submitted"], 1)
        self.assertEqual(ev["failed"], 0)
        self.assertTrue(ev.get("correlation_id"))
        mock_submit.assert_called_once()
        self.assertEqual(
            mock_submit.call_args.kwargs.get("http_timeout"),
            8.0,
        )
        breakdown = get_evisitor_checkin_auto_breakdown()
        self.assertTrue(any(row["result"] == "complete" and row["count"] >= 1 for row in breakdown))
        mock_notify_status.assert_called_once()

    @patch("apps.reservations.checkin.property_local_now")
    @patch("apps.core.tasks.notify_reservation_status_changed.delay")
    @patch(
        "apps.reservations.reservation_checkin_complete.submit_evisitor_for_reservation",
    )
    def test_check_in_skips_evisitor_when_no_eligible_guests(
        self,
        mock_submit_reservation,
        mock_notify_status,
        mock_local_now,
    ):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from apps.integrations.evisitor.metrics import (
            get_evisitor_checkin_auto_breakdown,
            reset_evisitor_checkin_auto_total,
        )

        reset_evisitor_checkin_auto_total()
        mock_local_now.return_value = datetime(
            2026, 5, 10, 10, 0, tzinfo=ZoneInfo("Europe/Zagreb")
        )
        self.guest.date_of_birth = date(2015, 1, 1)
        self.guest.save(update_fields=["date_of_birth"])

        response = self.client.patch(
            f"/api/v1/reception/reservations/{self.reservation.id}/",
            {"status": Reservation.Status.CHECKED_IN},
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], Reservation.Status.CHECKED_IN)
        ev = body["evisitor_checkin"]
        self.assertEqual(ev["overall"], "not_required")
        self.assertEqual(ev["skipped"], 1)
        mock_submit_reservation.assert_not_called()
        breakdown = get_evisitor_checkin_auto_breakdown()
        self.assertTrue(
            any(row["result"] == "not_required" and row["count"] >= 1 for row in breakdown)
        )
        mock_notify_status.assert_called_once()

    @patch("apps.reservations.checkin.property_local_now")
    @patch("apps.core.tasks.notify_reservation_status_changed.delay")
    @patch(
        "apps.reservations.reservation_checkin_complete.submit_guest_checkin",
    )
    def test_check_in_auto_evisitor_partial_and_per_guest_continue(
        self,
        mock_submit,
        mock_notify_status,
        mock_local_now,
    ):
        from datetime import datetime
        from types import SimpleNamespace
        from zoneinfo import ZoneInfo

        from apps.integrations.evisitor.exceptions import EvisitorApiError

        mock_local_now.return_value = datetime(
            2026, 5, 10, 10, 0, tzinfo=ZoneInfo("Europe/Zagreb")
        )
        Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="Iva",
            last_name="Ivić",
            is_primary=False,
            date_of_birth=date(1990, 1, 1),
        )
        self.guest.date_of_birth = date(1985, 1, 1)
        self.guest.save(update_fields=["date_of_birth"])

        mock_submit.side_effect = [
            EvisitorApiError(
                "timeout",
                user_message="eVisitor zahtjev je istekao (timeout).",
            ),
            SimpleNamespace(
                status="sent",
                registration_id="b11c2e9f-3839-4f0e-b39b-775e107d6f36",
            ),
        ]

        response = self.client.patch(
            f"/api/v1/reception/reservations/{self.reservation.id}/",
            {"status": Reservation.Status.CHECKED_IN},
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], Reservation.Status.CHECKED_IN)
        ev = body["evisitor_checkin"]
        self.assertEqual(ev["overall"], "partial")
        self.assertEqual(ev["submitted"], 1)
        self.assertEqual(ev["failed"], 1)
        self.assertEqual(len(ev["failed_guests"]), 1)
        self.assertEqual(mock_submit.call_count, 2)

    @patch("apps.reservations.checkin.property_local_now")
    @patch("apps.core.tasks.notify_reservation_status_changed.delay")
    @patch(
        "apps.reservations.reservation_checkin_complete.submit_guest_checkin",
    )
    def test_already_checked_in_patch_does_not_retry_evisitor(
        self,
        mock_submit,
        mock_notify_status,
        mock_local_now,
    ):
        from datetime import datetime
        from types import SimpleNamespace
        from zoneinfo import ZoneInfo

        mock_local_now.return_value = datetime(
            2026, 5, 10, 10, 0, tzinfo=ZoneInfo("Europe/Zagreb")
        )
        mock_submit.return_value = SimpleNamespace(
            status="sent",
            registration_id="a01c2e9f-3839-4f0e-b39b-775e107d6f36",
        )

        first = self.client.patch(
            f"/api/v1/reception/reservations/{self.reservation.id}/",
            {"status": Reservation.Status.CHECKED_IN},
            format="json",
            **self.auth,
        )
        self.assertEqual(first.status_code, 200)
        self.assertIsNotNone(first.json().get("evisitor_checkin"))
        mock_submit.assert_called_once()

        second = self.client.patch(
            f"/api/v1/reception/reservations/{self.reservation.id}/",
            {"status": Reservation.Status.CHECKED_IN},
            format="json",
            **self.auth,
        )
        self.assertEqual(second.status_code, 200)
        self.assertIsNone(second.json().get("evisitor_checkin"))
        mock_submit.assert_called_once()

    @patch("apps.reservations.checkin.property_local_now")
    def test_detail_includes_check_in_allowed_on_arrival_date(self, mock_local_now):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        mock_local_now.return_value = datetime(
            2026, 5, 10, 10, 0, tzinfo=ZoneInfo("Europe/Zagreb")
        )
        response = self.client.get(
            f"/api/v1/reception/reservations/{self.reservation.id}/",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["check_in_allowed"])
        self.assertIsNone(data["check_in_blocked_code"])
        self.assertIsNone(data.get("evisitor_checkin"))

    @patch("apps.reservations.checkin.property_local_now")
    def test_detail_check_in_blocked_wrong_date(self, mock_local_now):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        mock_local_now.return_value = datetime(
            2026, 5, 9, 10, 0, tzinfo=ZoneInfo("Europe/Zagreb")
        )
        response = self.client.get(
            f"/api/v1/reception/reservations/{self.reservation.id}/",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["check_in_allowed"])
        self.assertEqual(data["check_in_blocked_code"], "wrong_date")

    @patch("apps.reservations.checkin.property_local_now")
    def test_detail_check_in_blocked_room_occupied(self, mock_local_now):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        mock_local_now.return_value = datetime(
            2026, 5, 10, 10, 0, tzinfo=ZoneInfo("Europe/Zagreb")
        )
        other = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            check_in=date(2026, 5, 8),
            check_out=date(2026, 5, 12),
            status=Reservation.Status.CHECKED_IN,
            booker_name="Petra Petrić",
            amount=Decimal("90.00"),
        )
        ReservationUnit.objects.create(
            tenant=self.tenant,
            reservation=other,
            unit=self.unit,
            room_name="Soba 101",
            sort_order=0,
        )
        response = self.client.get(
            f"/api/v1/reception/reservations/{self.reservation.id}/",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["check_in_allowed"])
        self.assertEqual(data["check_in_blocked_code"], "room_occupied")

    def test_patch_reservation_dates_expected(self):
        response = self.client.patch(
            f"/api/v1/reception/reservations/{self.reservation.id}/",
            {"check_in": "2026-05-20", "check_out": "2026-05-25"},
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["check_in_date"], "2026-05-20")
        self.assertEqual(response.json()["check_out_date"], "2026-05-25")
        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.check_in, date(2026, 5, 20))
        self.assertEqual(self.reservation.check_out, date(2026, 5, 25))

    def test_patch_reservation_dates_checked_in_rejected(self):
        self.reservation.status = Reservation.Status.CHECKED_IN
        self.reservation.save(update_fields=["status", "updated_at"])

        response = self.client.patch(
            f"/api/v1/reception/reservations/{self.reservation.id}/",
            {"check_in": "2026-05-20", "check_out": "2026-05-25"},
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 400)

    def test_patch_reservation_dates_overlap_rejected(self):
        Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            check_in=date(2026, 5, 22),
            check_out=date(2026, 5, 24),
            status=Reservation.Status.EXPECTED,
        )
        other = Reservation.objects.latest("id")
        ReservationUnit.objects.create(
            tenant=self.tenant,
            reservation=other,
            unit=self.unit,
        )

        response = self.client.patch(
            f"/api/v1/reception/reservations/{self.reservation.id}/",
            {"check_in": "2026-05-20", "check_out": "2026-05-25"},
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 400)

    def test_unit_availability_exclude_reservation_id(self):
        response = self.client.get(
            f"/api/v1/reception/units/{self.unit.id}/availability/"
            f"?from=2026-05-10&to=2026-05-20&exclude_reservation_id={self.reservation.id}",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["blocked_nights"], [])

        response_without_exclude = self.client.get(
            f"/api/v1/reception/units/{self.unit.id}/availability/"
            f"?from=2026-05-10&to=2026-05-20",
            **self.auth,
        )
        self.assertEqual(response_without_exclude.status_code, 200)
        self.assertEqual(
            response_without_exclude.json()["blocked_nights"],
            ["2026-05-10", "2026-05-11", "2026-05-12", "2026-05-13", "2026-05-14"],
        )

    @patch("apps.core.tasks.notify_reservation_status_changed.delay")
    @patch("apps.reservations.checkout.checkout_reservation_guests_in_evisitor")
    def test_patch_checkout_checked_in_to_checked_out(
        self,
        mock_evisitor_checkout,
        mock_notify_status,
    ):
        from apps.reservations.models import EvisitorGuestStatus

        self.reservation.status = Reservation.Status.CHECKED_IN
        self.reservation.save(update_fields=["status", "updated_at"])
        self.guest.evisitor_status = EvisitorGuestStatus.SENT
        self.guest.save(update_fields=["evisitor_status"])
        mock_evisitor_checkout.return_value = []

        response = self.client.patch(
            f"/api/v1/reception/reservations/{self.reservation.id}/",
            {"status": Reservation.Status.CHECKED_OUT},
            format="json",
            HTTP_AUTHORIZATION=self.auth["HTTP_AUTHORIZATION"],
            HTTP_X_INSTALLATION_ID="tablet-checkout",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], Reservation.Status.CHECKED_OUT)
        mock_evisitor_checkout.assert_called_once()
        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.status, Reservation.Status.CHECKED_OUT)
        mock_notify_status.assert_called_once_with(
            self.reservation.id,
            Reservation.Status.CHECKED_IN,
            Reservation.Status.CHECKED_OUT,
            "tablet-checkout",
        )

    @patch("apps.core.tasks.notify_reservation_status_changed.delay")
    @patch("apps.reservations.checkout.checkout_reservation_guests_in_evisitor")
    def test_patch_checkout_removes_unfilled_secondary_guests(
        self,
        mock_evisitor_checkout,
        mock_notify_status,
    ):
        from apps.reservations.guest_slots import PLACEHOLDER_FIRST, PLACEHOLDER_LAST
        from apps.reservations.models import EvisitorGuestStatus

        self.reservation.status = Reservation.Status.CHECKED_IN
        self.reservation.save(update_fields=["status", "updated_at"])
        self.guest.evisitor_status = EvisitorGuestStatus.SENT
        self.guest.save(update_fields=["evisitor_status"])
        placeholder = Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name=PLACEHOLDER_FIRST,
            last_name=PLACEHOLDER_LAST,
            name="Novi gost",
            is_primary=False,
            evisitor_status=EvisitorGuestStatus.NOT_SENT,
        )
        mock_evisitor_checkout.return_value = []

        response = self.client.patch(
            f"/api/v1/reception/reservations/{self.reservation.id}/",
            {"status": Reservation.Status.CHECKED_OUT},
            format="json",
            HTTP_AUTHORIZATION=self.auth["HTTP_AUTHORIZATION"],
            HTTP_X_INSTALLATION_ID="tablet-checkout-cleanup",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], Reservation.Status.CHECKED_OUT)
        self.assertFalse(Guest.objects.filter(pk=placeholder.pk).exists())
        mock_evisitor_checkout.assert_called_once()
        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.status, Reservation.Status.CHECKED_OUT)

    def test_create_guest(self):
        response = self.client.post(
            f"/api/v1/reception/reservations/{self.reservation.id}/guests/",
            {"first_name": "Petra", "last_name": "Petrić"},
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["first_name"], "Petra")

    @override_settings(MEDIA_ROOT="/tmp/stay_test_media")
    def test_id_scan_sample_upload(self):
        # Minimal valid JPEG (1x1)
        jpeg_bytes = (
            b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
            b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c"
            b"\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c"
            b"\x1c $.\x27 ,#\x1c\x1c(7),01444\x1f\x27=9=82<.7\xff\xc0\x00\x0b\x08"
            b"\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01"
            b"\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07"
            b"\x08\t\n\x0b\xff\xc4\x00\xb5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05"
            b"\x04\x04\x00\x00\x01}\x01\x02\x03\x00\x04\x11\x05\x12!1A\x06\x13Qa\x07"
            b"\"q\x142\x81\x91\xa1\x08#B\xb1\xc1\x15R\xd1\xf0$3br\x82\t\n\x16\x17\x18"
            b"\x19\x1a%&'()*456789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz\x83\x84\x85\x86"
            b"\x87\x88\x89\x8a\x92\x93\x94\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6"
            b"\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4\xc5\xc6"
            b"\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe1\xe2\xe3\xe4\xe5"
            b"\xe6\xe7\xe8\xe9\xea\xf1\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xda\x00"
            b"\x08\x01\x01\x00\x00?\x00\xfb\xd5\xdb\x20\xff\xd9"
        )
        image = SimpleUploadedFile("sample.jpg", jpeg_bytes, content_type="image/jpeg")
        response = self.client.post(
            f"/api/v1/reception/reservations/{self.reservation.id}/guests/{self.guest.id}/id-scan-samples/",
            {
                "image": image,
                "document_type": "passport",
                "source": "mrz_plus",
                "raw_mrz": "P<HRVMARKO<<MARKOVIC",
                "device_id": "tablet-test",
            },
            format="multipart",
            **self.auth,
        )
        self.assertEqual(response.status_code, 201)
        sample_id = response.json()["sample_id"]
        sample = IdRecognitionSample.objects.get(pk=sample_id)
        self.assertEqual(sample.tenant_id, self.tenant.id)
        self.assertEqual(sample.source, "mrz_plus")
        self.assertTrue(sample.image.name)
        self.assertTrue(io.BytesIO(sample.image.read()).getvalue())

    def test_document_scan_ingest(self):
        response = self.client.post(
            f"/api/v1/reception/reservations/{self.reservation.id}/guests/{self.guest.id}/document-scan/",
            {
                "metapodaci": {"metoda_ocitanja": "NFC", "tip_dokumenta": "passport"},
                "podaci_gosta": {
                    "ime": "Marko",
                    "prezime": "Marković",
                    "broj_dokumenta": "P1234567",
                },
                "sirovi_mrz": "P<HRVMARKO<<MARKOVIC",
            },
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["scan_status"], "ok")
        self.guest.refresh_from_db()
        self.assertEqual(self.guest.document_number, "P1234567")

    @patch("apps.api.reception_views.submit_guest_checkin")
    def test_evisitor_submit(self, mock_submit):
        from apps.reservations.models import EvisitorSubmission
        from django.utils import timezone
        import uuid

        mock_submit.return_value = EvisitorSubmission(
            tenant=self.tenant,
            guest=self.guest,
            registration_id=uuid.uuid4(),
            status="sent",
            submitted_at=timezone.now(),
            created_at=timezone.now(),
        )
        response = self.client.post(
            f"/api/v1/reception/reservations/{self.reservation.id}/guests/{self.guest.id}/evisitor-submit/",
            {},
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        mock_submit.assert_called_once()

    def test_sync_versions(self):
        response = self.client.get(
            "/api/v1/reception/sync-versions/?year=2026",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("reservations", data)
        self.assertIn("rooms", data)
        self.assertIn("2026", data["statistics"])
        self.assertEqual(len(data["reservations"]), 16)
        etag = response["ETag"]
        self.assertTrue(etag.startswith('W/"'))
        self.assertTrue(etag.endswith('"'))

        cached = self.client.get(
            "/api/v1/reception/sync-versions/?year=2026",
            HTTP_IF_NONE_MATCH=etag,
            **self.auth,
        )
        self.assertEqual(cached.status_code, 304)
        self.assertEqual(cached.content, b"")

        self.reservation.status = Reservation.Status.CHECKED_IN
        self.reservation.save(update_fields=["status", "updated_at"])

        after_change = self.client.get(
            "/api/v1/reception/sync-versions/?year=2026",
            HTTP_IF_NONE_MATCH=etag,
            **self.auth,
        )
        self.assertEqual(after_change.status_code, 200)
        self.assertNotEqual(after_change["ETag"], etag)

    def test_sync_versions_with_reservation_id(self):
        response = self.client.get(
            f"/api/v1/reception/sync-versions/?year=2026&reservation_id={self.reservation.id}",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("reservation_detail", data)
        self.assertEqual(len(data["reservation_detail"]), 16)
        etag = response["ETag"]

        cached = self.client.get(
            f"/api/v1/reception/sync-versions/?year=2026&reservation_id={self.reservation.id}",
            HTTP_IF_NONE_MATCH=etag,
            **self.auth,
        )
        self.assertEqual(cached.status_code, 304)

        self.reservation.status = Reservation.Status.CHECKED_IN
        self.reservation.save(update_fields=["status", "updated_at"])

        after_change = self.client.get(
            f"/api/v1/reception/sync-versions/?year=2026&reservation_id={self.reservation.id}",
            HTTP_IF_NONE_MATCH=etag,
            **self.auth,
        )
        self.assertEqual(after_change.status_code, 200)
        self.assertNotEqual(after_change["ETag"], etag)
        self.assertNotEqual(
            after_change.json()["reservation_detail"],
            data["reservation_detail"],
        )

    def test_sync_versions_reservation_id_not_found(self):
        response = self.client.get(
            "/api/v1/reception/sync-versions/?year=2026&reservation_id=999999",
            **self.auth,
        )
        self.assertEqual(response.status_code, 404)

    def test_sync_versions_reservation_id_other_tenant_returns_404(self):
        other_tenant = Tenant.objects.create(name="Other", slug="other")
        other_property = Property.objects.create(
            tenant=other_tenant,
            name="Other",
            slug="other",
        )
        other_reservation = Reservation.objects.create(
            tenant=other_tenant,
            property=other_property,
            check_in=date(2026, 6, 1),
            check_out=date(2026, 6, 5),
            status=Reservation.Status.EXPECTED,
            booker_name="Other guest",
            amount=Decimal("80.00"),
        )

        full = self.client.get(
            f"/api/v1/reception/sync-versions/?year=2026&reservation_id={other_reservation.id}",
            **self.auth,
        )
        self.assertEqual(full.status_code, 404)

        scoped = self.client.get(
            f"/api/v1/reception/sync-versions/?reservation_id={other_reservation.id}&scope=messages",
            **self.auth,
        )
        self.assertEqual(scoped.status_code, 404)

    def test_sync_versions_etag_differs_between_full_and_scoped_payload(self):
        full = self.client.get(
            f"/api/v1/reception/sync-versions/?year=2026&reservation_id={self.reservation.id}",
            **self.auth,
        )
        scoped = self.client.get(
            f"/api/v1/reception/sync-versions/?reservation_id={self.reservation.id}&scope=messages",
            **self.auth,
        )
        self.assertEqual(full.status_code, 200)
        self.assertEqual(scoped.status_code, 200)
        self.assertNotEqual(full["ETag"], scoped["ETag"])

    def test_sync_versions_scope_messages_returns_zero_without_row(self):
        response = self.client.get(
            f"/api/v1/reception/sync-versions/?reservation_id={self.reservation.id}&scope=messages",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data, {"versions": {"messages": 0}})
        self.assertNotIn("reservations", data)
        etag = response["ETag"]
        self.assertTrue(etag.startswith('W/"'))

    def test_sync_versions_scope_messages_returns_version_after_touch(self):
        touch_reservation_version(
            self.reservation.id,
            ReservationVersionScope.MESSAGES,
            reason="test",
        )
        response = self.client.get(
            f"/api/v1/reception/sync-versions/?reservation_id={self.reservation.id}&scope=messages",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"versions": {"messages": 1}})

    def test_sync_versions_scope_messages_etag_304(self):
        response = self.client.get(
            f"/api/v1/reception/sync-versions/?reservation_id={self.reservation.id}&scope=messages",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        etag = response["ETag"]

        cached = self.client.get(
            f"/api/v1/reception/sync-versions/?reservation_id={self.reservation.id}&scope=messages",
            HTTP_IF_NONE_MATCH=etag,
            **self.auth,
        )
        self.assertEqual(cached.status_code, 304)
        self.assertEqual(cached.content, b"")

        touch_reservation_version(
            self.reservation.id,
            ReservationVersionScope.MESSAGES,
            reason="test",
        )
        after_change = self.client.get(
            f"/api/v1/reception/sync-versions/?reservation_id={self.reservation.id}&scope=messages",
            HTTP_IF_NONE_MATCH=etag,
            **self.auth,
        )
        self.assertEqual(after_change.status_code, 200)
        self.assertNotEqual(after_change["ETag"], etag)
        self.assertEqual(after_change.json(), {"versions": {"messages": 1}})

    def test_sync_versions_scope_all(self):
        touch_reservation_version(self.reservation.id, ReservationVersionScope.MESSAGES)
        touch_reservation_version(self.reservation.id, ReservationVersionScope.PAYMENTS)
        response = self.client.get(
            f"/api/v1/reception/sync-versions/?reservation_id={self.reservation.id}&scope=all",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"versions": {"messages": 1, "payments": 1}},
        )

    def test_sync_versions_scope_invalid(self):
        response = self.client.get(
            f"/api/v1/reception/sync-versions/?reservation_id={self.reservation.id}&scope=guest_messages",
            **self.auth,
        )
        self.assertEqual(response.status_code, 400)

    def test_sync_versions_scope_requires_reservation_id(self):
        response = self.client.get(
            "/api/v1/reception/sync-versions/?year=2026&scope=messages",
            **self.auth,
        )
        self.assertEqual(response.status_code, 400)

    def test_sync_versions_with_reservation_id_includes_versions(self):
        touch_reservation_version(self.reservation.id, ReservationVersionScope.MESSAGES)
        response = self.client.get(
            f"/api/v1/reception/sync-versions/?year=2026&reservation_id={self.reservation.id}",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("reservation_detail", data)
        self.assertIn("versions", data)
        self.assertEqual(data["versions"], {"messages": 1})

    def test_reservation_version_stream_requires_params(self):
        response = self.client.get(
            "/api/v1/reception/reservation-versions/stream/",
            **self.auth,
        )
        self.assertEqual(response.status_code, 400)

    def test_reservation_version_stream_rejects_scope_all(self):
        response = self.client.get(
            f"/api/v1/reception/reservation-versions/stream/?reservation_id={self.reservation.id}&scope=all",
            **self.auth,
        )
        self.assertEqual(response.status_code, 400)

    def test_reservation_version_stream_not_found(self):
        response = self.client.get(
            "/api/v1/reception/reservation-versions/stream/?reservation_id=999999&scope=messages",
            **self.auth,
        )
        self.assertEqual(response.status_code, 404)

    def test_reservation_version_stream_connected_event(self):
        response = self.client.get(
            f"/api/v1/reception/reservation-versions/stream/?reservation_id={self.reservation.id}&scope=messages",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/event-stream", response["Content-Type"])
        self.assertTrue(response.get("X-SSE-Stream-Id"))
        first_chunk = next(response.streaming_content).decode("utf-8")
        self.assertIn("event: connected", first_chunk)
        self.assertIn('"scope":"messages"', first_chunk)
        self.assertIn('"stream_id":', first_chunk)
        _close_sse_streaming_content(response)

    @patch.dict(os.environ, {"GUNICORN_WORKERS": "2"}, clear=False)
    @patch(
        "apps.api.reception_views.get_sse_connection_stats",
        return_value={"active_connections": 1},
    )
    def test_reservation_version_stream_rejects_when_worker_saturated(self, _mock_stats):
        response = self.client.get(
            f"/api/v1/reception/reservation-versions/stream/?reservation_id={self.reservation.id}&scope=messages",
            **self.auth,
        )
        self.assertEqual(response.status_code, 503)
        self.assertIn("poll fallback", response.json()["detail"])

    def test_reservation_version_stream_closed_on_client_disconnect(self):
        from apps.reservations.reservation_version_events import (
            get_sse_connection_stats,
            reset_sse_connection_state_for_tests,
        )

        reset_sse_connection_state_for_tests()
        with self.assertLogs("apps.api.reception_views", level="INFO") as logs:
            response = self.client.get(
                f"/api/v1/reception/reservation-versions/stream/?reservation_id={self.reservation.id}&scope=messages",
                **self.auth,
            )
            self.assertEqual(response.status_code, 200)
            stream_id = response.get("X-SSE-Stream-Id")
            self.assertTrue(stream_id)
            mid = get_sse_connection_stats()
            self.assertEqual(mid["active_connections"], 1)
            self.assertEqual(mid["active_stream_count"], 1)
            self.assertTrue(mid["invariant_ok"])
            self.assertEqual(mid["invariant_delta"], 0)
            self.assertEqual(mid["active_streams"][0]["stream_id"], stream_id)
            next(response.streaming_content)
            _close_sse_streaming_content(response)
        closed = [line for line in logs.output if "sse_stream_closed" in line]
        self.assertTrue(closed, logs.output)
        self.assertTrue(
            any(f"stream_id={stream_id}" in line for line in closed),
            closed,
        )
        self.assertTrue(
            any("close_reason=client_disconnect" in line for line in closed),
            closed,
        )
        after = get_sse_connection_stats()
        self.assertEqual(after["active_connections"], 0)
        self.assertEqual(after["active_stream_count"], 0)
        self.assertEqual(after["active_streams"], [])
        self.assertEqual(after["invariant_delta"], 0)
        self.assertTrue(after["invariant_ok"])
        self.assertEqual(after["connections_opened_total"], 1)
        self.assertEqual(after["connections_closed_total"], 1)

    def test_reservation_version_stream_finally_always_cleans_up(self):
        """finally must always unsubscribe + record close + registry close."""
        from apps.reservations.reservation_version_event_bus import (
            get_reservation_version_event_bus,
        )
        from apps.reservations.reservation_version_events import (
            reset_sse_connection_state_for_tests,
        )

        reset_sse_connection_state_for_tests()
        event_bus = get_reservation_version_event_bus()
        with (
            patch.object(event_bus, "unsubscribe") as mock_unsub,
            patch("apps.api.reception_views.record_sse_stream_closed") as mock_record,
            patch("apps.api.reception_views.close_sse_stream") as mock_close,
            patch("apps.api.reception_views.check_sse_invariant") as mock_invariant,
        ):
            # Real subscribe/register still run so the stream starts; cleanup is mocked.
            response = self.client.get(
                f"/api/v1/reception/reservation-versions/stream/?reservation_id={self.reservation.id}&scope=messages",
                **self.auth,
            )
            self.assertEqual(response.status_code, 200)
            stream_id = response.get("X-SSE-Stream-Id")
            next(response.streaming_content)
            _close_sse_streaming_content(response)
            mock_record.assert_called_once()
            mock_unsub.assert_called_once()
            mock_close.assert_called_once()
            self.assertEqual(mock_close.call_args.args[0], stream_id)
            self.assertEqual(mock_close.call_args.args[1], "client_disconnect")
            # open + close each call check_sse_invariant
            self.assertGreaterEqual(mock_invariant.call_count, 2)
        reset_sse_connection_state_for_tests()

    def test_monthly_statistics(self):
        self.reservation.status = Reservation.Status.CHECKED_IN
        self.reservation.save(update_fields=["status", "updated_at"])

        response = self.client.get(
            "/api/v1/reception/statistics/monthly/?year=2026",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["year"], 2026)
        self.assertEqual(data["comparison_year"], 2025)
        self.assertEqual(len(data["months"]), 12)
        may = next(m for m in data["months"] if m["month"] == 5)
        self.assertEqual(may["current"]["revenue"], "120.00")

    def test_monthly_statistics_invalid_year(self):
        response = self.client.get(
            "/api/v1/reception/statistics/monthly/?year=abc",
            **self.auth,
        )
        self.assertEqual(response.status_code, 400)

    def test_guest_countries_statistics(self):
        self.reservation.status = Reservation.Status.CHECKED_IN
        self.reservation.save(update_fields=["status", "updated_at"])
        self.guest.nationality = "DE"
        self.guest.save(update_fields=["nationality"])

        reservation_two = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            external_id="ext-gc-2",
            booking_code="BK-GC-2",
            check_in=date(2026, 6, 1),
            check_out=date(2026, 6, 5),
            status=Reservation.Status.CHECKED_OUT,
            booker_name="Other Booker",
            amount=Decimal("100.00"),
        )
        Guest.objects.create(
            tenant=self.tenant,
            reservation=reservation_two,
            first_name="Hans",
            last_name="Müller",
            is_primary=True,
            nationality="AT",
        )
        Guest.objects.create(
            tenant=self.tenant,
            reservation=reservation_two,
            first_name="Unknown",
            last_name="Guest",
            is_primary=False,
        )

        response = self.client.get(
            "/api/v1/reception/statistics/guest-countries/?year=2026",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["year"], 2026)
        self.assertEqual(data["total_guests"], 3)

        by_iso = {row["iso2"]: row for row in data["countries"]}
        self.assertEqual(by_iso["DE"]["guest_count"], 1)
        self.assertAlmostEqual(by_iso["DE"]["share"], 1 / 3, places=3)
        self.assertEqual(by_iso["AT"]["guest_count"], 1)
        self.assertAlmostEqual(by_iso["AT"]["share"], 1 / 3, places=3)
        self.assertEqual(by_iso[""]["guest_count"], 1)
        self.assertAlmostEqual(by_iso[""]["share"], 1 / 3, places=3)

        guest_counts = [row["guest_count"] for row in data["countries"]]
        self.assertEqual(guest_counts, sorted(guest_counts, reverse=True))

    def test_guest_countries_statistics_invalid_year(self):
        response = self.client.get(
            "/api/v1/reception/statistics/guest-countries/?year=abc",
            **self.auth,
        )
        self.assertEqual(response.status_code, 400)

    def test_read_scope_blocks_write(self):
        read_only_app, read_token = ApiApplication.create_with_token(
            tenant=self.tenant,
            name="Read only",
            scopes=["reception:read"],
        )
        response = self.client.patch(
            f"/api/v1/reception/reservations/{self.reservation.id}/",
            {"status": Reservation.Status.CHECKED_IN},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {read_token}",
        )
        self.assertEqual(response.status_code, 403)

    def test_import_booking_pdf(self):
        pdf_path = Path(__file__).resolve().parents[4] / ".imports" / "5145601516.pdf"
        if not pdf_path.is_file():
            self.skipTest("Sample PDF not available")

        with pdf_path.open("rb") as handle:
            upload = SimpleUploadedFile(
                "5145601516.pdf",
                handle.read(),
                content_type="application/pdf",
            )

        response = self.client.post(
            "/api/v1/reception/reservations/import-pdf/",
            {"file": upload, "property_slug": "uzorita"},
            format="multipart",
            **self.auth,
        )
        self.assertIn(response.status_code, {200, 201})
        data = response.json()
        self.assertEqual(data["external_id"], "5145601516")
        self.assertEqual(data["import_source"], "booking_pdf")
        self.assertTrue(data["pdf_imported_at"])
        self.assertTrue(data["confirmation_pdf_url"])
        self.assertIn("confirmation-pdf", data["confirmation_pdf_url"])

        reservation = Reservation.objects.get(pk=data["id"])
        self.assertEqual(reservation.import_source, "booking_pdf")
        self.assertIsNotNone(reservation.pdf_imported_at)
        self.assertIn("5145601516", reservation.confirmation_pdf.name)

    def test_import_booking_pdf_matching_reservation_id(self):
        pdf_path = Path(__file__).resolve().parents[4] / ".imports" / "5145601516.pdf"
        if not pdf_path.is_file():
            self.skipTest("Sample PDF not available")

        reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            external_id="5145601516",
            booking_code="5145601516",
            check_in=date(2026, 5, 23),
            check_out=date(2026, 5, 24),
            status=Reservation.Status.EXPECTED,
            booker_name="Peter Boogaart",
        )

        with pdf_path.open("rb") as handle:
            upload = SimpleUploadedFile(
                "5145601516.pdf",
                handle.read(),
                content_type="application/pdf",
            )

        response = self.client.post(
            "/api/v1/reception/reservations/import-pdf/",
            {
                "file": upload,
                "property_slug": "uzorita",
                "reservation_id": str(reservation.id),
            },
            format="multipart",
            **self.auth,
        )
        self.assertIn(response.status_code, {200, 201})
        self.assertEqual(response.json()["external_id"], "5145601516")

    def test_import_booking_pdf_mismatch_requires_confirm(self):
        pdf_path = Path(__file__).resolve().parents[4] / ".imports" / "5145601516.pdf"
        if not pdf_path.is_file():
            self.skipTest("Sample PDF not available")

        other = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            external_id="6250886338",
            booking_code="6250886338",
            check_in=date(2026, 5, 23),
            check_out=date(2026, 5, 24),
            status=Reservation.Status.EXPECTED,
            booker_name="Other Guest",
        )

        with pdf_path.open("rb") as handle:
            upload = SimpleUploadedFile(
                "5145601516.pdf",
                handle.read(),
                content_type="application/pdf",
            )

        response = self.client.post(
            "/api/v1/reception/reservations/import-pdf/",
            {
                "file": upload,
                "property_slug": "uzorita",
                "reservation_id": str(other.id),
            },
            format="multipart",
            **self.auth,
        )
        self.assertEqual(response.status_code, 409)
        data = response.json()
        self.assertEqual(data["code"], "booking_number_mismatch")
        self.assertEqual(data["pdf_booking_number"], "5145601516")
        self.assertEqual(data["context_booking_number"], "6250886338")
        self.assertEqual(data["context_reservation_id"], other.id)

    def test_import_booking_pdf_mismatch_confirmed(self):
        pdf_path = Path(__file__).resolve().parents[4] / ".imports" / "5145601516.pdf"
        if not pdf_path.is_file():
            self.skipTest("Sample PDF not available")

        other = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            external_id="6250886338",
            booking_code="6250886338",
            check_in=date(2026, 5, 23),
            check_out=date(2026, 5, 24),
            status=Reservation.Status.EXPECTED,
            booker_name="Other Guest",
        )

        with pdf_path.open("rb") as handle:
            upload = SimpleUploadedFile(
                "5145601516.pdf",
                handle.read(),
                content_type="application/pdf",
            )

        response = self.client.post(
            "/api/v1/reception/reservations/import-pdf/",
            {
                "file": upload,
                "property_slug": "uzorita",
                "reservation_id": str(other.id),
                "confirm_booking_mismatch": "true",
            },
            format="multipart",
            **self.auth,
        )
        self.assertIn(response.status_code, {200, 201})
        data = response.json()
        self.assertEqual(data["external_id"], "5145601516")
        self.assertNotEqual(data["id"], other.id)

    def test_import_booking_pdf_requires_property_slug_for_multi_property_tenant(self):
        Property.objects.create(
            tenant=self.tenant,
            name="Second Property",
            slug="second-property",
        )

        response = self.client.post(
            "/api/v1/reception/reservations/import-pdf/",
            {"file": SimpleUploadedFile("empty.pdf", b"%PDF-1.4", content_type="application/pdf")},
            format="multipart",
            **self.auth,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("property_slug", response.json())

    def test_import_booking_pdf_uses_reservation_property_over_form_slug(self):
        pdf_path = Path(__file__).resolve().parents[4] / ".imports" / "5145601516.pdf"
        if not pdf_path.is_file():
            self.skipTest("Sample PDF not available")

        other_property = Property.objects.create(
            tenant=self.tenant,
            name="Other Property",
            slug="other-property",
        )
        reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=other_property,
            external_id="5145601516",
            booking_code="5145601516",
            check_in=date(2026, 5, 23),
            check_out=date(2026, 5, 24),
            status=Reservation.Status.EXPECTED,
            booker_name="Peter Boogaart",
        )

        with pdf_path.open("rb") as handle:
            upload = SimpleUploadedFile(
                "5145601516.pdf",
                handle.read(),
                content_type="application/pdf",
            )

        response = self.client.post(
            "/api/v1/reception/reservations/import-pdf/",
            {
                "file": upload,
                "property_slug": "uzorita",
                "reservation_id": str(reservation.id),
            },
            format="multipart",
            **self.auth,
        )
        self.assertIn(response.status_code, {200, 201})
        reservation.refresh_from_db()
        self.assertEqual(reservation.property_id, other_property.id)

    @patch("apps.core.tasks.notify_reservation_status_changed.delay")
    def test_patch_status_to_no_show(self, mock_notify_status):
        response = self.client.patch(
            f"/api/v1/reception/reservations/{self.reservation.id}/",
            {"status": Reservation.Status.NO_SHOW, "waived_fees": True},
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], Reservation.Status.NO_SHOW)
        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.status, Reservation.Status.NO_SHOW)
        self.assertEqual(self.reservation.booking_status, "no_show")
        mock_notify_status.assert_called_once()

    def test_patch_status_to_no_show_rejected_from_checked_in(self):
        self.reservation.status = Reservation.Status.CHECKED_IN
        self.reservation.save(update_fields=["status", "updated_at"])
        response = self.client.patch(
            f"/api/v1/reception/reservations/{self.reservation.id}/",
            {"status": Reservation.Status.NO_SHOW},
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 400)

