from datetime import date, datetime
from decimal import Decimal

from django.db.models.deletion import ProtectedError
from django.test import TestCase

from apps.billing.exceptions import InvoiceGraphError
from apps.billing.models import Invoice
from apps.billing.services.invoice_resolution import resolve_effective_invoice
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant


class InvoiceReservationFkTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Invoice FK Tenant",
            slug="invoice-reservation-fk",
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

    def test_reservation_can_hold_multiple_invoices(self):
        first = self._add_invoice(sequence_number=1)
        second = self._add_invoice(sequence_number=2)
        self.assertEqual(
            list(self.reservation.invoices.order_by("sequence_number")),
            [first, second],
        )

    def test_related_name_is_invoices_not_invoice(self):
        self._add_invoice()
        self.assertTrue(hasattr(self.reservation, "invoices"))
        self.assertFalse(hasattr(self.reservation, "invoice"))

    def test_delete_reservation_with_invoice_is_protected(self):
        self._add_invoice()
        with self.assertRaises(ProtectedError):
            self.reservation.delete()
        self.assertTrue(Reservation.objects.filter(pk=self.reservation.pk).exists())
        self.assertEqual(Invoice.objects.filter(reservation=self.reservation).count(), 1)

    def test_delete_reservation_without_invoice_still_works(self):
        pk = self.reservation.pk
        self.reservation.delete()
        self.assertFalse(Reservation.objects.filter(pk=pk).exists())

    def test_two_persisted_invoices_are_fail_closed_until_graph_walk(self):
        self._add_invoice(sequence_number=1)
        self._add_invoice(sequence_number=2)
        with self.assertRaises(InvoiceGraphError):
            resolve_effective_invoice(self.reservation)
