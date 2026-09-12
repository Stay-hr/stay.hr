import threading
import time
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test import TransactionTestCase

from apps.billing.models import Invoice, InvoiceReplacement
from apps.billing.services.invoice_replacement_service import open_replacement_case
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant


class InvoiceReplacementOpenRaceTests(TransactionTestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Replacement Race Tenant",
            slug="invoice-replacement-race",
        )
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="P",
            slug="p-race",
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            check_in=date(2026, 9, 10),
            check_out=date(2026, 9, 12),
            status=Reservation.Status.CHECKED_OUT,
            booker_name="DARIO PREZEC",
            amount=Decimal("100.00"),
        )
        self.actor = get_user_model().objects.create_user(
            username="race-admin",
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
            subtotal=Decimal("88.50"),
            vat_amount=Decimal("11.50"),
            total=Decimal("100.00"),
        )

    def _wait_until_blocked(self, thread: threading.Thread) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not thread.is_alive():
                return
            time.sleep(0.05)
            if thread.is_alive():
                time.sleep(0.15)
                return
        self.fail("timed out waiting for the lock waiter")

    def test_concurrent_open_creates_one_case(self):
        started = threading.Event()
        release = threading.Event()
        results: list[InvoiceReplacement] = []
        errors: list[BaseException] = []

        def hold_reservation():
            try:
                with transaction.atomic():
                    Reservation.objects.select_for_update().get(pk=self.reservation.pk)
                    started.set()
                    self.assertTrue(release.wait(timeout=5))
            finally:
                connection.close()

        def open_after_lock():
            try:
                started.wait(timeout=5)
                results.append(
                    open_replacement_case(
                        original=self.original,
                        actor=self.actor,
                        reason="Wrong buyer on issued invoice",
                        original_issuer_oib="12345678901",
                        original_issuer_oib_source="259-ROOMS-1 PDF header",
                    )
                )
            except BaseException as exc:
                errors.append(exc)
            finally:
                connection.close()

        holder = threading.Thread(target=hold_reservation)
        first = threading.Thread(target=open_after_lock)
        second = threading.Thread(target=open_after_lock)
        holder.start()
        self.assertTrue(started.wait(timeout=5))
        first.start()
        second.start()
        self._wait_until_blocked(first)
        self._wait_until_blocked(second)
        release.set()
        holder.join(timeout=5)
        first.join(timeout=5)
        second.join(timeout=5)
        self.assertFalse(holder.is_alive())
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].pk, results[1].pk)
        self.assertEqual(InvoiceReplacement.objects.count(), 1)
        self.assertEqual(Invoice.objects.filter(reservation=self.reservation).count(), 1)
