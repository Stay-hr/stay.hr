from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch
import uuid
from zoneinfo import ZoneInfo

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from apps.billing.models import Invoice, InvoiceLine, TenantFiscalSettings
from apps.billing.tests.helpers import make_guest
from apps.properties.models import Property
from apps.reservations.checkout import perform_reservation_checkout
from apps.reservations.models import EvisitorGuestStatus, Reservation
from apps.tenants.models import Tenant
from apps.tourist_tax.management.commands.seed_sibenik_tourist_tax import Command as SeedCommand
from apps.tourist_tax.models import TouristTaxAccommodationCategory, TouristTaxZone


class CheckoutInvoiceHookTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        SeedCommand().handle()
        cls.tenant = Tenant.objects.create(name="Checkout Tenant", slug="checkout-bill")
        cls.zone = TouristTaxZone.objects.get(code="sibenik-central")
        cls.category = TouristTaxAccommodationCategory.objects.get(code="room")
        cls.property = Property.objects.create(
            tenant=cls.tenant,
            name="Checkout Property",
            slug="checkout-property",
            tourist_tax_zone=cls.zone,
            tourist_tax_category=cls.category,
        )

    def _ready_reservation(self):
        reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            check_in=date(2026, 4, 15),
            check_out=date(2026, 4, 18),
            status=Reservation.Status.CHECKED_IN,
            booker_name="Guest Guest",
            booker_email="guest@example.com",
            amount=Decimal("150.00"),
            adults_count=2,
            children_count=0,
            payment_provider="Booking.com",
        )
        make_guest(
            tenant=self.tenant,
            reservation=reservation,
            first_name="Guest",
            last_name="Guest",
            date_of_birth=date(1990, 1, 1),
            is_primary=True,
            evisitor_status=EvisitorGuestStatus.SENT,
        )
        make_guest(
            tenant=self.tenant,
            reservation=reservation,
            first_name="Second",
            last_name="Guest",
            date_of_birth=date(1992, 6, 15),
            evisitor_status=EvisitorGuestStatus.SENT,
        )
        return reservation

    @patch("apps.reservations.checkout.checkout_reservation_guests_in_evisitor")
    @patch("apps.billing.tasks.send_invoice_email_task.delay")
    @patch("apps.billing.tasks.fiscalize_invoice.delay")
    @patch("apps.billing.services.pdf.render_invoice_pdf")
    def test_checkout_creates_invoice_for_vat_tenant(
        self,
        mock_pdf,
        mock_fiscalize,
        mock_email,
        mock_evisitor_checkout,
    ):
        mock_evisitor_checkout.return_value = []
        settings = TenantFiscalSettings.objects.create(
            tenant=self.tenant,
            is_vat_registered=True,
            issuer_oib="12345678901",
            issuer_name="Issuer d.o.o.",
            issuer_address="Ulica 1",
            business_premise_code="PP1",
            payment_device_code="1",
            certificate_file=SimpleUploadedFile("test.p12", b"fake-cert"),
        )
        settings.set_certificate_password("secret")
        settings.save()

        reservation = self._ready_reservation()
        perform_reservation_checkout(reservation)

        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.CHECKED_OUT)
        self.assertTrue(hasattr(reservation, "invoice"))
        mock_fiscalize.assert_called_once()
        mock_email.assert_called_once()

    @patch("apps.reservations.checkout.checkout_reservation_guests_in_evisitor")
    def test_checkout_without_vat_does_not_create_invoice(self, mock_evisitor_checkout):
        mock_evisitor_checkout.return_value = []
        reservation = self._ready_reservation()
        perform_reservation_checkout(reservation)
        self.assertFalse(Invoice.objects.filter(reservation=reservation).exists())


class PublicInvoiceAccessTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Public Tenant", slug="public-bill")
        TenantFiscalSettings.objects.create(
            tenant=self.tenant,
            is_vat_registered=True,
            issuer_oib="12345678901",
            issuer_name="Issuer",
            issuer_address="Adresa 1",
            business_premise_code="PP1",
            payment_device_code="1",
        )
        reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=Property.objects.create(
                tenant=self.tenant,
                name="Property",
                slug="property",
            ),
            check_in=date(2026, 4, 15),
            check_out=date(2026, 4, 16),
            status=Reservation.Status.CHECKED_OUT,
            booker_name="Guest",
            amount=Decimal("100.00"),
        )
        self.invoice = Invoice.objects.create(
            tenant=self.tenant,
            reservation=reservation,
            invoice_number="1-PP1-1",
            sequence_number=1,
            issued_at=datetime(2026, 4, 16, 10, 0, tzinfo=ZoneInfo("Europe/Zagreb")),
            buyer_name="Guest Guest",
            subtotal=Decimal("88.50"),
            vat_amount=Decimal("11.50"),
            total=Decimal("100.00"),
            zki="abc",
        )
        InvoiceLine.objects.create(
            invoice=self.invoice,
            sort_order=1,
            line_kind=InvoiceLine.LineKind.ACCOMMODATION,
            description="Noćenje",
            quantity=Decimal("1"),
            unit_price=Decimal("88.50"),
            vat_rate=Decimal("13.00"),
            vat_amount=Decimal("11.50"),
            line_total=Decimal("100.00"),
        )

    def test_public_html_invoice(self):
        url = reverse(
            "public-invoice-html",
            kwargs={"public_access_token": self.invoice.public_access_token},
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("Guest Guest", content)
        self.assertIn("1-PP1-1", content)
        self.assertIn('href="pdf/"', content)
        self.assertIn("aria-label=\"Download invoice PDF\"", content)
        self.assertIn("aria-label=\"Print invoice\"", content)
        self.assertIn("window.print()", content)
        self.assertIn("no-print", content)
        self.assertIn('property="og:title"', content)
        self.assertIn("Invoice 1-PP1-1", content)
        self.assertNotRegex(content, r'https?://[^"\s]+/pdf/')
        self.assertNotIn(f"/api/v1/public/invoices/{self.invoice.public_access_token}/pdf/", content)

    def test_public_invoice_unknown_token_404(self):
        url = reverse(
            "public-invoice-html",
            kwargs={"public_access_token": uuid.uuid4()},
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)
        content = response.content.decode()
        self.assertIn("Invoice is not available.", content)
        self.assertIn("Please contact the property if you believe this is an error.", content)
