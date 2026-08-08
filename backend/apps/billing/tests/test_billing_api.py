from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from apps.billing.models import Invoice, TenantFiscalSettings
from apps.billing.tests.helpers import make_guest
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import RECEPTION_DEVICE_SCOPES, ApiApplication, Tenant


class BillingApiTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Billing API", slug="billing-api")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Property",
            slug="property",
        )
        self.app, self.raw_token = ApiApplication.create_with_token(
            tenant=self.tenant,
            name="Tablet",
            scopes=RECEPTION_DEVICE_SCOPES,
        )
        self.fiscal_settings = TenantFiscalSettings.objects.create(
            tenant=self.tenant,
            is_vat_registered=True,
            issuer_oib="12345678901",
            issuer_name="Issuer",
            business_premise_code="PP1",
            payment_device_code="1",
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            check_in=date(2026, 4, 15),
            check_out=date(2026, 4, 16),
            status=Reservation.Status.CHECKED_OUT,
            booker_name="Guest Guest",
            amount=Decimal("100.00"),
        )
        self.primary_guest = make_guest(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="Guest",
            last_name="Guest",
            is_primary=True,
        )
        self.invoice = Invoice.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            invoice_number="1-PP1-1",
            sequence_number=1,
            issued_at=datetime(2026, 4, 16, 10, 0, tzinfo=ZoneInfo("Europe/Zagreb")),
            buyer_name="Guest Guest",
            subtotal=Decimal("88.50"),
            vat_amount=Decimal("11.50"),
            total=Decimal("100.00"),
            zki="abc123",
        )
        self.invoice.pdf_file.save(
            "test.pdf",
            SimpleUploadedFile("test.pdf", b"%PDF-1.4 test"),
            save=True,
        )
        self.client = APIClient()
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.raw_token}"}

    def test_app_config_guest_invoices_flag(self):
        response = self.client.get("/api/v1/app/config", **self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["feature_flags"]["guest_invoices"])

        self.fiscal_settings.is_vat_registered = False
        self.fiscal_settings.save(update_fields=["is_vat_registered"])
        response = self.client.get("/api/v1/app/config", **self.auth)
        self.assertFalse(response.json()["feature_flags"]["guest_invoices"])

    def test_get_invoice_json(self):
        response = self.client.get(
            f"/api/v1/reception/reservations/{self.reservation.pk}/invoice/",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["invoice_number"], "1-PP1-1")

    def test_get_invoice_pdf_by_reservation(self):
        response = self.client.get(
            f"/api/v1/reception/reservations/{self.reservation.pk}/invoice/pdf/",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")

    @patch("apps.api.billing_views.send_invoice_email")
    def test_send_email_with_booker(self, mock_send):
        mock_send.return_value = {
            "status": "sent",
            "recipient": "booker@example.com",
        }
        self.reservation.booker_email = "booker@example.com"
        self.reservation.save(update_fields=["booker_email"])

        response = self.client.post(
            f"/api/v1/reception/reservations/{self.reservation.pk}/invoice/send-email/",
            {},
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "sent")
        mock_send.assert_called_once_with(self.invoice.pk)

    @patch("apps.api.billing_views.send_invoice_email")
    def test_send_email_saves_primary_guest_email(self, mock_send):
        mock_send.return_value = {
            "status": "sent",
            "recipient": "new@example.com",
        }

        response = self.client.post(
            f"/api/v1/reception/reservations/{self.reservation.pk}/invoice/send-email/",
            {"email": "new@example.com"},
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["guest_email_saved"])
        self.primary_guest.refresh_from_db()
        self.assertEqual(self.primary_guest.email, "new@example.com")

    def test_send_email_no_primary_guest(self):
        self.primary_guest.is_primary = False
        self.primary_guest.save(update_fields=["is_primary"])

        response = self.client.post(
            f"/api/v1/reception/reservations/{self.reservation.pk}/invoice/send-email/",
            {"email": "new@example.com"},
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["reason"], "no_primary_guest")

    @patch("apps.api.billing_views.send_invoice_email")
    def test_send_email_no_recipient(self, mock_send):
        mock_send.return_value = {
            "status": "skipped",
            "reason": "no_recipient",
        }

        response = self.client.post(
            f"/api/v1/reception/reservations/{self.reservation.pk}/invoice/send-email/",
            {},
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["reason"], "no_recipient")

    @patch("apps.billing.tasks.fiscalize_invoice")
    @patch("apps.billing.services.issue.issue_guest_invoice")
    def test_create_invoice_for_checked_out_reservation(self, mock_issue, mock_fiscalize):
        self.invoice.delete()

        # Create invoice inside the mock call — pre-creating would make POST return 200.
        def _issue(_reservation):
            return Invoice.objects.create(
                tenant=self.tenant,
                reservation=self.reservation,
                invoice_number="2-PP1-1",
                sequence_number=2,
                issued_at=datetime(2026, 4, 16, 11, 0, tzinfo=ZoneInfo("Europe/Zagreb")),
                buyer_name="Guest Guest",
                subtotal=Decimal("88.50"),
                vat_amount=Decimal("11.50"),
                total=Decimal("100.00"),
                zki="def456",
            )

        mock_issue.side_effect = _issue

        response = self.client.post(
            f"/api/v1/reception/reservations/{self.reservation.pk}/invoice/",
            {},
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["invoice_number"], "2-PP1-1")
        mock_issue.assert_called_once()
        mock_fiscalize.delay.assert_called_once()

    def test_create_invoice_requires_checked_out(self):
        self.invoice.delete()
        self.reservation.status = Reservation.Status.CHECKED_IN
        self.reservation.save(update_fields=["status"])

        response = self.client.post(
            f"/api/v1/reception/reservations/{self.reservation.pk}/invoice/",
            {},
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["reason"], "not_checked_out")
