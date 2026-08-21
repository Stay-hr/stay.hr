"""PR1 gate: canonical create_reception_reservation + B2B invoice buyer + amount split."""

from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.billing.models import InvoiceLine, TenantFiscalSettings
from apps.billing.services.invoice_builder import build_invoice_from_reservation
from apps.billing.tests.helpers import make_guest
from apps.properties.models import Property, Unit
from apps.reservations.create_reception_reservation import (
    CreateReceptionReservationInput,
    ReceptionGuestInput,
    b2b_billing_snapshot_locked,
    create_reception_reservation,
)
from apps.reservations.models import Reservation
from apps.tenants.models import ChannelManager, Tenant, TenantMembership, TenantReceptionSettings
from apps.tourist_tax.management.commands.seed_sibenik_tourist_tax import Command as SeedCommand
from apps.tourist_tax.models import TouristTaxAccommodationCategory, TouristTaxZone

User = get_user_model()


class CreateReceptionReservationServiceTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="pr1-demo", name="PR1 Demo")
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

    @patch("apps.integrations.channel_manager.tasks.sync_reservation_outbound_task.delay")
    def test_service_sets_amount_b2b_guest_and_booking_code(self, _delay):
        reservation = create_reception_reservation(
            CreateReceptionReservationInput(
                tenant=self.tenant,
                property=self.property,
                unit=self.unit,
                check_in=date(2026, 8, 24),
                check_out=date(2026, 8, 28),
                booker_name="AKD-ZAŠTITA D.O.O. / Hrvoje Hrčka",
                booker_phone="+385991636951",
                booker_email="luka.golik@akdz.hr",
                booker_address="Savska cesta 28, 10000 Zagreb",
                amount=Decimal("400.00"),
                buyer_company_name="AKD-ZAŠTITA D.O.O.",
                buyer_oib="09253797076",
                buyer_address="Savska cesta 28, 10000 Zagreb",
                invoice_email="luka.golik@akdz.hr",
                guest=ReceptionGuestInput(first_name="Hrvoje", last_name="Hrčka"),
            )
        )
        reservation.refresh_from_db()
        self.assertEqual(reservation.amount, Decimal("400.00"))
        self.assertEqual(reservation.currency, "EUR")
        self.assertEqual(reservation.import_source, "manual")
        self.assertEqual(reservation.source, "reception")
        self.assertEqual(reservation.status, Reservation.Status.EXPECTED)
        self.assertTrue(reservation.booking_code)
        self.assertEqual(reservation.buyer_company_name, "AKD-ZAŠTITA D.O.O.")
        self.assertEqual(reservation.buyer_oib, "09253797076")
        self.assertEqual(reservation.nights_count, 4)
        self.assertEqual(reservation.adults_count, 1)
        unit_row = reservation.units.get()
        self.assertEqual(unit_row.unit_id, self.unit.pk)
        self.assertEqual(unit_row.amount, Decimal("400.00"))
        guest = reservation.guests.get()
        self.assertEqual(guest.first_name, "Hrvoje")
        self.assertTrue(guest.is_primary)
        self.assertFalse(b2b_billing_snapshot_locked(reservation))

    @patch("apps.integrations.channel_manager.tasks.sync_reservation_outbound_task.delay")
    def test_api_create_delegates_to_canonical_service(self, _delay):
        from apps.reservations.create_reception_reservation import (
            create_reception_reservation as real_create,
        )

        with patch(
            "apps.reservations.create_reception_reservation.create_reception_reservation",
            wraps=real_create,
        ) as wrapped:
            staff = User.objects.create_user(
                username="pr1_staff2", password="secret-pass", is_staff=True
            )
            TenantMembership.objects.create(user=staff, tenant=self.tenant)
            client = APIClient()
            client.post(
                "/api/v1/auth/reception-login/",
                {
                    "username": "pr1_staff2",
                    "password": "secret-pass",
                    "tenant_id": self.tenant.pk,
                },
                format="json",
                HTTP_HOST="app.stay.hr",
            )
            response = client.post(
                "/api/v1/reception/reservations/create/",
                {
                    "property_slug": "uzorita",
                    "unit_id": self.unit.id,
                    "check_in": "2026-09-01",
                    "check_out": "2026-09-03",
                    "booker_name": "Test Guest",
                    "amount": "100.00",
                },
                format="json",
                HTTP_HOST="app.stay.hr",
            )
            self.assertEqual(response.status_code, 201, response.content)
            wrapped.assert_called_once()
            self.assertEqual(
                Reservation.objects.get(pk=response.json()["id"]).amount,
                Decimal("100.00"),
            )

    @patch("apps.integrations.channel_manager.tasks.sync_reservation_outbound_task.delay")
    def test_api_create_uses_same_service_semantics(self, _delay):
        staff = User.objects.create_user(username="pr1_staff", password="secret-pass", is_staff=True)
        TenantMembership.objects.create(user=staff, tenant=self.tenant)
        client = APIClient()
        client.post(
            "/api/v1/auth/reception-login/",
            {
                "username": "pr1_staff",
                "password": "secret-pass",
                "tenant_id": self.tenant.pk,
            },
            format="json",
            HTTP_HOST="app.stay.hr",
        )
        response = client.post(
            "/api/v1/reception/reservations/create/",
            {
                "property_slug": "uzorita",
                "unit_id": self.unit.id,
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
                "guest": {"first_name": "Hrvoje", "last_name": "Hrčka"},
            },
            format="json",
            HTTP_HOST="app.stay.hr",
        )
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        self.assertEqual(data["total_amount"], "400.00")
        self.assertEqual(data["buyer_company_name"], "AKD-ZAŠTITA D.O.O.")
        self.assertEqual(data["buyer_oib"], "09253797076")
        self.assertEqual(data["invoice_email"], "luka.golik@akdz.hr")
        reservation = Reservation.objects.get(pk=data["id"])
        self.assertEqual(reservation.amount, Decimal("400.00"))
        self.assertEqual(reservation.import_source, "manual")
        self.assertTrue(reservation.booking_code)
        self.assertEqual(reservation.guests.count(), 1)


