from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from django.test import TestCase

from apps.billing.models import Invoice, InvoiceLine, TenantFiscalSettings
from apps.billing.services.fiskal_platform.payload import (
    build_guest_invoice_f1_payload,
    issued_at_for_f1,
)
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant


class GuestInvoiceF1PayloadTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Payload Tenant", slug="uzorita")
        self.settings = TenantFiscalSettings.objects.create(
            tenant=self.tenant,
            is_vat_registered=True,
            issuer_oib="12345678901",
            issuer_name="Test Issuer",
            business_premise_code="PP1",
            payment_device_code="1",
            operator_code="98765432109-1",
        )
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
            booker_name="Guest Guest",
            amount=Decimal("100.00"),
        )
        self.invoice = Invoice.objects.create(
            tenant=self.tenant,
            reservation=reservation,
            invoice_number="1-PP1-1",
            sequence_number=1,
            issued_at=datetime(2026, 4, 16, 10, 30, 0),
            buyer_name="Guest Guest",
            payment_method=Invoice.PaymentMethod.BOOKING,
            subtotal=Decimal("88.50"),
            vat_amount=Decimal("11.50"),
            total=Decimal("100.00"),
            zki="abc123zki",
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

    def test_build_guest_invoice_f1_payload(self):
        payload = build_guest_invoice_f1_payload(self.invoice, self.settings)

        self.assertEqual(payload["sequence_number"], 1)
        self.assertEqual(payload["issuer_oib"], "12345678901")
        self.assertEqual(payload["operator_oib"], "98765432109")
        self.assertEqual(payload["zki"], "abc123zki")
        self.assertEqual(payload["business_premise_code"], "PP1")
        self.assertEqual(payload["payment_device_code"], "1")
        self.assertEqual(payload["payment_code"], "T")
        self.assertTrue(payload["in_vat_system"])
        self.assertEqual(payload["vat_rate"], "13.00")
        self.assertEqual(payload["vat_base"], "88.50")
        self.assertEqual(payload["vat_amount"], "11.50")
        self.assertEqual(payload["total"], "100.00")
        self.assertEqual(payload["reservation_id"], self.invoice.reservation_id)
        self.assertEqual(payload["guest_name"], "Guest Guest")

        issued_at = issued_at_for_f1(self.invoice)
        self.assertEqual(payload["issued_at"], issued_at.strftime("%d.%m.%YT%H:%M:%S"))

    def test_issued_at_uses_tenant_timezone_not_utc(self):
        self.tenant.timezone = "Europe/Zagreb"
        self.tenant.save(update_fields=["timezone"])
        self.invoice.reservation.property.timezone = "Europe/Zagreb"
        self.invoice.reservation.property.save(update_fields=["timezone"])
        self.invoice.issued_at = datetime(2026, 9, 1, 9, 3, 49, tzinfo=dt_timezone.utc)
        self.invoice.save(update_fields=["issued_at"])
        self.invoice.refresh_from_db()
        self.invoice.tenant.refresh_from_db()

        payload = build_guest_invoice_f1_payload(self.invoice, self.settings)

        self.assertEqual(payload["issued_at"], "01.09.2026T11:03:49")
        self.assertEqual(
            issued_at_for_f1(self.invoice).tzinfo,
            ZoneInfo("Europe/Zagreb"),
        )

    def test_operator_oib_falls_back_to_issuer(self):
        self.settings.operator_code = "short"
        payload = build_guest_invoice_f1_payload(self.invoice, self.settings)
        self.assertEqual(payload["operator_oib"], "12345678901")
