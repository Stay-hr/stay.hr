from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from apps.billing.models import BookingOffer, InvoiceLine, TenantFiscalSettings
from apps.billing.services.offer_builder import build_offer_snapshot
from apps.billing.services.offer_issue import issue_booking_offer
from apps.billing.tests.helpers import make_guest
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import RECEPTION_DEVICE_SCOPES, ApiApplication, Tenant
from apps.tourist_tax.management.commands.seed_sibenik_tourist_tax import Command as SeedCommand
from apps.tourist_tax.models import TouristTaxAccommodationCategory, TouristTaxZone


class BookingOfferBuilderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        SeedCommand().handle()
        cls.tenant = Tenant.objects.create(name="Offer Tenant", slug="offer-tenant")
        cls.zone = TouristTaxZone.objects.get(code="sibenik-central")
        cls.category = TouristTaxAccommodationCategory.objects.get(code="room")
        cls.property = Property.objects.create(
            tenant=cls.tenant,
            name="Uzorita",
            slug="uzorita-offer",
            tourist_tax_zone=cls.zone,
            tourist_tax_category=cls.category,
        )
        cls.settings = TenantFiscalSettings.objects.create(
            tenant=cls.tenant,
            is_vat_registered=True,
            issuer_oib="12345678901",
            issuer_name="Šupina Poljica d.o.o.",
            issuer_address="Poljica",
            issuer_iban="HR5824810001128008571",
            business_premise_code="PP1",
            payment_device_code="1",
            accommodation_vat_rate=Decimal("13.00"),
        )

    def test_akd_400_offer_snapshot_b2b_buyer_and_lines(self):
        reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="39E3CB64",
            check_in=date(2026, 8, 24),
            check_out=date(2026, 8, 28),
            status=Reservation.Status.EXPECTED,
            booker_name="Hrvoje Hrčka",
            amount=Decimal("400.00"),
            adults_count=1,
            buyer_company_name="AKD-ZAŠTITA D.O.O.",
            buyer_oib="09253797076",
            buyer_address="Savska cesta 28, 10000 Zagreb",
            invoice_email="racuni@akd-zastita.hr",
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
        snapshot = build_offer_snapshot(reservation, self.settings)
        self.assertEqual(snapshot["buyer"]["name"], "AKD-ZAŠTITA D.O.O.")
        self.assertEqual(snapshot["buyer"]["document_number"], "09253797076")
        self.assertEqual(snapshot["total"], "400.00")
        self.assertEqual(snapshot["payment_reference"], "39E3CB64")
        self.assertEqual(snapshot["seller"]["iban"], "HR5824810001128008571")
        self.assertEqual(len(snapshot["lines"]), 2)
        kinds = {line["line_kind"] for line in snapshot["lines"]}
        self.assertIn(InvoiceLine.LineKind.ACCOMMODATION, kinds)
        self.assertIn(InvoiceLine.LineKind.TOURIST_TAX_ADULT, kinds)
        tax_line = next(
            line for line in snapshot["lines"]
            if line["line_kind"] == InvoiceLine.LineKind.TOURIST_TAX_ADULT
        )
        self.assertEqual(tax_line["line_total"], "10.00")

    def test_issue_offer_idempotent_and_pdf(self):
        reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="OFFER-1",
            check_in=date(2026, 9, 1),
            check_out=date(2026, 9, 3),
            status=Reservation.Status.EXPECTED,
            booker_name="Test Guest",
            amount=Decimal("200.00"),
            adults_count=1,
            buyer_company_name="Firma d.o.o.",
            buyer_oib="11111111111",
        )
        make_guest(
            tenant=self.tenant,
            reservation=reservation,
            first_name="Test",
            last_name="Guest",
        )
        first = issue_booking_offer(reservation)
        second = issue_booking_offer(reservation)
        self.assertEqual(first.pk, second.pk)
        self.assertTrue(first.pdf_file.name.endswith(".pdf"))
        self.assertEqual(BookingOffer.objects.filter(reservation=reservation).count(), 1)

    def test_snapshot_immutable_after_settings_change(self):
        reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="IMMUTABLE-1",
            check_in=date(2026, 9, 10),
            check_out=date(2026, 9, 12),
            status=Reservation.Status.EXPECTED,
            booker_name="Guest",
            amount=Decimal("150.00"),
            adults_count=1,
            buyer_company_name="Buyer d.o.o.",
        )
        make_guest(
            tenant=self.tenant,
            reservation=reservation,
            first_name="Immutable",
            last_name="Guest",
        )
        offer = issue_booking_offer(reservation)
        original_iban = offer.snapshot["seller"]["iban"]
        self.settings.issuer_iban = "HR0000000000000000000"
        self.settings.save(update_fields=["issuer_iban", "updated_at"])
        offer.refresh_from_db()
        self.assertEqual(offer.snapshot["seller"]["iban"], original_iban)


class BookingOfferAPITests(TestCase):
    def setUp(self):
        SeedCommand().handle()
        self.client = APIClient()
        self.tenant = Tenant.objects.create(name="Offer API", slug="offer-api")
        self.zone = TouristTaxZone.objects.get(code="sibenik-central")
        self.category = TouristTaxAccommodationCategory.objects.get(code="room")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="API Property",
            slug="api-offer",
            tourist_tax_zone=self.zone,
            tourist_tax_category=self.category,
        )
        TenantFiscalSettings.objects.create(
            tenant=self.tenant,
            is_vat_registered=True,
            issuer_oib="12345678901",
            issuer_name="Issuer d.o.o.",
            issuer_iban="HR5824810001128008571",
            business_premise_code="PP1",
            payment_device_code="1",
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="API-OFFER-1",
            check_in=date(2026, 8, 24),
            check_out=date(2026, 8, 28),
            status=Reservation.Status.EXPECTED,
            booker_name="API Guest",
            amount=Decimal("400.00"),
            adults_count=1,
            buyer_company_name="API Buyer d.o.o.",
            buyer_oib="09253797076",
            invoice_email="buyer@example.com",
        )
        make_guest(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="API",
            last_name="Guest",
        )
        self.app, self.raw_token = ApiApplication.create_with_token(
            tenant=self.tenant,
            name="Offer tablet",
            scopes=RECEPTION_DEVICE_SCOPES,
        )
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.raw_token}"}

    def test_create_and_get_offer(self):
        url = reverse("reception-reservation-offer", kwargs={"pk": self.reservation.pk})
        create = self.client.post(url, **self.auth)
        self.assertEqual(create.status_code, 201)
        data = create.json()
        self.assertTrue(data["offer_number"].startswith("PON-"))
        self.assertEqual(data["total"], "400.00")

        get = self.client.get(url, **self.auth)
        self.assertEqual(get.status_code, 200)
        self.assertEqual(get.json()["id"], data["id"])

    def test_public_offer_pdf(self):
        offer = issue_booking_offer(self.reservation)
        url = reverse(
            "public-offer-pdf",
            kwargs={"public_access_token": offer.public_access_token},
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")

    @patch("apps.api.offer_views.send_booking_offer_email", return_value={"status": "sent", "recipient": "buyer@example.com"})
    def test_send_offer_email(self, mock_send):
        issue_booking_offer(self.reservation)
        url = reverse(
            "reception-reservation-offer-send-email",
            kwargs={"pk": self.reservation.pk},
        )
        response = self.client.post(url, data={}, content_type="application/json", **self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "sent")
        mock_send.assert_called_once()
