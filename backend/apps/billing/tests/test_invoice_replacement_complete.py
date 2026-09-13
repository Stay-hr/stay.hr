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
from apps.billing.services.invoice_replacement_issue import (
    complete_replacement,
    issue_replacement_storno,
)
from apps.billing.services.invoice_replacement_service import (
    open_replacement_case,
    update_replacement_recipient,
    verify_replacement_recipient,
)
from apps.billing.services.invoice_resolution import resolve_effective_invoice
from apps.billing.services.pdf import render_invoice_html
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


class CompleteReplacementTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Complete Tenant",
            slug="invoice-replacement-complete",
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
            username="complete-admin",
            password="x",
            is_staff=True,
        )
        self.other_actor = get_user_model().objects.create_user(
            username="other-complete-admin",
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

    def test_complete_mirrors_original_with_frozen_recipient_buyer(self):
        case = self._verified_case()
        issue_replacement_storno(case=case)
        with (
            patch("apps.billing.tasks.fiscalize_invoice.delay") as fiscalize,
            patch("apps.billing.tasks.send_invoice_email_task.delay") as email,
        ):
            replacement = complete_replacement(case=case, actor=self.actor)

        fiscalize.assert_not_called()
        email.assert_not_called()
        case.refresh_from_db()
        self.original.refresh_from_db()
        self.settings.refresh_from_db()
        self.assertEqual(case.status, InvoiceReplacement.Status.COMPLETED)
        self.assertEqual(case.replacement_invoice_id, replacement.pk)
        self.assertEqual(case.completed_by_id, self.actor.pk)
        self.assertIsNotNone(case.completed_at)
        self.assertEqual(replacement.invoice_number, "261-ROOMS-1")
        self.assertEqual(replacement.sequence_number, 261)
        self.assertEqual(self.settings.invoice_sequence, 261)
        self.assertEqual(replacement.total, Decimal("295.36"))
        self.assertEqual(replacement.subtotal, Decimal("261.38"))
        self.assertEqual(replacement.buyer_name, "PRO AUTOMATIKA")
        self.assertEqual(replacement.buyer_document_number, "87357644223")
        self.assertEqual(replacement.buyer_address, "Novo naselje 19E, 22214 Bilice")
        self.assertEqual(replacement.buyer_country, "Hrvatska")
        self.assertEqual(replacement.payment_method, Invoice.PaymentMethod.BOOKING)
        self.assertEqual(replacement.fiscal_status, Invoice.FiscalStatus.PENDING)
        self.assertTrue(replacement.zki)
        line = replacement.lines.get()
        self.assertEqual(line.quantity, Decimal("2"))
        self.assertEqual(line.unit_price, Decimal("130.00"))
        self.assertEqual(line.description, "Noćenje")
        self.assertEqual(self.original.buyer_name, "DARIO PREZEC")
        self.assertEqual(self.reservation.buyer_company_name, "")
        self.assertEqual(resolve_effective_invoice(self.reservation).pk, replacement.pk)

        html = render_invoice_html(replacement, self.settings)
        self.assertIn("Zamjenjuje račun 259-ROOMS-1", html)
        self.assertIn("storno 260-ROOMS-1", html)
        self.assertIn("PRO AUTOMATIKA", html)
        self.assertNotIn("DARIO PREZEC", html)

    def test_complete_is_idempotent_and_does_not_rewrite_audit(self):
        case = self._verified_case()
        issue_replacement_storno(case=case)
        first = complete_replacement(case=case, actor=self.actor)
        completed_at = InvoiceReplacement.objects.get(pk=case.pk).completed_at
        second = complete_replacement(case=case, actor=self.other_actor)
        case.refresh_from_db()
        self.settings.refresh_from_db()
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(case.completed_by_id, self.actor.pk)
        self.assertEqual(case.completed_at, completed_at)
        self.assertEqual(Invoice.objects.filter(reservation=self.reservation).count(), 3)
        self.assertEqual(self.settings.invoice_sequence, 261)

    def test_rejects_before_storno_without_consuming_sequence(self):
        case = self._verified_case()
        with self.assertRaises(InvoiceReplacementError) as ctx:
            complete_replacement(case=case, actor=self.actor)
        self.assertEqual(ctx.exception.reason, ReplacementRejectReason.STORNO_MISSING)
        self.settings.refresh_from_db()
        self.assertEqual(self.settings.invoice_sequence, 259)
        self.assertEqual(Invoice.objects.filter(reservation=self.reservation).count(), 1)

    def test_rejects_issuer_oib_mismatch_before_sequence(self):
        case = self._verified_case()
        issue_replacement_storno(case=case)
        self.settings.issuer_oib = "10987654321"
        self.settings.save(update_fields=["issuer_oib", "updated_at"])
        with self.assertRaises(InvoiceReplacementError) as ctx:
            complete_replacement(case=case, actor=self.actor)
        self.assertEqual(ctx.exception.reason, ReplacementRejectReason.ISSUER_OIB_MISMATCH)
        self.settings.refresh_from_db()
        self.assertEqual(self.settings.invoice_sequence, 260)
        case.refresh_from_db()
        self.assertEqual(case.status, InvoiceReplacement.Status.OPEN)
        self.assertIsNone(case.replacement_invoice_id)

    def test_lock_order_and_no_cis_or_email(self):
        source = inspect.getsource(complete_replacement)
        self.assertIn("Reservation.objects.select_for_update", source)
        self.assertIn("_lock_case", source)
        self.assertIn("_lock_recipient", source)
        self.assertLess(
            source.index("Reservation.objects.select_for_update"),
            source.index("_lock_case"),
        )
        self.assertLess(source.index("can_complete"), source.index("_next_invoice_number"))
        self.assertLess(source.index("expected_issuer_oib"), source.index("_next_invoice_number"))
        self.assertLess(source.index("Invoice.objects.create"), source.index("COMPLETED"))
        self.assertNotIn("fiscalize_invoice", source)
        self.assertNotIn("send_invoice_email", source)


class CompleteReplacementRaceTests(TransactionTestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Complete Race Tenant",
            slug="invoice-replacement-complete-race",
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
            username="complete-race-admin",
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
        issue_replacement_storno(case=self.case)

    def test_concurrent_complete_creates_one_invoice(self):
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

        def complete_after_lock():
            try:
                started.wait(timeout=5)
                results.append(complete_replacement(case=self.case, actor=self.actor))
            except BaseException as exc:
                errors.append(exc)
            finally:
                connection.close()

        holder = threading.Thread(target=hold_reservation)
        first = threading.Thread(target=complete_after_lock)
        second = threading.Thread(target=complete_after_lock)
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
        self.assertEqual(Invoice.objects.filter(reservation=self.reservation).count(), 3)
        self.settings.refresh_from_db()
        self.assertEqual(self.settings.invoice_sequence, 261)
        self.assertEqual(
            InvoiceReplacement.objects.get(pk=self.case.pk).status,
            InvoiceReplacement.Status.COMPLETED,
        )
