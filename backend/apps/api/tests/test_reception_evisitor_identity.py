from datetime import date
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from apps.properties.models import Property, Unit
from apps.reservations.models import Guest, Reservation, ReservationUnit
from apps.tenants.models import RECEPTION_DEVICE_SCOPES, ApiApplication, Tenant


class ReceptionEvisitorIdentityApiTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Identity API", slug="identity-api")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="P",
            slug="p",
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
            booking_code="BK-ID",
            check_in=date(2026, 9, 12),
            check_out=date(2026, 9, 13),
            status=Reservation.Status.EXPECTED,
            booker_name="Ana Anić",
            amount=Decimal("80.00"),
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
            first_name="Ana",
            last_name="Anić",
            is_primary=True,
        )
        self.client = APIClient()
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.raw_token}"}
        self.guest_url = (
            f"/api/v1/reception/reservations/{self.reservation.id}/guests/{self.guest.id}/"
        )
        self.detail_url = f"/api/v1/reception/reservations/{self.reservation.id}/"

    def test_reservation_guests_include_invented_flag(self):
        response = self.client.get(self.detail_url, **self.auth)
        self.assertEqual(response.status_code, 200)
        row = response.json()["guests"][0]
        self.assertFalse(row["evisitor_identity_invented"])
        self.assertIsNone(row["evisitor_identity_invented_at"])

    def test_patch_sets_and_clears_invented_flag(self):
        marked = self.client.patch(
            self.guest_url,
            {"evisitor_identity_invented": True},
            format="json",
            **self.auth,
        )
        self.assertEqual(marked.status_code, 200)
        self.assertTrue(marked.json()["evisitor_identity_invented"])
        self.assertIsNotNone(marked.json()["evisitor_identity_invented_at"])

        self.guest.refresh_from_db()
        self.assertIsNotNone(self.guest.evisitor_identity_invented_at)
        self.assertIsNone(self.guest.evisitor_identity_invented_by_id)

        cleared = self.client.patch(
            self.guest_url,
            {"evisitor_identity_invented": False},
            format="json",
            **self.auth,
        )
        self.assertEqual(cleared.status_code, 200)
        self.assertFalse(cleared.json()["evisitor_identity_invented"])
        self.assertIsNone(cleared.json()["evisitor_identity_invented_at"])

        self.guest.refresh_from_db()
        self.assertIsNone(self.guest.evisitor_identity_invented_at)
        self.assertIsNone(self.guest.evisitor_identity_invented_by_id)

    def test_patch_keeps_existing_timestamp(self):
        first = self.client.patch(
            self.guest_url,
            {"evisitor_identity_invented": True},
            format="json",
            **self.auth,
        )
        stamped_at = first.json()["evisitor_identity_invented_at"]
        second = self.client.patch(
            self.guest_url,
            {"evisitor_identity_invented": True},
            format="json",
            **self.auth,
        )
        self.assertEqual(second.json()["evisitor_identity_invented_at"], stamped_at)
