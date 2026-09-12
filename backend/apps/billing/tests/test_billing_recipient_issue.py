from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from apps.billing.exceptions import BillingRecipientError, BillingRecipientIssuanceDeferred
from apps.billing.models import BillingRecipient, Invoice, InvoiceLine, TenantFiscalSettings
from apps.billing.services.billing_recipient import RecipientRejectReason
from apps.billing.services.billing_recipient_service import create_open_recipient
from apps.billing.services.invoice_builder import BuiltInvoice, BuiltInvoiceLine
from apps.billing.services.issue import issue_guest_invoice
from apps.billing.tests.helpers import make_test_p12
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant


def _built_invoice(*, buyer_name: str = "Guest Guest") -> BuiltInvoice:
    return BuiltInvoice(
        buyer_name=buyer_name,
        buyer_document_number="GUESTDOC",
        buyer_address="Guest Street 1",
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


class BillingRecipientRuntimeIssuanceTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Recipient Issue Tenant",
            slug="billing-recipient-issue",
        )
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="P",
            slug="p",
        )
        self.settings = TenantFiscalSettings.objects.create(
            tenant=self.tenant,
            is_vat_registered=True,
            issuer_oib="12345678901",
            issuer_name="Issuer d.o.o.",
            business_premise_code="PP1",
            payment_device_code="1",
            certificate_file=make_test_p12(password="secret", oib="12345678901"),
        )
        self.settings.set_certificate_password("secret")
        self.settings.save()
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            check_in=date(2026, 9, 10),
            check_out=date(2026, 9, 12),
            status=Reservation.Status.CHECKED_OUT,
            booker_name="Guest Guest",
            amount=Decimal("100.00"),
            buyer_company_name="",
            buyer_oib="",
            buyer_address="",
        )

    def _ready_fields(self) -> dict:
        return {
            "company_name": "Example GmbH",
            "tax_id": "DE123456789",
            "tax_id_country": "DE",
            "country": "DE",
            "address": "Unter den Linden 1",
            "postal_code": "10115",
            "city": "Berlin",
            "email": "billing@example.com",
        }

    @patch("apps.billing.services.issue._next_invoice_number")
    @patch("apps.billing.services.issue.get_fiscal_settings_for_reservation")
    def test_requested_defers_before_issuance_side_effects(
        self,
        mock_settings,
        mock_next_number,
    ):
        row = create_open_recipient(self.reservation, {"company_name": "Example GmbH"})
        with self.assertRaises(BillingRecipientIssuanceDeferred) as ctx:
            issue_guest_invoice(self.reservation)
        self.assertEqual(ctx.exception.code, "billing_recipient_incomplete")
        mock_settings.assert_not_called()
        mock_next_number.assert_not_called()
        row.refresh_from_db()
        self.assertEqual(row.status, BillingRecipient.Status.REQUESTED)
        self.assertEqual(Invoice.objects.count(), 0)
        self.settings.refresh_from_db()
        self.assertEqual(self.settings.invoice_sequence, 0)

    def test_existing_invoice_wins_over_requested(self):
        create_open_recipient(self.reservation, {"company_name": "Example GmbH"})
        existing = Invoice.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            invoice_number="1-PP1-1",
            sequence_number=1,
            issued_at=datetime(2026, 9, 12, 11, 3, 0),
            buyer_name="Guest Guest",
            payment_method=Invoice.PaymentMethod.CARD,
            subtotal=Decimal("88.50"),
            vat_amount=Decimal("11.50"),
            total=Decimal("100.00"),
        )
        returned = issue_guest_invoice(self.reservation)
        self.assertEqual(returned.pk, existing.pk)
        self.assertEqual(Invoice.objects.count(), 1)

    def test_ready_applies_company_snapshot_not_guest_buyer(self):
        recipient = create_open_recipient(self.reservation, self._ready_fields())
        captured: dict = {}
        original_apply = __import__(
            "apps.billing.services.billing_recipient_apply",
            fromlist=["apply_recipient_to_new_invoice"],
        ).apply_recipient_to_new_invoice

        def _capture(*, recipient, invoice_create_kwargs):
            captured["kwargs"] = dict(invoice_create_kwargs)
            return original_apply(
                recipient=recipient,
                invoice_create_kwargs=invoice_create_kwargs,
            )

        with (
            patch(
                "apps.billing.services.issue.build_invoice_from_reservation",
                return_value=_built_invoice(),
            ),
            patch("apps.billing.services.issue.render_invoice_pdf"),
            patch(
                "apps.billing.services.issue.apply_recipient_to_new_invoice",
                side_effect=_capture,
            ),
        ):
            invoice = issue_guest_invoice(self.reservation)

        self.assertEqual(invoice.buyer_name, "Example GmbH")
        self.assertEqual(invoice.buyer_document_number, "DE123456789")
        self.assertEqual(invoice.buyer_address, "Unter den Linden 1, 10115 Berlin")
        self.assertEqual(invoice.buyer_country, "Njemačka")
        self.assertNotEqual(invoice.buyer_name, "Guest Guest")
        self.assertEqual(invoice.lines.count(), 1)
        recipient.refresh_from_db()
        self.assertEqual(recipient.status, BillingRecipient.Status.APPLIED)
        self.assertEqual(recipient.applied_invoice_id, invoice.pk)
        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.buyer_company_name, "")
        self.assertEqual(self.reservation.buyer_oib, "")
        self.assertEqual(self.reservation.buyer_address, "")
        kwargs = captured["kwargs"]
        self.assertFalse(
            {
                "buyer_name",
                "buyer_document_number",
                "buyer_address",
                "buyer_country",
            }.intersection(kwargs)
        )
        self.assertEqual(kwargs["total"], Decimal("100.00"))
        self.assertEqual(kwargs["payment_method"], "booking")
        self.assertEqual(kwargs["issuer_oib"], "12345678901")
        self.assertEqual(kwargs["issuer_name"], "Issuer d.o.o.")
        self.assertEqual(invoice.issuer_oib, "12345678901")
        self.assertEqual(invoice.business_premise_code, "PP1")
        self.assertEqual(invoice.payment_device_code, "1")
        self.assertEqual(invoice.reservation_reference, str(self.reservation.pk))

    def test_apply_failure_does_not_fall_back_to_guest_and_rolls_back_sequence(self):
        recipient = create_open_recipient(self.reservation, self._ready_fields())
        with (
            patch(
                "apps.billing.services.issue.build_invoice_from_reservation",
                return_value=_built_invoice(),
            ),
            patch("apps.billing.services.issue.render_invoice_pdf"),
            patch(
                "apps.billing.services.issue.apply_recipient_to_new_invoice",
                side_effect=BillingRecipientError(
                    "Billing recipient cannot be applied.",
                    reason=RecipientRejectReason.NOT_READY,
                ),
            ),
        ):
            with self.assertRaises(BillingRecipientError) as ctx:
                issue_guest_invoice(self.reservation)
        self.assertEqual(ctx.exception.reason, RecipientRejectReason.NOT_READY)
        self.assertEqual(Invoice.objects.count(), 0)
        recipient.refresh_from_db()
        self.assertEqual(recipient.status, BillingRecipient.Status.READY)
        self.assertIsNone(recipient.applied_invoice_id)
        self.settings.refresh_from_db()
        self.assertEqual(self.settings.invoice_sequence, 0)
        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.buyer_company_name, "")
