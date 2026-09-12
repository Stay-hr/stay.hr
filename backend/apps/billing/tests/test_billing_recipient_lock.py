import inspect
import threading
import time
from datetime import date, datetime
from decimal import Decimal

from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase

from apps.billing.exceptions import BillingRecipientError
from apps.billing.models import BillingRecipient, Invoice
from apps.billing.services.billing_recipient import RecipientRejectReason
from apps.billing.services.billing_recipient_apply import apply_recipient_to_new_invoice
from apps.billing.services.billing_recipient_service import (
    create_open_recipient,
    update_open_recipient,
)
from apps.billing.services.issue import issue_guest_invoice
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant


def _ready_fields() -> dict:
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


class BillingRecipientIssueLockTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Recipient Lock Tenant",
            slug="billing-recipient-lock",
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
            buyer_company_name="",
            buyer_oib="",
            buyer_address="",
        )

    def _add_invoice(self) -> Invoice:
        return Invoice.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            invoice_number="1-ROOMS-1",
            sequence_number=1,
            issued_at=datetime(2026, 9, 12, 11, 3, 0),
            buyer_name="Guest Guest",
            payment_method=Invoice.PaymentMethod.CARD,
            subtotal=Decimal("88.50"),
            vat_amount=Decimal("11.50"),
            total=Decimal("100.00"),
        )

    def test_issue_does_not_wire_recipient_resolver(self):
        source = inspect.getsource(issue_guest_invoice)
        self.assertNotIn("resolve_billing_recipient_issuance", source)

    def test_issue_rereads_existing_invoice_after_reservation_lock(self):
        self.assertFalse(hasattr(self.reservation, "invoice"))
        invoice = self._add_invoice()
        returned = issue_guest_invoice(self.reservation)
        self.assertEqual(returned.pk, invoice.pk)
        self.assertEqual(Invoice.objects.count(), 1)


