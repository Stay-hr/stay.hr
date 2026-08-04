from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import TestCase

from apps.billing.models import Invoice, InvoiceLine, TenantFiscalSettings
from apps.billing.services.invoice_builder import (
    build_invoice_from_reservation,
    resolve_buyer_identity,
)
from apps.billing.services.pdf import (
    invoice_template_context,
    render_invoice_html,
    render_invoice_pdf,
    split_rendered_invoice_html,
)
from apps.properties.models import Property
from apps.reservations.models import Guest, Reservation
from apps.tenants.models import Tenant
from apps.tourist_tax.management.commands.seed_sibenik_tourist_tax import Command as SeedCommand
from apps.tourist_tax.models import TouristTaxAccommodationCategory, TouristTaxZone


class InvoicePdfTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        SeedCommand().handle()
        cls.tenant = Tenant.objects.create(name="PDF Tenant", slug="pdf-tenant")
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
            issuer_name="ŠUPINA POLJICA j.d.o.o.",
            issuer_address="Šibenska 1\n22000 Šibenik",
            business_premise_code="PP1",
            payment_device_code="1",
        )
        cls.reservation = Reservation.objects.create(
            tenant=cls.tenant,
            property=cls.property,
            check_in=date(2026, 5, 25),
            check_out=date(2026, 5, 26),
            status=Reservation.Status.CHECKED_OUT,
            booker_name="Kris Meeus",
            booker_address="Bruxellesstraat 1, 2800 Mechelen",
            external_id="5898434847",
            amount=Decimal("201.65"),
            adults_count=4,
            payment_provider="Booking.com",
        )
        Guest.objects.create(
            tenant=cls.tenant,
            reservation=cls.reservation,
            first_name="Kris",
            last_name="Meeus",
            is_primary=True,
            nationality="BE",
            address="Bruxellesstraat 1, 2800 Mechelen",
        )
        cls.invoice = Invoice.objects.create(
            tenant=cls.tenant,
            reservation=cls.reservation,
            invoice_number="1-ROOMS-1",
            sequence_number=1,
            issued_at=datetime(2026, 5, 27, 7, 21, tzinfo=ZoneInfo("Europe/Zagreb")),
            buyer_name="Kris Meeus",
            buyer_document_number="BE1234567",
            buyer_address="Bruxellesstraat 1, 2800 Mechelen",
            buyer_country="Belgija",
            payment_method=Invoice.PaymentMethod.BOOKING,
            payment_note=(
                "Plaćeno u cijelosti online putem posrednika Booking.com. "
                "Način plaćanja: TRANSAKCIJSKI RAČUN."
            ),
            subtotal=Decimal("178.45"),
            vat_amount=Decimal("22.05"),
            total=Decimal("201.65"),
            zki="abc123",
        )
        InvoiceLine.objects.create(
            invoice=cls.invoice,
            sort_order=1,
            line_kind=InvoiceLine.LineKind.ACCOMMODATION,
            description="Noćenje (4 odraslih + 0 djece)",
            quantity=Decimal("1"),
            unit_price=Decimal("169.60"),
            vat_rate=Decimal("13.00"),
            vat_amount=Decimal("22.05"),
            line_total=Decimal("191.65"),
        )
        InvoiceLine.objects.create(
            invoice=cls.invoice,
            sort_order=2,
            line_kind=InvoiceLine.LineKind.TOURIST_TAX_ADULT,
            description="Turistička pristojba - Odrasli",
            quantity=Decimal("4"),
            unit_price=Decimal("2.50"),
            vat_rate=Decimal("0.00"),
            vat_amount=Decimal("0.00"),
            line_total=Decimal("10.00"),
        )

    def test_invoice_html_preserves_croatian_characters(self):
        html = render_invoice_html(self.invoice, self.settings)
        self.assertIn("Račun", html)
        self.assertIn("Noćenje", html)
        self.assertIn("Turistička pristojba", html)
        self.assertIn("Plaćeno", html)
        self.assertIn("Način", html)
        self.assertIn("čl. 33.", html)
        self.assertIn("Broj dokumenta: BE1234567", html)
        self.assertIn("Broj rezervacije: 5898434847", html)
        self.assertIn("Država: Belgija", html)
        self.assertIn("Bruxellesstraat 1", html)
        self.assertIn('class="invoice-document"', html)

    def test_split_rendered_invoice_html_preserves_document(self):
        html = render_invoice_html(self.invoice, self.settings)
        styles, body = split_rendered_invoice_html(html)
        self.assertIn("DejaVuSans", styles)
        self.assertIn("invoice-document", body)
        self.assertIn("Noćenje", body)
        self.assertNotIn("<html", body.lower())
        self.assertNotIn("<body", body.lower())

    def test_invoice_template_context_utf8(self):
        context = invoice_template_context(self.invoice, self.settings)
        self.assertIn("Turistička", context["tourist_tax_clause"])
        self.assertIn("čl.", context["tourist_tax_clause"])

    @patch("apps.billing.services.pdf.pisa.CreatePDF")
    def test_render_invoice_pdf_registers_fonts(self, mock_create_pdf):
        mock_create_pdf.return_value = type("Result", (), {"err": 0})()

        render_invoice_pdf(self.invoice, self.settings)

        mock_create_pdf.assert_called_once()
        args, kwargs = mock_create_pdf.call_args
        self.assertIsNotNone(kwargs.get("link_callback"))
        html = args[0]
        self.assertIn("Račun", html)
        self.assertIn("DejaVuSans.ttf", html)

    def test_resolve_buyer_identity_fallbacks(self):
        reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            check_in=date(2026, 6, 1),
            check_out=date(2026, 6, 2),
            status=Reservation.Status.CHECKED_OUT,
            booker_name="Test Guest",
            booker_address="Booker adresa 1",
            amount=Decimal("100.00"),
            adults_count=1,
        )
        Guest.objects.create(
            tenant=self.tenant,
            reservation=reservation,
            first_name="Test",
            last_name="Guest",
            is_primary=True,
            personal_id_number="12345678901",
        )
        document_number, address = resolve_buyer_identity(reservation)
        self.assertEqual(document_number, "12345678901")
        self.assertEqual(address, "Booker adresa 1")

    def test_build_invoice_includes_buyer_identity(self):
        built = build_invoice_from_reservation(self.reservation, self.settings)
        self.assertEqual(built.buyer_document_number, "BE1234567")
        self.assertIn("Mechelen", built.buyer_address)
        self.assertEqual(built.buyer_country, "Belgija")
