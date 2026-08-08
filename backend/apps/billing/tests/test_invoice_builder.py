from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from apps.billing.exceptions import InvoiceBuildError
from apps.billing.models import InvoiceLine, TenantFiscalSettings
from apps.billing.services.invoice_builder import build_invoice_from_reservation
from apps.billing.tests.helpers import make_guest
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant
from apps.tourist_tax.management.commands.seed_sibenik_tourist_tax import Command as SeedCommand
from apps.tourist_tax.models import TouristTaxAccommodationCategory, TouristTaxZone


class InvoiceBuilderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        SeedCommand().handle()
        cls.tenant = Tenant.objects.create(name="Billing Tenant", slug="billing")
        cls.zone = TouristTaxZone.objects.get(code="sibenik-central")
        cls.category = TouristTaxAccommodationCategory.objects.get(code="room")
        cls.property = Property.objects.create(
            tenant=cls.tenant,
            name="Test Property",
            slug="test-property",
            tourist_tax_zone=cls.zone,
            tourist_tax_category=cls.category,
        )
        cls.settings = TenantFiscalSettings.objects.create(
            tenant=cls.tenant,
            is_vat_registered=True,
            issuer_oib="12345678901",
            issuer_name="Test d.o.o.",
            business_premise_code="PP1",
            payment_device_code="1",
        )

    def _reservation(self, *, amount="150.00"):
        reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            check_in=date(2026, 4, 15),
            check_out=date(2026, 4, 18),
            status=Reservation.Status.CHECKED_IN,
            booker_name="John Doe",
            amount=Decimal(amount),
            adults_count=2,
            children_count=2,
            payment_provider="Booking.com",
        )
        make_guest(
            tenant=self.tenant,
            reservation=reservation,
            first_name="Ana",
            last_name="Anic",
            date_of_birth=date(1988, 1, 1),
            is_primary=True,
        )
        make_guest(
            tenant=self.tenant,
            reservation=reservation,
            first_name="Marko",
            last_name="Markic",
            date_of_birth=date(1985, 1, 1),
        )
        make_guest(
            tenant=self.tenant,
            reservation=reservation,
            first_name="Petra",
            last_name="Petra",
            date_of_birth=date(1990, 1, 1),
        )
        make_guest(
            tenant=self.tenant,
            reservation=reservation,
            first_name="Luka",
            last_name="Lukic",
            # Age ≤11 on check_in → free child tourist-tax line (0–11 bracket).
            date_of_birth=date(2018, 1, 1),
        )
        return reservation

    def test_build_invoice_lines_and_vat(self):
        reservation = self._reservation()
        built = build_invoice_from_reservation(reservation, self.settings)

        self.assertEqual(built.buyer_name, "Ana Anic")
        self.assertEqual(built.buyer_document_number, "")
        self.assertEqual(built.buyer_address, "")
        # Primary guest nationality from make_guest → HR display name.
        self.assertEqual(built.buyer_country, "Hrvatska")
        self.assertEqual(built.total, Decimal("150.00"))
        self.assertEqual(len(built.lines), 3)

        accommodation = built.lines[0]
        self.assertEqual(accommodation.line_kind, InvoiceLine.LineKind.ACCOMMODATION)
        self.assertEqual(accommodation.vat_rate, Decimal("13.00"))
        self.assertGreater(accommodation.vat_amount, Decimal("0.00"))

        tourist_adult = built.lines[1]
        self.assertEqual(tourist_adult.line_kind, InvoiceLine.LineKind.TOURIST_TAX_ADULT)
        self.assertEqual(tourist_adult.vat_rate, Decimal("0.00"))

        tourist_child = built.lines[2]
        self.assertEqual(tourist_child.line_kind, InvoiceLine.LineKind.TOURIST_TAX_CHILD)
        self.assertEqual(tourist_child.line_total, Decimal("0.00"))

    def test_missing_amount_raises(self):
        reservation = self._reservation(amount="150.00")
        reservation.amount = None
        with self.assertRaises(InvoiceBuildError):
            build_invoice_from_reservation(reservation, self.settings)
