from datetime import date, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.test import TestCase

from apps.billing.admin import regenerate_invoice_pdf as regenerate_invoice_pdf_action
from apps.billing.exceptions import InvoiceIssuerContextMissing
from apps.billing.models import Invoice, InvoiceLine, TenantFiscalSettings
from apps.billing.services.invoice_builder import BuiltInvoice, BuiltInvoiceLine
from apps.billing.services.issue import issue_guest_invoice
from apps.billing.services.issuer_context import (
    ISSUER_CONTEXT_FIELDS,
    has_frozen_issuer_context,
    require_frozen_issuer_context,
    snapshot_issuer_document_context,
)
from apps.billing.services.pdf import invoice_template_context, render_invoice_html
from apps.billing.tests.helpers import make_test_p12
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant


def _built_invoice() -> BuiltInvoice:
    return BuiltInvoice(
        buyer_name="Guest Guest",
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


class InvoiceIssuerContextTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Issuer Context Tenant",
            slug="invoice-issuer-context",
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
            issuer_address="Ulica 1\n22000 Šibenik",
            issuer_iban="HR1210010051863000160",
            operator_code="OIB-1",
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
            booking_code="BK-1159",
            external_id="ext-should-not-win",
            amount=Decimal("100.00"),
        )

    def _legacy_invoice(self) -> Invoice:
        return Invoice.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            invoice_number="259-ROOMS-1",
            sequence_number=259,
            issued_at=datetime(2026, 5, 27, 7, 21, tzinfo=ZoneInfo("Europe/Zagreb")),
            buyer_name="DARIO PREZEC",
            payment_method=Invoice.PaymentMethod.BOOKING,
            subtotal=Decimal("88.50"),
            vat_amount=Decimal("11.50"),
            total=Decimal("100.00"),
        )

    def _frozen_invoice(self, **overrides) -> Invoice:
        values = {
            "tenant": self.tenant,
            "reservation": self.reservation,
            "invoice_number": "1-PP1-1",
            "sequence_number": 1,
            "issued_at": datetime(2026, 9, 12, 11, 3, tzinfo=ZoneInfo("Europe/Zagreb")),
            "buyer_name": "Guest Guest",
            "payment_method": Invoice.PaymentMethod.BOOKING,
            "subtotal": Decimal("88.50"),
            "vat_amount": Decimal("11.50"),
            "total": Decimal("100.00"),
            "issuer_name": "Issuer d.o.o.",
            "issuer_address": "Ulica 1\n22000 Šibenik",
            "issuer_oib": "12345678901",
            "issuer_iban": "HR1210010051863000160",
            "operator_code": "OIB-1",
            "business_premise_code": "PP1",
            "payment_device_code": "1",
            "reservation_reference": "BK-1159",
        }
        values.update(overrides)
        return Invoice.objects.create(**values)

    def test_snapshot_omits_certificate_secrets(self):
        snapshot = snapshot_issuer_document_context(self.settings, self.reservation)
        self.assertEqual(set(snapshot), set(ISSUER_CONTEXT_FIELDS))
        self.assertNotIn("certificate_file", snapshot)
        self.assertNotIn("certificate_password", snapshot)
        self.assertNotIn("certificate_password_encrypted", snapshot)
        self.assertEqual(snapshot["issuer_oib"], "12345678901")
        self.assertEqual(snapshot["operator_code"], "OIB-1")
        self.assertEqual(snapshot["reservation_reference"], "BK-1159")

    def test_issue_guest_invoice_freezes_issuer_context(self):
        with (
            patch(
                "apps.billing.services.issue.build_invoice_from_reservation",
                return_value=_built_invoice(),
            ),
            patch("apps.billing.services.issue.render_invoice_pdf"),
        ):
            invoice = issue_guest_invoice(self.reservation)

        self.assertTrue(has_frozen_issuer_context(invoice))
        self.assertEqual(invoice.issuer_name, "Issuer d.o.o.")
        self.assertEqual(invoice.issuer_address, "Ulica 1\n22000 Šibenik")
        self.assertEqual(invoice.issuer_oib, "12345678901")
        self.assertEqual(invoice.issuer_iban, "HR1210010051863000160")
        self.assertEqual(invoice.operator_code, "OIB-1")
        self.assertEqual(invoice.business_premise_code, "PP1")
        self.assertEqual(invoice.payment_device_code, "1")
        self.assertEqual(invoice.reservation_reference, "BK-1159")

    def test_frozen_pdf_ignores_live_settings_and_reservation_changes(self):
        invoice = self._frozen_invoice()
        self.settings.issuer_name = "CHANGED d.o.o."
        self.settings.issuer_oib = "10987654321"
        self.settings.issuer_iban = "HR0000000000000000000"
        self.settings.operator_code = "CHANGED"
        self.settings.save()
        self.reservation.booking_code = "BK-CHANGED"
        self.reservation.external_id = "ext-changed"
        self.reservation.save()

        html = render_invoice_html(invoice, self.settings)
        context = invoice_template_context(invoice, self.settings)
        self.assertIn("Issuer d.o.o.", html)
        self.assertNotIn("CHANGED d.o.o.", html)
        self.assertIn("OIB: 12345678901", html)
        self.assertNotIn("10987654321", html)
        self.assertIn("Broj rezervacije: BK-1159", html)
        self.assertNotIn("BK-CHANGED", html)
        self.assertEqual(context["operator_code"], "OIB-1")
        self.assertEqual(context["issuer_iban"], "HR1210010051863000160")

    def test_legacy_empty_fields_still_render_from_live_settings(self):
        invoice = self._legacy_invoice()
        self.assertFalse(has_frozen_issuer_context(invoice))
        html = render_invoice_html(invoice, self.settings)
        self.assertIn("Issuer d.o.o.", html)
        self.assertIn("OIB: 12345678901", html)
        self.assertIn("Broj rezervacije: BK-1159", html)

        self.settings.issuer_name = "Live Fallback d.o.o."
        self.settings.save()
        html = render_invoice_html(invoice, self.settings)
        self.assertIn("Live Fallback d.o.o.", html)
        self.assertNotIn("Issuer d.o.o.", html)

    def test_save_rejects_issuer_mutation_and_legacy_backfill(self):
        frozen = self._frozen_invoice()
        frozen.issuer_name = "Mutated"
        with self.assertRaises(ValidationError) as mutated:
            frozen.save()
        self.assertIn("issuer_name", mutated.exception.message_dict)
        frozen.refresh_from_db()
        self.assertEqual(frozen.issuer_name, "Issuer d.o.o.")

        legacy = self._legacy_invoice()
        legacy.issuer_oib = "12345678901"
        legacy.issuer_name = "Issuer d.o.o."
        legacy.business_premise_code = "PP1"
        legacy.payment_device_code = "1"
        legacy.reservation_reference = "BK-1159"
        with self.assertRaises(ValidationError):
            legacy.save()
        legacy.refresh_from_db()
        self.assertEqual(legacy.issuer_oib, "")
        self.assertFalse(has_frozen_issuer_context(legacy))

    def test_regenerate_command_rejects_legacy_before_buyer_refresh(self):
        invoice = self._legacy_invoice()
        invoice.buyer_name = "SHOULD STAY"
        invoice.save(update_fields=["buyer_name", "updated_at"])
        with (
            patch(
                "apps.billing.management.commands.regenerate_invoice_pdf.refresh_invoice_buyer_from_reservation"
            ) as refresh,
            patch("apps.billing.management.commands.regenerate_invoice_pdf.render_invoice_pdf") as render,
            self.assertRaises(CommandError) as ctx,
        ):
            call_command("regenerate_invoice_pdf", invoice_id=invoice.pk)
        self.assertIn("frozen issuer context", str(ctx.exception))
        refresh.assert_not_called()
        render.assert_not_called()
        invoice.refresh_from_db()
        self.assertEqual(invoice.buyer_name, "SHOULD STAY")

    def test_regenerate_command_allows_frozen_invoice(self):
        invoice = self._frozen_invoice()
        with (
            patch(
                "apps.billing.management.commands.regenerate_invoice_pdf.refresh_invoice_buyer_from_reservation"
            ) as refresh,
            patch("apps.billing.management.commands.regenerate_invoice_pdf.render_invoice_pdf") as render,
        ):
            call_command("regenerate_invoice_pdf", invoice_id=invoice.pk)
        refresh.assert_called_once()
        render.assert_called_once()

    def test_admin_action_skips_legacy_without_mutating_buyer(self):
        invoice = self._legacy_invoice()
        invoice.buyer_name = "SHOULD STAY"
        invoice.save(update_fields=["buyer_name", "updated_at"])
        messages = []
        modeladmin = MagicMock()
        modeladmin.message_user.side_effect = lambda request, message, level=None: messages.append(
            (str(message), level)
        )
        with (
            patch("apps.billing.admin.refresh_invoice_buyer_from_reservation") as refresh,
            patch("apps.billing.admin.render_invoice_pdf") as render,
        ):
            regenerate_invoice_pdf_action(
                modeladmin,
                request=MagicMock(),
                queryset=Invoice.objects.filter(pk=invoice.pk),
            )
        refresh.assert_not_called()
        render.assert_not_called()
        invoice.refresh_from_db()
        self.assertEqual(invoice.buyer_name, "SHOULD STAY")
        self.assertTrue(any("Skipped 1" in message for message, _level in messages))

    def test_require_frozen_raises_for_legacy(self):
        invoice = self._legacy_invoice()
        with self.assertRaises(InvoiceIssuerContextMissing):
            require_frozen_issuer_context(invoice)
