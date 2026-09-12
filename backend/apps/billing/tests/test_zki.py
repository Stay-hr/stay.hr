from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import SimpleTestCase, TestCase

from apps.billing.models import InvoiceLine, TenantFiscalSettings
from apps.billing.services.fiskal_platform.payload import build_guest_invoice_f1_payload
from apps.billing.services.invoice_builder import BuiltInvoice, BuiltInvoiceLine
from apps.billing.services.issue import issue_guest_invoice
from apps.billing.services.zki import (
    build_zki_input_string,
    calculate_zki,
    format_amount_for_zki,
    format_datetime_for_zki,
    load_fiscal_private_key,
)
from apps.billing.tests.helpers import make_test_p12
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant


def _test_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class ZkiTests(SimpleTestCase):
    def test_format_amount_for_zki_uses_dot(self):
        self.assertEqual(format_amount_for_zki(Decimal("150")), "150.00")
        self.assertEqual(format_amount_for_zki(Decimal("1246.5")), "1246.50")
        self.assertNotIn(",", format_amount_for_zki(Decimal("1246.5")))

    def test_format_datetime_for_zki(self):
        dt = datetime(2026, 4, 15, 14, 35, 22)
        self.assertEqual(format_datetime_for_zki(dt), "15.04.2026 14:35:22")

    def test_zki_input_string_matches_pravilnik_order(self):
        payload = build_zki_input_string(
            oib="91381354893",
            issued_at=datetime(2026, 9, 12, 10, 30, 0),
            invoice_number="257",
            business_premise_code="ROOMS",
            payment_device_code="1",
            total=Decimal("150.00"),
        )
        self.assertEqual(payload, "9138135489312.09.2026 10:30:00257ROOMS1150.00")

    def test_calculate_zki_is_deterministic_for_same_key(self):
        key = _test_key()
        issued_at = datetime(2026, 4, 15, 14, 35, 22)
        kwargs = dict(
            oib="12345678901",
            issued_at=issued_at,
            invoice_number="1",
            business_premise_code="PP1",
            payment_device_code="1",
            total=Decimal("150.00"),
            private_key=key,
        )
        first = calculate_zki(**kwargs)
        second = calculate_zki(**kwargs)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 32)
        self.assertRegex(first, r"^[a-f0-9]{32}$")

    def test_calculate_zki_changes_with_different_key(self):
        issued_at = datetime(2026, 4, 15, 14, 35, 22)
        common = dict(
            oib="12345678901",
            issued_at=issued_at,
            invoice_number="1",
            business_premise_code="PP1",
            payment_device_code="1",
            total=Decimal("150.00"),
        )
        first = calculate_zki(**common, private_key=_test_key())
        second = calculate_zki(**common, private_key=_test_key())
        self.assertNotEqual(first, second)


class IssuedInvoiceZkiTests(TestCase):
    def test_issue_guest_invoice_stores_p12_zki_and_payload_reuses_it(self):
        tenant = Tenant.objects.create(name="ZKI Tenant", slug="zki-tenant")
        settings = TenantFiscalSettings.objects.create(
            tenant=tenant,
            is_vat_registered=True,
            issuer_oib="12345678901",
            issuer_name="Issuer d.o.o.",
            business_premise_code="PP1",
            payment_device_code="1",
            certificate_file=make_test_p12(password="secret", oib="12345678901"),
        )
        settings.set_certificate_password("secret")
        settings.save()

        reservation = Reservation.objects.create(
            tenant=tenant,
            property=Property.objects.create(tenant=tenant, name="P", slug="p"),
            check_in=date(2026, 4, 15),
            check_out=date(2026, 4, 16),
            status=Reservation.Status.CHECKED_OUT,
            booker_name="Guest Guest",
            amount=Decimal("100.00"),
        )
        built = BuiltInvoice(
            buyer_name="Guest Guest",
            buyer_document_number="",
            buyer_address="",
            buyer_country="HR",
            payment_method="booking",
            payment_note="",
            lines=(
                BuiltInvoiceLine(
                    sort_order=1,
                    line_kind=InvoiceLine.LineKind.ACCOMMODATION,
                    description="Noćenje",
                    quantity=Decimal("1"),
                    unit_price=Decimal("88.50"),
                    vat_rate=Decimal("13.00"),
                    vat_amount=Decimal("11.50"),
                    line_total=Decimal("100.00"),
                ),
            ),
            subtotal=Decimal("88.50"),
            vat_amount=Decimal("11.50"),
            total=Decimal("100.00"),
            currency="EUR",
        )

        with (
            patch(
                "apps.billing.services.issue.build_invoice_from_reservation",
                return_value=built,
            ),
            patch("apps.billing.services.issue.render_invoice_pdf"),
        ):
            invoice = issue_guest_invoice(reservation)

        expected = calculate_zki(
            oib=settings.issuer_oib,
            issued_at=invoice.issued_at,
            invoice_number=str(invoice.sequence_number),
            business_premise_code=settings.business_premise_code,
            payment_device_code=settings.payment_device_code,
            total=invoice.total,
            private_key=load_fiscal_private_key(settings),
        )
        self.assertEqual(invoice.zki, expected)
        self.assertEqual(len(invoice.zki), 32)
        self.assertRegex(invoice.zki, r"^[a-f0-9]{32}$")

        payload = build_guest_invoice_f1_payload(invoice, settings)
        self.assertEqual(payload["zki"], invoice.zki)
