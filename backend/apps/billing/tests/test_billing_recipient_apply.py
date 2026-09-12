from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.billing.exceptions import BillingRecipientError
from apps.billing.models import BillingRecipient, Invoice
from apps.billing.services.billing_recipient import RecipientRejectReason
from apps.billing.services.billing_recipient_apply import apply_recipient_to_new_invoice
from apps.billing.services.billing_recipient_service import create_open_recipient
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant


class BillingRecipientApplyTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Recipient Apply Tenant",
            slug="billing-recipient-apply",
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
            "tax_id_country": "DE",
            "country": "DE",
            "address": "Unter den Linden 1",
            "postal_code": "10115",
            "city": "Berlin",
            "email": "billing@example.com",
        }

    def _invoice_kwargs(self, reservation=None, **overrides) -> dict:
        fields = {
            "tenant": self.tenant,
            "reservation": reservation or self.reservation,
            "invoice_number": "1-ROOMS-1",
            "sequence_number": 1,
            "issued_at": datetime(2026, 9, 12, 11, 3, 0),
            "payment_method": Invoice.PaymentMethod.CARD,
            "subtotal": Decimal("88.50"),
            "vat_amount": Decimal("11.50"),
            "total": Decimal("100.00"),
        }
        fields.update(overrides)
        return fields

    def test_ready_recipient_creates_invoice_and_becomes_applied(self):
        recipient = create_open_recipient(self.reservation, self._ready_fields())
        before = timezone.now()
        invoice = apply_recipient_to_new_invoice(
            recipient=recipient,
            invoice_create_kwargs=self._invoice_kwargs(),
        )
        recipient.refresh_from_db()
        self.assertEqual(invoice.buyer_name, "Example GmbH")
        self.assertEqual(invoice.buyer_document_number, "DE123456789")
        self.assertEqual(invoice.buyer_address, "Unter den Linden 1, 10115 Berlin")
        self.assertEqual(invoice.buyer_country, "Njemačka")
        self.assertEqual(recipient.status, BillingRecipient.Status.APPLIED)
        self.assertEqual(recipient.applied_invoice_id, invoice.pk)
        self.assertIsNotNone(recipient.applied_at)
        self.assertGreaterEqual(recipient.applied_at, before)
        self.assertEqual(
            recipient.identity_confidence,
            BillingRecipient.IdentityConfidence.UNVERIFIED,
        )
        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.buyer_company_name, "")
        self.assertEqual(self.reservation.buyer_oib, "")
        self.assertEqual(self.reservation.buyer_address, "")

    def test_requested_is_rejected_without_invoice(self):
        recipient = create_open_recipient(self.reservation, {"company_name": "Example GmbH"})
        with self.assertRaises(BillingRecipientError) as ctx:
            apply_recipient_to_new_invoice(
                recipient=recipient,
                invoice_create_kwargs=self._invoice_kwargs(),
            )
        self.assertEqual(ctx.exception.reason, RecipientRejectReason.SKIP_TO_APPLIED)
        recipient.refresh_from_db()
        self.assertEqual(recipient.status, BillingRecipient.Status.REQUESTED)
        self.assertIsNone(recipient.applied_invoice_id)
        self.assertEqual(Invoice.objects.count(), 0)

    def test_stale_ready_that_is_now_applied_is_rejected(self):
        recipient = create_open_recipient(self.reservation, self._ready_fields())
        apply_recipient_to_new_invoice(
            recipient=recipient,
            invoice_create_kwargs=self._invoice_kwargs(),
        )
        stale = BillingRecipient(pk=recipient.pk, status=BillingRecipient.Status.READY)
        stale.reservation = self.reservation
        with self.assertRaises(BillingRecipientError) as ctx:
            apply_recipient_to_new_invoice(
                recipient=stale,
                invoice_create_kwargs=self._invoice_kwargs(
                    invoice_number="2-ROOMS-1",
                    sequence_number=2,
                ),
            )
        self.assertEqual(ctx.exception.reason, RecipientRejectReason.ALREADY_APPLIED)
        self.assertEqual(Invoice.objects.count(), 1)

    def test_other_reservation_is_rejected(self):
        other = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            check_in=date(2026, 9, 13),
            check_out=date(2026, 9, 14),
            status=Reservation.Status.CHECKED_OUT,
            booker_name="Other Guest",
            amount=Decimal("80.00"),
        )
        recipient = create_open_recipient(self.reservation, self._ready_fields())
        with self.assertRaises(BillingRecipientError) as ctx:
            apply_recipient_to_new_invoice(
                recipient=recipient,
                invoice_create_kwargs=self._invoice_kwargs(reservation=other),
            )
        self.assertEqual(ctx.exception.reason, RecipientRejectReason.RESERVATION_MISMATCH)
        recipient.refresh_from_db()
        self.assertEqual(recipient.status, BillingRecipient.Status.READY)
        self.assertEqual(Invoice.objects.count(), 0)

    def test_caller_buyer_fields_are_rejected(self):
        recipient = create_open_recipient(self.reservation, self._ready_fields())
        with self.assertRaises(BillingRecipientError) as ctx:
            apply_recipient_to_new_invoice(
                recipient=recipient,
                invoice_create_kwargs=self._invoice_kwargs(buyer_name="Forged Buyer"),
            )
        self.assertEqual(ctx.exception.reason, RecipientRejectReason.FORBIDDEN_BUYER_FIELDS)
        recipient.refresh_from_db()
        self.assertEqual(recipient.status, BillingRecipient.Status.READY)
        self.assertEqual(Invoice.objects.count(), 0)

    def test_apply_failure_rolls_back_created_invoice(self):
        recipient = create_open_recipient(self.reservation, self._ready_fields())
        original_save = BillingRecipient.save

        def fail_apply(self, *args, **kwargs):
            if kwargs.get("allow_apply"):
                raise RuntimeError("apply save failed")
            return original_save(self, *args, **kwargs)

        with patch.object(BillingRecipient, "save", fail_apply):
            with self.assertRaises(RuntimeError):
                apply_recipient_to_new_invoice(
                    recipient=recipient,
                    invoice_create_kwargs=self._invoice_kwargs(),
                )
        recipient.refresh_from_db()
        self.assertEqual(recipient.status, BillingRecipient.Status.READY)
        self.assertIsNone(recipient.applied_invoice_id)
        self.assertEqual(Invoice.objects.count(), 0)

    def test_second_apply_does_not_create_another_invoice(self):
        recipient = create_open_recipient(self.reservation, self._ready_fields())
        first = apply_recipient_to_new_invoice(
            recipient=recipient,
            invoice_create_kwargs=self._invoice_kwargs(),
        )
        with self.assertRaises(BillingRecipientError) as ctx:
            apply_recipient_to_new_invoice(
                recipient=recipient,
                invoice_create_kwargs=self._invoice_kwargs(
                    invoice_number="2-ROOMS-1",
                    sequence_number=2,
                ),
            )
        self.assertEqual(ctx.exception.reason, RecipientRejectReason.ALREADY_APPLIED)
        self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(Invoice.objects.get().pk, first.pk)
        recipient.refresh_from_db()
        self.assertEqual(recipient.applied_invoice_id, first.pk)
