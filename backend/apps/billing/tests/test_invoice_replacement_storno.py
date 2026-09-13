import inspect
import threading
import time
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase

from apps.billing.exceptions import InvoiceReplacementError
from apps.billing.models import Invoice, InvoiceLine, InvoiceReplacement, TenantFiscalSettings
from apps.billing.services.invoice_replacement import ReplacementRejectReason
from apps.billing.services.invoice_replacement_issue import issue_replacement_storno
from apps.billing.services.invoice_replacement_service import (
    open_replacement_case,
    update_replacement_recipient,
    verify_replacement_recipient,
)
from apps.billing.services.pdf import render_invoice_html
from apps.billing.services.zki import build_zki_input_string, format_amount_for_zki
from apps.billing.tests.helpers import make_test_p12
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant


def _ready_fields() -> dict:
    return {
        "company_name": "PRO AUTOMATIKA",
        "tax_id": "87357644223",
        "tax_id_country": "HR",
        "country": "HR",
        "address": "Novo naselje 19E",
        "postal_code": "22214",
        "city": "Bilice",
        "email": "domagoj.perjanec@pro-automatika.hr",
        "source": "staff",
        "source_excerpt": "R1 request from booking message",
    }


class IssueReplacementStornoTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Storno Tenant",
            slug="invoice-replacement-storno",
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
            issuer_address="Ulica 1",
            business_premise_code="ROOMS",
            payment_device_code="1",
            invoice_sequence=259,
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
            booker_name="DARIO PREZEC",
            booking_code="BK-1159",
            amount=Decimal("295.36"),
            buyer_company_name="",
            buyer_oib="",
            buyer_address="",
        )
        self.actor = get_user_model().objects.create_user(
            username="storno-admin",
            password="x",
            is_staff=True,
        )
        self.original = Invoice.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            invoice_number="259-ROOMS-1",
            sequence_number=259,
            issued_at=datetime(2026, 5, 27, 7, 21, tzinfo=ZoneInfo("Europe/Zagreb")),
            buyer_name="DARIO PREZEC",
            buyer_document_number="119907920",
            buyer_address="ZAGREB, GRAD ZAGREB, OPOROVEČKI VINOGRADI 66 A",
            buyer_country="Hrvatska",
            payment_method=Invoice.PaymentMethod.BOOKING,
            payment_note="Booking.com",
            subtotal=Decimal("261.38"),
            vat_amount=Decimal("33.98"),
            total=Decimal("295.36"),
        )
        InvoiceLine.objects.create(
            invoice=self.original,
            sort_order=1,
            line_kind=InvoiceLine.LineKind.ACCOMMODATION,
            description="Noćenje",
            quantity=Decimal("2"),
            unit_price=Decimal("130.00"),
            vat_rate=Decimal("13.00"),
            vat_amount=Decimal("29.91"),
            line_total=Decimal("260.00"),
        )

    def _verified_case(self) -> InvoiceReplacement:
        case = open_replacement_case(
            original=self.original,
            actor=self.actor,
            reason="Wrong buyer on issued invoice",
            original_issuer_oib="12345678901",
            original_issuer_oib_source="259-ROOMS-1 PDF header",
        )
        update_replacement_recipient(case=case, fields=_ready_fields())
        verify_replacement_recipient(case=case, actor=self.actor)
        return case

    def test_storno_is_exact_negative_with_local_zki_and_storno_pdf(self):
        case = self._verified_case()
        with (
            patch("apps.billing.tasks.fiscalize_invoice.delay") as fiscalize,
            patch("apps.billing.tasks.send_invoice_email_task.delay") as email,
        ):
            storno = issue_replacement_storno(case=case)

        fiscalize.assert_not_called()
        email.assert_not_called()
        case.refresh_from_db()
        self.original.refresh_from_db()
        self.settings.refresh_from_db()
        self.assertEqual(storno.pk, case.storno_invoice_id)
        self.assertEqual(storno.invoice_number, "260-ROOMS-1")
        self.assertEqual(storno.sequence_number, 260)
        self.assertEqual(self.settings.invoice_sequence, 260)
        self.assertEqual(storno.total, Decimal("-295.36"))
        self.assertEqual(storno.subtotal, Decimal("-261.38"))
        self.assertEqual(storno.vat_amount, Decimal("-33.98"))
        self.assertEqual(storno.buyer_name, "DARIO PREZEC")
        self.assertEqual(storno.buyer_document_number, "119907920")
        self.assertEqual(storno.payment_method, Invoice.PaymentMethod.BOOKING)
        self.assertEqual(storno.fiscal_status, Invoice.FiscalStatus.PENDING)
        self.assertTrue(storno.zki)
        line = storno.lines.get()
        self.assertEqual(line.quantity, Decimal("2"))
        self.assertEqual(line.unit_price, Decimal("-130.00"))
        self.assertEqual(line.description, "Noćenje")
        self.assertEqual(self.original.buyer_name, "DARIO PREZEC")
        self.assertEqual(self.original.total, Decimal("295.36"))
        self.assertEqual(self.reservation.buyer_company_name, "")

        zki_input = build_zki_input_string(
            oib=storno.issuer_oib,
            issued_at=storno.issued_at,
            invoice_number=str(storno.sequence_number),
            business_premise_code=storno.business_premise_code,
            payment_device_code=storno.payment_device_code,
            total=storno.total,
        )
        self.assertTrue(zki_input.endswith("-295.36"))
        self.assertEqual(format_amount_for_zki(Decimal("-295.36")), "-295.36")

        html = render_invoice_html(storno, self.settings)
        self.assertIn("STORNO računa 259-ROOMS-1", html)
        self.assertIn("DARIO PREZEC", html)
        self.assertNotIn("PRO AUTOMATIKA", html)

    def test_storno_is_idempotent_and_does_not_consume_another_number(self):
        case = self._verified_case()
        first = issue_replacement_storno(case=case)
        second = issue_replacement_storno(case=case)
        self.settings.refresh_from_db()
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Invoice.objects.filter(reservation=self.reservation).count(), 2)
        self.assertEqual(self.settings.invoice_sequence, 260)

    def test_rejects_unverified_before_sequence(self):
        case = open_replacement_case(
            original=self.original,
            actor=self.actor,
            reason="Wrong buyer",
            original_issuer_oib="12345678901",
            original_issuer_oib_source="259-ROOMS-1 PDF header",
        )
        update_replacement_recipient(case=case, fields=_ready_fields())
        with self.assertRaises(InvoiceReplacementError) as ctx:
            issue_replacement_storno(case=case)
        self.assertEqual(ctx.exception.reason, ReplacementRejectReason.RECIPIENT_NOT_VERIFIED)
        self.settings.refresh_from_db()
        self.assertEqual(self.settings.invoice_sequence, 259)
        self.assertEqual(Invoice.objects.filter(reservation=self.reservation).count(), 1)

    def test_rejects_issuer_oib_mismatch_before_sequence(self):
        case = self._verified_case()
        self.settings.issuer_oib = "10987654321"
        self.settings.save(update_fields=["issuer_oib", "updated_at"])
        with self.assertRaises(InvoiceReplacementError) as ctx:
            issue_replacement_storno(case=case)
        self.assertEqual(ctx.exception.reason, ReplacementRejectReason.ISSUER_OIB_MISMATCH)
        self.settings.refresh_from_db()
        self.assertEqual(self.settings.invoice_sequence, 259)
        self.assertIsNone(InvoiceReplacement.objects.get(pk=case.pk).storno_invoice_id)

    def test_recipient_freezes_after_storno(self):
        case = self._verified_case()
        issue_replacement_storno(case=case)
        with self.assertRaises(InvoiceReplacementError) as ctx:
            update_replacement_recipient(case=case, fields={"city": "Šibenik"})
        self.assertEqual(ctx.exception.reason, ReplacementRejectReason.RECIPIENT_FROZEN)

    def test_lock_order_and_no_cis_or_email(self):
        source = inspect.getsource(issue_replacement_storno)
        self.assertIn("Reservation.objects.select_for_update", source)
        self.assertIn("_lock_case", source)
        self.assertIn("_lock_recipient", source)
        self.assertLess(
            source.index("Reservation.objects.select_for_update"),
            source.index("_lock_case"),
        )
        self.assertLess(source.index("_lock_case"), source.index("_next_invoice_number"))
        self.assertLess(source.index("expected_issuer_oib"), source.index("_next_invoice_number"))
        self.assertNotIn("fiscalize_invoice", source)
        self.assertNotIn("send_invoice_email", source)