class B2BInvoiceBuyerAndAmountSplitTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        SeedCommand().handle()
        cls.tenant = Tenant.objects.create(name="PR1 Billing", slug="pr1-billing")
        cls.zone = TouristTaxZone.objects.get(code="sibenik-central")
        cls.category = TouristTaxAccommodationCategory.objects.get(code="room")
        cls.property = Property.objects.create(
            tenant=cls.tenant,
            name="Uzorita",
            slug="uzorita-bill",
            tourist_tax_zone=cls.zone,
            tourist_tax_category=cls.category,
        )
        cls.settings = TenantFiscalSettings.objects.create(
            tenant=cls.tenant,
            is_vat_registered=True,
            issuer_oib="12345678901",
            issuer_name="Šupina Poljica d.o.o.",
            business_premise_code="PP1",
            payment_device_code="1",
            accommodation_vat_rate=Decimal("13.00"),
        )

    def test_company_buyer_preferred_over_primary_guest(self):
        reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            check_in=date(2026, 8, 24),
            check_out=date(2026, 8, 28),
            status=Reservation.Status.CHECKED_IN,
            booker_name="Should Not Win",
            amount=Decimal("400.00"),
            adults_count=1,
            buyer_company_name="AKD-ZAŠTITA D.O.O.",
            buyer_oib="09253797076",
            buyer_address="Savska cesta 28, 10000 Zagreb",
            booker_country="HR",
        )
        make_guest(
            tenant=self.tenant,
            reservation=reservation,
            first_name="Hrvoje",
            last_name="Hrčka",
            date_of_birth=date(1985, 5, 1),
            is_primary=True,
        )
        built = build_invoice_from_reservation(reservation, self.settings)
        self.assertEqual(built.buyer_name, "AKD-ZAŠTITA D.O.O.")
        self.assertEqual(built.buyer_document_number, "09253797076")
        self.assertEqual(built.buyer_address, "Savska cesta 28, 10000 Zagreb")

    def test_amount_400_stays_400_after_tourist_tax_split(self):
        reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            check_in=date(2026, 8, 24),
            check_out=date(2026, 8, 28),
            status=Reservation.Status.CHECKED_IN,
            booker_name="AKD-ZAŠTITA D.O.O.",
            amount=Decimal("400.00"),
            adults_count=1,
            buyer_company_name="AKD-ZAŠTITA D.O.O.",
            buyer_oib="09253797076",
            buyer_address="Savska cesta 28, 10000 Zagreb",
        )
        make_guest(
            tenant=self.tenant,
            reservation=reservation,
            first_name="Hrvoje",
            last_name="Hrčka",
            date_of_birth=date(1985, 5, 1),
            is_primary=True,
        )
        built = build_invoice_from_reservation(reservation, self.settings)
        self.assertEqual(built.total, Decimal("400.00"))

        accommodation = next(
            line for line in built.lines if line.line_kind == InvoiceLine.LineKind.ACCOMMODATION
        )
        tourist_tax = sum(
            (
                line.line_total
                for line in built.lines
                if line.line_kind
                in {
                    InvoiceLine.LineKind.TOURIST_TAX_ADULT,
                    InvoiceLine.LineKind.TOURIST_TAX_CHILD,
                }
            ),
            Decimal("0.00"),
        )
        self.assertEqual(tourist_tax, Decimal("10.00"))
        self.assertEqual(accommodation.line_total, Decimal("390.00"))
        self.assertEqual(accommodation.line_total + tourist_tax, Decimal("400.00"))
        self.assertEqual(accommodation.vat_amount, Decimal("44.87"))
        self.assertEqual(accommodation.unit_price, Decimal("345.13"))
