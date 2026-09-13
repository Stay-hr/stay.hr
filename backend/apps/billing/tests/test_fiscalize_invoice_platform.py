import inspect
from datetime import datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from apps.billing import tasks as billing_tasks
from apps.billing.exceptions import FiscalizationError
from apps.billing.models import FiscalizationAttempt, Invoice, InvoiceLine, TenantFiscalSettings
from apps.billing.services.fisk1 import FiscalResult
from apps.billing.tasks import fiscalize_invoice
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant


class FiscalizeInvoiceStayNativeTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Platform Tenant", slug="uzorita")
        self.settings = TenantFiscalSettings.objects.create(
            tenant=self.tenant,
            is_vat_registered=True,
            issuer_oib="12345678901",
            issuer_name="Test Issuer",
            business_premise_code="PP1",
            payment_device_code="1",
        )
        self.settings.set_certificate_password("secret")
        self.settings.save()

        reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=Property.objects.create(
                tenant=self.tenant,
                name="P",
                slug="p",
            ),
            check_in=datetime(2026, 4, 15).date(),
            check_out=datetime(2026, 4, 16).date(),
            status=Reservation.Status.CHECKED_OUT,
            booker_name="Guest",
            amount=Decimal("100.00"),
        )
        self.invoice = Invoice.objects.create(
            tenant=self.tenant,
            reservation=reservation,
            invoice_number="1-PP1-1",
            sequence_number=1,
            issued_at=datetime(2026, 4, 16, 10, 0, 0),
            buyer_name="Guest",
            subtotal=Decimal("88.50"),
            vat_amount=Decimal("11.50"),
            total=Decimal("100.00"),
            zki="abc123",
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

        cert_file = MagicMock()
        cert_file.read.return_value = b"fake-p12"
        self.settings.certificate_file = cert_file

    def test_task_source_does_not_call_fiskal_platform(self):
        source = inspect.getsource(billing_tasks.fiscalize_invoice)
        self.assertNotIn("fiscalize_via_platform", source)
        self.assertNotIn("FISKAL_EXECUTION_ENABLED", source)
        self.assertIn("Fisk1Connector", source)

    @override_settings(FISKAL_EXECUTION_ENABLED=True)
    @patch("apps.billing.services.fisk1.connector.render_invoice_pdf")
    @patch("apps.billing.services.fiskal_platform.submit.fiscalize_via_platform")
    @patch("apps.billing.services.fisk1.connector.Fisk1Connector.fiscalize")
    def test_fiscalize_invoice_uses_fisk1_when_platform_flag_enabled(
        self, mock_fisk1, mock_platform, _pdf
    ):
        mock_fisk1.return_value = FiscalResult(jir="STAY-JIR-1")

        result = fiscalize_invoice.run(self.invoice.pk)

        self.assertEqual(result["status"], "fiscalized")
        self.assertEqual(result["jir"], "STAY-JIR-1")
        mock_fisk1.assert_called_once()
        mock_platform.assert_not_called()

        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.jir, "STAY-JIR-1")
        self.assertEqual(self.invoice.fiscal_status, Invoice.FiscalStatus.FISCALIZED)

        attempt = FiscalizationAttempt.objects.get(invoice=self.invoice)
        self.assertTrue(attempt.success)

    @override_settings(FISKAL_EXECUTION_ENABLED=False)
    @patch("apps.billing.services.fisk1.connector.render_invoice_pdf")
    @patch("apps.billing.services.fisk1.connector.Fisk1Connector.fiscalize")
    def test_fiscalize_invoice_uses_fisk1_when_disabled(self, mock_fiscalize, _pdf):
        mock_fiscalize.return_value = FiscalResult(jir="LEGACY-JIR")

        result = fiscalize_invoice.run(self.invoice.pk)

        self.assertEqual(result["status"], "fiscalized")
        self.assertEqual(result["jir"], "LEGACY-JIR")
        mock_fiscalize.assert_called_once()

    @patch("apps.billing.services.fisk1.connector.render_invoice_pdf")
    @patch("apps.billing.services.fisk1.connector.Fisk1Connector.fiscalize")
    def test_failure_stores_cis_response_snapshot(self, mock_fiscalize, _pdf):
        mock_fiscalize.side_effect = FiscalizationError(
            "CIS HTTP 500: s006 Sistemska pogreška prilikom obrade zahtjeva.",
            request_snapshot="<req/>",
            response_snapshot="<tns:Greske><tns:Greska>s006</tns:Greska></tns:Greske>",
        )

        with self.assertRaises(FiscalizationError):
            fiscalize_invoice.run(self.invoice.pk)

        attempt = FiscalizationAttempt.objects.get(invoice=self.invoice)
        self.assertFalse(attempt.success)
        self.assertIn("s006", attempt.error_message)
        self.assertIn("s006", attempt.response_snapshot)
        self.assertEqual(attempt.request_snapshot, "<req/>")
