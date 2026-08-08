from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.properties.models import Property
from apps.reservations.guest_checkin_orchestrator import GuestCheckInOrchestrator
from apps.reservations.models import (
    Guest,
    GuestCheckInSessionCreatedFrom,
    GuestCheckInSessionStatus,
    Reservation,
)
from apps.tenants.models import RECEPTION_DEVICE_SCOPES, ApiApplication, Tenant


class GuestCheckInReportAPITests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Report Tenant", slug="report-tenant")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Report Property",
            slug="report-property",
            guest_checkin_opens_days_before=0,
        )
        self.app, self.raw_token = ApiApplication.create_with_token(
            tenant=self.tenant,
            name="Report tablet",
            scopes=RECEPTION_DEVICE_SCOPES,
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="GC-RPT-001",
            check_in=timezone.localdate(),
            check_out=timezone.localdate() + timedelta(days=2),
            adults_count=1,
            booker_name="Report Guest",
            amount=Decimal("120.00"),
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
        GuestCheckInOrchestrator.ensure_session_and_link(
            self.reservation,
            created_from=GuestCheckInSessionCreatedFrom.EMAIL,
        )
        self.client = APIClient()
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.raw_token}"}

    def test_guest_checkin_report_returns_kpis_and_active_sessions(self):
        response = self.client.get(
            "/api/v1/reception/reports/guest-checkin/",
            {"property_slug": self.property.slug, "days": 30},
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["property_slug"], self.property.slug)
        self.assertGreaterEqual(data["kpis"]["sessions_created"], 1)
        self.assertGreaterEqual(data["kpis"]["sessions_active"], 1)
        self.assertEqual(len(data["active_sessions"]), 1)
        self.assertEqual(data["active_sessions"][0]["reservation_id"], self.reservation.pk)

    def test_guest_checkin_report_defaults_to_sole_property(self):
        """Omit property_slug when tenant has one property → auto-resolve."""
        response = self.client.get(
            "/api/v1/reception/reports/guest-checkin/",
            {"days": 30},
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["property_slug"], self.property.slug)

    def test_guest_checkin_report_requires_property_when_ambiguous(self):
        Property.objects.create(
            tenant=self.tenant,
            name="Second Property",
            slug="report-property-2",
        )
        response = self.client.get(
            "/api/v1/reception/reports/guest-checkin/",
            {"days": 30},
            **self.auth,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "property_required")