class IssueReplacementStornoRaceTests(TransactionTestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Storno Race Tenant",
            slug="invoice-replacement-storno-race",
        )
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="P",
            slug="p-race",
        )
        self.settings = TenantFiscalSettings.objects.create(
            tenant=self.tenant,
            is_vat_registered=True,
            issuer_oib="12345678901",
            issuer_name="Issuer d.o.o.",
            business_premise_code="ROOMS",
            payment_device_code="1",
            invoice_sequence=259,
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
            booker_name="DARIO PREZEC",
            amount=Decimal("295.36"),
        )
        self.actor = get_user_model().objects.create_user(
            username="storno-race-admin",
            password="x",
            is_staff=True,
        )
        self.original = Invoice.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            invoice_number="259-ROOMS-1",
            sequence_number=259,
            issued_at=datetime(2026, 5, 27, 7, 21, tzinfo=ZoneInfo("Europe/Zagreb")),
            buyer_name="DARIO PREZEC",
            payment_method=Invoice.PaymentMethod.BOOKING,
            subtotal=Decimal("261.38"),
            vat_amount=Decimal("33.98"),
            total=Decimal("295.36"),
        )
        InvoiceLine.objects.create(
            invoice=self.original,
            sort_order=1,
            line_kind=InvoiceLine.LineKind.ACCOMMODATION,
            description="Noćenje",
            quantity=Decimal("1"),
            unit_price=Decimal("261.38"),
            vat_rate=Decimal("13.00"),
            vat_amount=Decimal("33.98"),
            line_total=Decimal("295.36"),
        )
        self.case = open_replacement_case(
            original=self.original,
            actor=self.actor,
            reason="Wrong buyer",
            original_issuer_oib="12345678901",
            original_issuer_oib_source="259-ROOMS-1 PDF header",
        )
        update_replacement_recipient(case=self.case, fields=_ready_fields())
        verify_replacement_recipient(case=self.case, actor=self.actor)

    def test_concurrent_storno_creates_one_invoice(self):
        started = threading.Event()
        release = threading.Event()
        results: list[Invoice] = []
        errors: list[BaseException] = []

        def hold_reservation():
            try:
                with transaction.atomic():
                    Reservation.objects.select_for_update().get(pk=self.reservation.pk)
                    started.set()
                    self.assertTrue(release.wait(timeout=5))
            finally:
                connection.close()

        def issue_after_lock():
            try:
                started.wait(timeout=5)
                results.append(issue_replacement_storno(case=self.case))
            except BaseException as exc:
                errors.append(exc)
            finally:
                connection.close()

        holder = threading.Thread(target=hold_reservation)
        first = threading.Thread(target=issue_after_lock)
        second = threading.Thread(target=issue_after_lock)
        holder.start()
        self.assertTrue(started.wait(timeout=5))
        first.start()
        second.start()
        time.sleep(0.2)
        release.set()
        holder.join(timeout=5)
        first.join(timeout=5)
        second.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].pk, results[1].pk)
        self.assertEqual(Invoice.objects.filter(reservation=self.reservation).count(), 2)
        self.settings.refresh_from_db()
        self.assertEqual(self.settings.invoice_sequence, 260)