class BillingRecipientReservationLockRaceTests(TransactionTestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Recipient Race Tenant",
            slug="billing-recipient-race",
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
            booker_name="Guest Guest",
            amount=Decimal("100.00"),
            buyer_company_name="",
            buyer_oib="",
            buyer_address="",
        )

    def _add_invoice(self) -> Invoice:
        return Invoice.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            invoice_number="1-ROOMS-1",
            sequence_number=1,
            issued_at=datetime(2026, 9, 12, 11, 3, 0),
            buyer_name="Guest Guest",
            payment_method=Invoice.PaymentMethod.CARD,
            subtotal=Decimal("88.50"),
            vat_amount=Decimal("11.50"),
            total=Decimal("100.00"),
        )

    def _hold_lock_then_write_invoice(self, started: threading.Event, release: threading.Event):
        try:
            with transaction.atomic():
                Reservation.objects.select_for_update().get(pk=self.reservation.pk)
                started.set()
                self.assertTrue(release.wait(timeout=5))
                self._add_invoice()
        finally:
            connection.close()

    def _wait_until_blocked(self, thread: threading.Thread) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not thread.is_alive():
                return
            time.sleep(0.05)
            # The waiter is blocked on FOR UPDATE once it has started and the
            # holder still owns the reservation row.
            if thread.is_alive():
                time.sleep(0.15)
                return
        self.fail("timed out waiting for the lock waiter")

    def test_create_rejects_when_invoice_commits_under_reservation_lock(self):
        started = threading.Event()
        release = threading.Event()
        errors: list[BillingRecipientError] = []

        def create_after_invoice():
            try:
                started.wait(timeout=5)
                create_open_recipient(self.reservation, {"company_name": "Example GmbH"})
            except BillingRecipientError as exc:
                errors.append(exc)
            finally:
                connection.close()

        holder = threading.Thread(target=self._hold_lock_then_write_invoice, args=(started, release))
        waiter = threading.Thread(target=create_after_invoice)
        holder.start()
        self.assertTrue(started.wait(timeout=5))
        waiter.start()
        self._wait_until_blocked(waiter)
        release.set()
        holder.join(timeout=5)
        waiter.join(timeout=5)
        self.assertFalse(holder.is_alive())
        self.assertFalse(waiter.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].reason, RecipientRejectReason.INVOICE_ALREADY_PERSISTED)
        self.assertEqual(BillingRecipient.objects.count(), 0)
        self.assertEqual(Invoice.objects.count(), 1)

    def test_update_rejects_when_invoice_commits_under_reservation_lock(self):
        row = create_open_recipient(self.reservation, {"company_name": "Example GmbH"})
        started = threading.Event()
        release = threading.Event()
        errors: list[BillingRecipientError] = []

        def update_after_invoice():
            try:
                started.wait(timeout=5)
                update_open_recipient(row, {"city": "Hamburg"})
            except BillingRecipientError as exc:
                errors.append(exc)
            finally:
                connection.close()

        holder = threading.Thread(target=self._hold_lock_then_write_invoice, args=(started, release))
        waiter = threading.Thread(target=update_after_invoice)
        holder.start()
        self.assertTrue(started.wait(timeout=5))
        waiter.start()
        self._wait_until_blocked(waiter)
        release.set()
        holder.join(timeout=5)
        waiter.join(timeout=5)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].reason, RecipientRejectReason.INVOICE_ALREADY_PERSISTED)
        row.refresh_from_db()
        self.assertEqual(row.status, BillingRecipient.Status.REQUESTED)
        self.assertEqual(row.city, "")

    def test_apply_rejects_when_invoice_commits_under_reservation_lock(self):
        recipient = create_open_recipient(self.reservation, _ready_fields())
        started = threading.Event()
        release = threading.Event()
        errors: list[BillingRecipientError] = []

        def apply_after_invoice():
            try:
                started.wait(timeout=5)
                apply_recipient_to_new_invoice(
                    recipient=recipient,
                    invoice_create_kwargs={
                        "tenant": self.tenant,
                        "reservation": self.reservation,
                        "invoice_number": "2-ROOMS-1",
                        "sequence_number": 2,
                        "issued_at": datetime(2026, 9, 12, 11, 4, 0),
                        "payment_method": Invoice.PaymentMethod.CARD,
                        "subtotal": Decimal("88.50"),
                        "vat_amount": Decimal("11.50"),
                        "total": Decimal("100.00"),
                    },
                )
            except BillingRecipientError as exc:
                errors.append(exc)
            finally:
                connection.close()

        holder = threading.Thread(target=self._hold_lock_then_write_invoice, args=(started, release))
        waiter = threading.Thread(target=apply_after_invoice)
        holder.start()
        self.assertTrue(started.wait(timeout=5))
        waiter.start()
        self._wait_until_blocked(waiter)
        release.set()
        holder.join(timeout=5)
        waiter.join(timeout=5)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].reason, RecipientRejectReason.INVOICE_ALREADY_PERSISTED)
        recipient.refresh_from_db()
        self.assertEqual(recipient.status, BillingRecipient.Status.READY)
        self.assertIsNone(recipient.applied_invoice_id)
        self.assertEqual(Invoice.objects.count(), 1)

    def test_issue_returns_invoice_written_under_reservation_lock(self):
        started = threading.Event()
        release = threading.Event()
        results: list[Invoice] = []

        def issue_after_invoice():
            try:
                started.wait(timeout=5)
                results.append(issue_guest_invoice(self.reservation))
            finally:
                connection.close()

        holder = threading.Thread(target=self._hold_lock_then_write_invoice, args=(started, release))
        waiter = threading.Thread(target=issue_after_invoice)
        holder.start()
        self.assertTrue(started.wait(timeout=5))
        waiter.start()
        self._wait_until_blocked(waiter)
        release.set()
        holder.join(timeout=5)
        waiter.join(timeout=5)
        self.assertEqual(len(results), 1)
        self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(results[0].pk, Invoice.objects.get().pk)
