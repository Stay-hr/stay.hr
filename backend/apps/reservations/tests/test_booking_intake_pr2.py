"""PR2 booking intake: parse (mocked LLM) + idempotent confirm."""

from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.properties.models import Property, Unit
from apps.reservations.booking_intake_models import BookingIntakeDraft
from apps.reservations.booking_intake_service import ConfirmPayload, confirm_draft
from apps.reservations.create_reception_reservation import create_reception_reservation
from apps.reservations.models import Reservation
from apps.tenants.models import ChannelManager, Tenant, TenantMembership, TenantReceptionSettings

User = get_user_model()

AKD_TEXT = """
24.8. - 28.8.
Soba R2
100 € po noći
Podaci tvrtke:
AKD-ZAŠTITA D.O.O.
Savska cesta 28, 10 000 Zagreb
OIB: 09253797076
Gost: Hrvoje Hrčka
Email: luka.golik@akdz.hr
+385 99 163 6951
"""


def _llm_payload(**overrides):
    base = {
        "property_slug": "uzorita",
        "unit_code": "R2",
        "check_in": "2026-08-24",
        "check_out": "2026-08-28",
        "nightly_rate": "100.00",
        "amount": "400.00",
        "currency": "EUR",
        "booker_name": "AKD-ZAŠTITA D.O.O. / Hrvoje Hrčka",
        "booker_phone": "+385991636951",
        "booker_email": "luka.golik@akdz.hr",
        "booker_address": "Savska cesta 28, 10000 Zagreb",
        "buyer_company_name": "AKD-ZAŠTITA D.O.O.",
        "buyer_oib": "09253797076",
        "buyer_address": "Savska cesta 28, 10000 Zagreb",
        "invoice_email": "luka.golik@akdz.hr",
        "guest_first_name": "Hrvoje",
        "guest_last_name": "Hrčka",
        "missing_fields": [],
    }
    base.update(overrides)
    return base


class BookingIntakeTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="uzorita-intake", name="Uzorita Intake")
        TenantReceptionSettings.objects.create(
            tenant=self.tenant,
            channel_manager=ChannelManager.NONE,
        )
        self.property = Property.objects.create(
            tenant=self.tenant,
            slug="uzorita",
            name="Uzorita",
            timezone="Europe/Zagreb",
        )
        self.unit = Unit.objects.create(
            tenant=self.tenant,
            property=self.property,
            code="R2",
            name="Room 2",
        )
        self.staff = User.objects.create_user(
            username="intake_staff", password="secret-pass", is_staff=True
        )
        TenantMembership.objects.create(user=self.staff, tenant=self.tenant)
        self.client = APIClient()

    def _login(self):
        self.client.post(
            "/api/v1/auth/reception-login/",
            {
                "username": "intake_staff",
                "password": "secret-pass",
                "tenant_id": self.tenant.pk,
            },
            format="json",
            HTTP_HOST="app.stay.hr",
        )

    @patch("apps.integrations.channel_manager.tasks.sync_reservation_outbound_task.delay")
    @patch("apps.reservations.booking_intake_parse.llm_configured", return_value=True)
    @patch("apps.reservations.booking_intake_parse.complete_chat_json")
    def test_parse_creates_draft(self, mock_llm, _cfg, _delay):
        mock_llm.return_value = _llm_payload()
        self._login()
        response = self.client.post(
            "/api/v1/reception/booking-intake/parse/",
            {"raw_text": AKD_TEXT, "property_slug": "uzorita"},
            format="json",
            HTTP_HOST="app.stay.hr",
        )
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        self.assertEqual(data["status"], "draft")
        self.assertEqual(data["unit_code"], "R2")
        self.assertEqual(data["unit_id"], self.unit.pk)
        self.assertEqual(data["amount"], "400.00")
        self.assertEqual(data["buyer_oib"], "09253797076")
        self.assertEqual(BookingIntakeDraft.objects.filter(tenant=self.tenant).count(), 1)

    @patch("apps.integrations.channel_manager.tasks.sync_reservation_outbound_task.delay")
    @patch("apps.reservations.booking_intake_parse.llm_configured", return_value=True)
    @patch("apps.reservations.booking_intake_parse.complete_chat_json")
    def test_confirm_creates_reservation_via_canonical_service(self, mock_llm, _cfg, _delay):
        mock_llm.return_value = _llm_payload()
        self._login()
        parse = self.client.post(
            "/api/v1/reception/booking-intake/parse/",
            {"raw_text": AKD_TEXT, "property_slug": "uzorita"},
            format="json",
            HTTP_HOST="app.stay.hr",
        )
        draft_id = parse.json()["id"]
        from apps.reservations import booking_intake_service as svc

        with patch.object(svc, "create_reception_reservation", wraps=create_reception_reservation) as wrapped:
            response = self.client.post(
                "/api/v1/reception/booking-intake/confirm/",
                {
                    "draft_id": draft_id,
                    "property_slug": "uzorita",
                    "unit_id": self.unit.pk,
                    "check_in": "2026-08-24",
                    "check_out": "2026-08-28",
                    "booker_name": "AKD-ZAŠTITA D.O.O. / Hrvoje Hrčka",
                    "booker_phone": "+385991636951",
                    "booker_email": "luka.golik@akdz.hr",
                    "booker_address": "Savska cesta 28, 10000 Zagreb",
                    "amount": "400.00",
                    "buyer_company_name": "AKD-ZAŠTITA D.O.O.",
                    "buyer_oib": "09253797076",
                    "buyer_address": "Savska cesta 28, 10000 Zagreb",
                    "invoice_email": "luka.golik@akdz.hr",
                    "guest_first_name": "Hrvoje",
                    "guest_last_name": "Hrčka",
                },
                format="json",
                HTTP_HOST="app.stay.hr",
            )
            self.assertEqual(response.status_code, 200, response.content)
            wrapped.assert_called_once()
        body = response.json()
        self.assertEqual(body["draft"]["status"], "confirmed")
        reservation = Reservation.objects.get(pk=body["reservation"]["id"])
        self.assertEqual(reservation.amount, Decimal("400.00"))
        self.assertEqual(reservation.buyer_company_name, "AKD-ZAŠTITA D.O.O.")
        self.assertEqual(reservation.import_source, "manual")
        self.assertEqual(reservation.guests.count(), 1)

    @patch("apps.integrations.channel_manager.tasks.sync_reservation_outbound_task.delay")
    def test_confirm_is_idempotent(self, _delay):
        draft = BookingIntakeDraft.objects.create(
            tenant=self.tenant,
            status=BookingIntakeDraft.Status.DRAFT,
            raw_text=AKD_TEXT,
            property_slug="uzorita",
            unit_id=self.unit.pk,
            unit_code="R2",
            check_in=date(2026, 8, 24),
            check_out=date(2026, 8, 28),
            amount=Decimal("400.00"),
            booker_name="AKD",
        )
        payload = ConfirmPayload(
            property_slug="uzorita",
            unit_id=self.unit.pk,
            check_in=date(2026, 8, 24),
            check_out=date(2026, 8, 28),
            booker_name="AKD",
            amount=Decimal("400.00"),
            guest_first_name="Hrvoje",
            guest_last_name="Hrčka",
        )
        draft1, res1 = confirm_draft(tenant=self.tenant, draft_id=draft.pk, payload=payload)
        draft2, res2 = confirm_draft(tenant=self.tenant, draft_id=draft.pk, payload=payload)
        self.assertEqual(res1.pk, res2.pk)
        self.assertEqual(Reservation.objects.filter(tenant=self.tenant).count(), 1)
        self.assertEqual(draft1.status, BookingIntakeDraft.Status.CONFIRMED)
        self.assertEqual(draft2.status, BookingIntakeDraft.Status.CONFIRMED)

    @patch("apps.integrations.channel_manager.tasks.sync_reservation_outbound_task.delay")
    def test_confirm_checks_availability(self, _delay):
        # Occupy R2 for the dates via an existing reservation.
        from apps.reservations.create_reception_reservation import (
            CreateReceptionReservationInput,
        )

        create_reception_reservation(
            CreateReceptionReservationInput(
                tenant=self.tenant,
                property=self.property,
                unit=self.unit,
                check_in=date(2026, 8, 24),
                check_out=date(2026, 8, 28),
                booker_name="Existing",
                amount=Decimal("100.00"),
            )
        )
        draft = BookingIntakeDraft.objects.create(
            tenant=self.tenant,
            status=BookingIntakeDraft.Status.DRAFT,
            raw_text=AKD_TEXT,
            property_slug="uzorita",
        )
        payload = ConfirmPayload(
            property_slug="uzorita",
            unit_id=self.unit.pk,
            check_in=date(2026, 8, 24),
            check_out=date(2026, 8, 28),
            booker_name="AKD",
            amount=Decimal("400.00"),
        )
        from apps.reservations.booking_intake_service import BookingIntakeError

        with self.assertRaises(BookingIntakeError) as ctx:
            confirm_draft(tenant=self.tenant, draft_id=draft.pk, payload=payload)
        self.assertEqual(ctx.exception.code, "create_failed")
        draft.refresh_from_db()
        self.assertEqual(draft.status, BookingIntakeDraft.Status.DRAFT)
