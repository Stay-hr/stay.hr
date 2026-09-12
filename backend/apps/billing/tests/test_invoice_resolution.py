import inspect
from datetime import date, datetime
from decimal import Decimal
from django.test import TestCase

from apps.api.billing_views import (
    ReservationInvoiceView,
    _get_reservation_invoice,
)
from apps.api.reception_serializers import ReservationTimelineSerializer
from apps.api.reception_views import _reservation_queryset
from apps.billing.exceptions import InvoiceGraphError
from apps.billing.management.commands.regenerate_invoice_pdf import Command as RegenCommand
from apps.billing.models import Invoice
from apps.billing.services.invoice_resolution import resolve_effective_invoice
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

    def test_more_than_one_invoice_is_fail_closed(self):
        self._add_invoice(sequence_number=1)
        self._add_invoice(sequence_number=2)
        with self.assertRaises(InvoiceGraphError):
            resolve_effective_invoice(self.reservation)

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
