from datetime import date, datetime
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.billing.exceptions import BillingRecipientError
from apps.billing.models import BillingRecipient, Invoice
from apps.billing.services.billing_recipient import RecipientRejectReason
from apps.billing.services.billing_recipient_service import (
    create_open_recipient,
    update_open_recipient,
)
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant


class BillingRecipientServiceTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Recipient Service Tenant",
            slug="billing-recipient-service",
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

    def _ready_fields(self) -> dict:
        return {
            "company_name": "Example GmbH",
            "tax_id": "DE123456789",
            "tax_id_country": "de",
            "country": "de",
            "address": "Unter den Linden 1",
            "postal_code": "10115",
            "city": "Berlin",
            "email": "billing@example.com",
        }

    def test_create_incomplete_is_requested(self):
        row = create_open_recipient(
            self.reservation,
            {"company_name": "Example GmbH"},
            source="staff",
            source_excerpt="R1 please",
        )
        self.assertEqual(row.status, BillingRecipient.Status.REQUESTED)
        self.assertIsNone(row.ready_at)
        self.assertIsNone(row.applied_invoice_id)
        self.assertEqual(
            row.identity_confidence,
            BillingRecipient.IdentityConfidence.UNVERIFIED,
        )
        self.assertEqual(row.tenant_id, self.tenant.id)

    def test_create_complete_is_ready(self):
        before = timezone.now()
        row = create_open_recipient(self.reservation, self._ready_fields())
        self.assertEqual(row.status, BillingRecipient.Status.READY)
        self.assertIsNotNone(row.ready_at)
        self.assertGreaterEqual(row.ready_at, before)
        self.assertEqual(row.tax_id_country, "DE")
        self.assertEqual(row.country, "DE")
        self.assertEqual(
            row.identity_confidence,
            BillingRecipient.IdentityConfidence.UNVERIFIED,
        )

    def test_create_empty_is_rejected(self):
        with self.assertRaises(BillingRecipientError) as ctx:
            create_open_recipient(self.reservation, {})
        self.assertEqual(ctx.exception.reason, RecipientRejectReason.EMPTY_REQUEST)

    def test_create_rejects_second_open_row(self):
        create_open_recipient(self.reservation, {"company_name": "A"})
        with self.assertRaises(BillingRecipientError) as ctx:
            create_open_recipient(self.reservation, {"company_name": "B"})
        self.assertEqual(ctx.exception.reason, RecipientRejectReason.OPEN_ALREADY_EXISTS)

    def test_create_ignores_applied_status_in_fields(self):
        row = create_open_recipient(
            self.reservation,
            {**self._ready_fields(), "status": "applied", "identity_confidence": "verified"},
        )
        self.assertEqual(row.status, BillingRecipient.Status.READY)
        self.assertEqual(
            row.identity_confidence,
            BillingRecipient.IdentityConfidence.UNVERIFIED,
        )
        self.assertIsNone(row.applied_invoice_id)

    def test_update_promotes_and_demotes(self):
        row = create_open_recipient(self.reservation, {"company_name": "Example GmbH"})
        requested_at = row.requested_at
        row = update_open_recipient(row, self._ready_fields())
        self.assertEqual(row.status, BillingRecipient.Status.READY)
        self.assertIsNotNone(row.ready_at)
        self.assertEqual(row.requested_at, requested_at)

        row = update_open_recipient(row, {"email": ""})
        self.assertEqual(row.status, BillingRecipient.Status.REQUESTED)
        self.assertIsNone(row.ready_at)
        self.assertEqual(row.requested_at, requested_at)
        self.assertEqual(row.company_name, "Example GmbH")

    def test_update_ready_keeps_ready_at_when_still_ready(self):
        row = create_open_recipient(self.reservation, self._ready_fields())
        ready_at = row.ready_at
        requested_at = row.requested_at
        row = update_open_recipient(row, {"city": "Hamburg"})
        self.assertEqual(row.status, BillingRecipient.Status.READY)
        self.assertEqual(row.ready_at, ready_at)
        self.assertEqual(row.requested_at, requested_at)
        self.assertEqual(row.city, "Hamburg")

    def test_invalid_country_is_rejected(self):
        with self.assertRaises(BillingRecipientError) as ctx:
            create_open_recipient(
                self.reservation,
                {"company_name": "Example GmbH", "country": "Hrvatska"},
            )
        self.assertEqual(ctx.exception.reason, RecipientRejectReason.INVALID_COUNTRY)

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

    def test_create_rejects_existing_invoice(self):
        self._add_invoice()
        with self.assertRaises(BillingRecipientError) as ctx:
            create_open_recipient(self.reservation, {"company_name": "Example GmbH"})
        self.assertEqual(
            ctx.exception.reason,
            RecipientRejectReason.INVOICE_ALREADY_PERSISTED,
        )
        self.assertEqual(BillingRecipient.objects.count(), 0)

    def test_create_rereads_invoice_after_reservation_lock(self):
        self.assertFalse(hasattr(self.reservation, "invoice"))
        self._add_invoice()
        with self.assertRaises(BillingRecipientError) as ctx:
            create_open_recipient(self.reservation, {"company_name": "Example GmbH"})
        self.assertEqual(
            ctx.exception.reason,
            RecipientRejectReason.INVOICE_ALREADY_PERSISTED,
        )
        self.assertEqual(BillingRecipient.objects.count(), 0)

    def test_update_rejects_existing_invoice(self):
        row = create_open_recipient(self.reservation, {"company_name": "Example GmbH"})
        self._add_invoice()
        with self.assertRaises(BillingRecipientError) as ctx:
            update_open_recipient(row, {"city": "Hamburg"})
        self.assertEqual(
            ctx.exception.reason,
            RecipientRejectReason.INVOICE_ALREADY_PERSISTED,
        )
        row.refresh_from_db()
        self.assertEqual(row.status, BillingRecipient.Status.REQUESTED)
        self.assertEqual(row.city, "")

    def test_update_applied_is_rejected(self):
        invoice = self._add_invoice()
        applied = BillingRecipient(
            tenant=self.tenant,
            reservation=self.reservation,
            status=BillingRecipient.Status.APPLIED,
            applied_invoice=invoice,
            **self._ready_fields(),
        )
        applied.save(allow_apply=True)
        with self.assertRaises(BillingRecipientError) as ctx:
            update_open_recipient(applied, {"city": "Hamburg"})
        self.assertEqual(
            ctx.exception.reason,
            RecipientRejectReason.INVOICE_ALREADY_PERSISTED,
        )
        applied.refresh_from_db()
        self.assertEqual(applied.city, "Berlin")

    def test_update_cannot_produce_applied(self):
        row = create_open_recipient(self.reservation, self._ready_fields())
        row = update_open_recipient(
            row,
            {"status": "applied", "applied_invoice_id": 1},
        )
        self.assertEqual(row.status, BillingRecipient.Status.READY)
        self.assertIsNone(row.applied_invoice_id)

    def test_does_not_write_reservation_buyer_snapshot(self):
        create_open_recipient(self.reservation, self._ready_fields())
        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.buyer_company_name, "")
        self.assertEqual(self.reservation.buyer_oib, "")
        self.assertEqual(self.reservation.buyer_address, "")
