import inspect
from datetime import date, datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.api.billing_views import (
    ReservationInvoiceView,
    _get_reservation_invoice,
)
from apps.api.reception_serializers import ReservationTimelineSerializer
from apps.api.reception_views import _reservation_queryset
from apps.billing.exceptions import InvoiceGraphError
from apps.billing.management.commands.regenerate_invoice_pdf import Command as RegenCommand
from apps.billing.models import Invoice, InvoiceReplacement
from apps.billing.services.invoice_resolution import (
    has_open_post_storno_gap,
    resolve_effective_invoice,
)
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant


class InvoiceResolutionTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Effective Invoice Tenant",
            slug="effective-invoice",
        )
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="P",
            slug="p",
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            check_in=date(2026, 9, 10),
            check_out=date(2026, 9, 12),
            status=Reservation.Status.CHECKED_OUT,
            booker_name="Guest Guest",
            amount=Decimal("100.00"),
        )
        self.actor = get_user_model().objects.create_user(
            username="effective-resolver",
            password="x",
            is_staff=True,
        )

    def _add_invoice(self, reservation=None, *, sequence_number=1) -> Invoice:
        reservation = reservation or self.reservation
        return Invoice.objects.create(
            tenant=reservation.tenant,
            reservation=reservation,
            invoice_number=f"{sequence_number}-ROOMS-1",
            sequence_number=sequence_number,
            issued_at=datetime(2026, 9, 12, 11, 3, 0),
            buyer_name="Guest Guest",
            payment_method=Invoice.PaymentMethod.CARD,
            subtotal=Decimal("88.50"),
            vat_amount=Decimal("11.50"),
            total=Decimal("100.00"),
        )

    def test_no_invoice_returns_none(self):
        self.assertIsNone(resolve_effective_invoice(self.reservation))

    def test_sole_invoice_is_effective(self):
        invoice = self._add_invoice()
        self.assertEqual(resolve_effective_invoice(self.reservation), invoice)

    def test_other_reservation_invoice_is_ignored(self):
        other = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            check_in=date(2026, 9, 10),
            check_out=date(2026, 9, 12),
            status=Reservation.Status.CHECKED_OUT,
            booker_name="Other Guest",
            amount=Decimal("80.00"),
        )
        self._add_invoice(other, sequence_number=2)
        self.assertIsNone(resolve_effective_invoice(self.reservation))

    def test_more_than_one_invoice_without_cases_is_fail_closed(self):
        self._add_invoice(sequence_number=1)
        self._add_invoice(sequence_number=2)
        with self.assertRaises(InvoiceGraphError):
            resolve_effective_invoice(self.reservation)

    def test_open_without_storno_keeps_original_effective(self):
        original = self._add_invoice(sequence_number=1)
        InvoiceReplacement.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            original_invoice=original,
            reason="wrong buyer",
            opened_by=self.actor,
            opened_at=timezone.now(),
        )
        self.assertEqual(resolve_effective_invoice(self.reservation), original)
        self.assertFalse(has_open_post_storno_gap(self.reservation))

    def test_open_with_storno_is_gap(self):
        original = self._add_invoice(sequence_number=1)
        storno = self._add_invoice(sequence_number=2)
        InvoiceReplacement.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            original_invoice=original,
            storno_invoice=storno,
            reason="wrong buyer",
            opened_by=self.actor,
            opened_at=timezone.now(),
        )
        self.assertIsNone(resolve_effective_invoice(self.reservation))
        self.assertTrue(has_open_post_storno_gap(self.reservation))

    def test_completed_returns_replacement(self):
        original = self._add_invoice(sequence_number=1)
        storno = self._add_invoice(sequence_number=2)
        replacement = self._add_invoice(sequence_number=3)
        InvoiceReplacement.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            original_invoice=original,
            storno_invoice=storno,
            replacement_invoice=replacement,
            status=InvoiceReplacement.Status.COMPLETED,
            reason="wrong buyer",
            opened_by=self.actor,
            opened_at=timezone.now(),
            completed_by=self.actor,
            completed_at=timezone.now(),
        )
        self.assertEqual(resolve_effective_invoice(self.reservation), replacement)
        self.assertFalse(has_open_post_storno_gap(self.reservation))

    def test_cancelled_keeps_original_effective(self):
        original = self._add_invoice(sequence_number=1)
        InvoiceReplacement.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            original_invoice=original,
            status=InvoiceReplacement.Status.CANCELLED,
            reason="wrong buyer",
            opened_by=self.actor,
            opened_at=timezone.now(),
            cancelled_by=self.actor,
            cancelled_at=timezone.now(),
            cancel_reason="opened by mistake",
        )
        self.assertEqual(resolve_effective_invoice(self.reservation), original)

    def test_second_completed_link_returns_latest_replacement(self):
        first = self._add_invoice(sequence_number=1)
        storno_first = self._add_invoice(sequence_number=2)
        second = self._add_invoice(sequence_number=3)
        storno_second = self._add_invoice(sequence_number=4)
        third = self._add_invoice(sequence_number=5)
        InvoiceReplacement.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            original_invoice=first,
            storno_invoice=storno_first,
            replacement_invoice=second,
            status=InvoiceReplacement.Status.COMPLETED,
            reason="first",
            opened_by=self.actor,
            opened_at=timezone.now(),
            completed_by=self.actor,
            completed_at=timezone.now(),
        )
        InvoiceReplacement.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            original_invoice=second,
            storno_invoice=storno_second,
            replacement_invoice=third,
            status=InvoiceReplacement.Status.COMPLETED,
            reason="second",
            opened_by=self.actor,
            opened_at=timezone.now(),
            completed_by=self.actor,
            completed_at=timezone.now(),
        )
        self.assertEqual(resolve_effective_invoice(self.reservation), third)

    def test_does_not_use_last_or_reservation_invoice(self):
        source = inspect.getsource(resolve_effective_invoice)
        self.assertNotIn(".last()", source)
        self.assertNotIn("reservation.invoice", source)
        self.assertNotIn('getattr(reservation, "invoice"', source)


class EffectiveInvoiceReadPathWiringTests(TestCase):
    def test_reception_and_billing_reads_use_resolver(self):
        self.assertIn(
            "resolve_effective_invoice",
            inspect.getsource(ReservationInvoiceView.get),
        )
        self.assertIn(
            "resolve_effective_invoice",
            inspect.getsource(ReservationInvoiceView.post),
        )
        self.assertIn(
            "resolve_effective_invoice",
            inspect.getsource(_get_reservation_invoice),
        )
        self.assertIn(
            "resolve_effective_invoice",
            inspect.getsource(ReservationTimelineSerializer.get_invoice_summary),
        )
        self.assertIn(
            "resolve_effective_invoice",
            inspect.getsource(RegenCommand.handle),
        )
        self.assertNotIn(
            'select_related("invoice")',
            inspect.getsource(_reservation_queryset),
        )
        self.assertNotIn(
            'select_related("invoice")',
            inspect.getsource(ReservationInvoiceView.post),
        )
        self.assertNotIn(
            'select_related("invoice")',
            inspect.getsource(_get_reservation_invoice),
        )
