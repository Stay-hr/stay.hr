from types import SimpleNamespace

from django.test import SimpleTestCase

from apps.billing.models import Invoice
from apps.billing.services.payment import (
    build_payment_note,
    fisk1_payment_code,
    resolve_payment_method,
)


class PaymentMethodTests(SimpleTestCase):
    def test_booking_source_stays_booking_method(self):
        reservation = SimpleNamespace(
            payment_provider="",
            payment_status="",
            source="booking.com",
        )
        self.assertEqual(
            resolve_payment_method(reservation),
            Invoice.PaymentMethod.BOOKING,
        )

    def test_booking_provider_stays_booking_method(self):
        reservation = SimpleNamespace(
            payment_provider="Payments by Booking.com",
            payment_status="paid",
            source="channex",
        )
        self.assertEqual(
            resolve_payment_method(reservation),
            Invoice.PaymentMethod.BOOKING,
        )

    def test_booking_maps_to_f73_card(self):
        self.assertEqual(fisk1_payment_code(Invoice.PaymentMethod.BOOKING), "K")

    def test_transfer_still_maps_to_f73_transfer(self):
        self.assertEqual(fisk1_payment_code(Invoice.PaymentMethod.TRANSFER), "T")

    def test_booking_note_says_card(self):
        reservation = SimpleNamespace(payment_provider="Payments by Booking.com")
        note = build_payment_note(reservation, Invoice.PaymentMethod.BOOKING)
        self.assertIn("KARTICE", note)
        self.assertIn("Payments by Booking.com", note)
        self.assertNotIn("TRANSAKCIJSKI", note)
