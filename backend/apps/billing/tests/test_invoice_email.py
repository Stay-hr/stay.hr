from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from apps.billing.models import Invoice
from apps.communications.invoice_email import send_invoice_email
from apps.properties.models import Property
from apps.reservations.models import Guest, Reservation
from apps.tenants.models import Tenant, TenantReceptionSettings


class InvoiceEmailTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Mail Tenant", slug="mail-bill")
        TenantReceptionSettings.objects.create(
            tenant=self.tenant,
            guest_contact_email="reception@example.com",
        )
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Mail Property",
            slug="mail-property",
        )

    def _invoice(self, *, email: str | None = "guest@example.com"):
        reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            check_in=date(2026, 4, 15),
            check_out=date(2026, 4, 16),
            status=Reservation.Status.CHECKED_OUT,
            booker_name="Guest Guest",
            booker_email=email or "",
            booking_code="BK123",
            amount=Decimal("100.00"),
        )
        if email is None:
            Guest.objects.create(
                tenant=self.tenant,
                reservation=reservation,
                first_name="Guest",
                last_name="Guest",
                is_primary=True,
            )
        return Invoice.objects.create(
            tenant=self.tenant,
            reservation=reservation,
            invoice_number="1-PP1-1",
            sequence_number=1,
            issued_at=datetime(2026, 4, 16, 10, 0, 0),
            buyer_name="Guest Guest",
            subtotal=Decimal("88.50"),
            vat_amount=Decimal("11.50"),
            total=Decimal("100.00"),
        )

    def test_send_invoice_email_skips_without_recipient(self):
        invoice = self._invoice(email=None)
        result = send_invoice_email(invoice.pk)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "no_recipient")

    @patch("apps.communications.invoice_email.EmailMultiAlternatives")
    @patch("apps.communications.invoice_email._smtp_connection_for_reservation")
    def test_send_invoice_email_success(self, mock_connection, mock_email_cls):
        mock_connection.return_value = object()
        message = mock_email_cls.return_value
        invoice = self._invoice()
        result = send_invoice_email(invoice.pk)
        self.assertEqual(result["status"], "sent")
        message.send.assert_called_once()
        html_body = message.attach_alternative.call_args[0][0]
        text_body = mock_email_cls.call_args.kwargs["body"]
        portal_path = f"/api/v1/public/invoices/{invoice.public_access_token}/"
        pdf_path = f"/api/v1/public/invoices/{invoice.public_access_token}/pdf/"
        self.assertIn(portal_path, html_body)
        self.assertNotIn(pdf_path, html_body)
        self.assertNotIn(pdf_path, text_body)
        invoice.refresh_from_db()
        self.assertEqual(invoice.email_recipient, "guest@example.com")
