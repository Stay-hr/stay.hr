from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.billing.exceptions import InvoiceReplacementError
from apps.billing.models import Invoice, InvoiceReplacement, InvoiceReplacementRecipient
from apps.billing.services.invoice_replacement import ReplacementRejectReason
from apps.billing.services.invoice_replacement_delivery import (
    send_replacement_invoice_email,
    send_replacement_storno_email,
)
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant


class InvoiceReplacementDeliveryTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Replacement Delivery Tenant",
            slug="invoice-replacement-delivery",
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
            booker_name="DARIO PREZEC",
            booker_email="guest@example.com",
            amount=Decimal("100.00"),
        )
        self.actor = get_user_model().objects.create_user(
            username="delivery-admin",
            password="x",
            is_staff=True,
        )
        self.original = self._add_invoice(
            sequence_number=259,
            email_recipient="dario@example.com",
        )
        self.storno = self._add_invoice(sequence_number=260)
        self.replacement = self._add_invoice(sequence_number=261)

    def _add_invoice(self, *, sequence_number: int, **overrides) -> Invoice:
        values = {
            "tenant": self.tenant,
            "reservation": self.reservation,
            "invoice_number": f"{sequence_number}-ROOMS-1",
            "sequence_number": sequence_number,
            "issued_at": datetime(2026, 9, 12, 11, 3, 0),
            "buyer_name": "DARIO PREZEC",
            "payment_method": Invoice.PaymentMethod.BOOKING,
            "subtotal": Decimal("88.50"),
            "vat_amount": Decimal("11.50"),
            "total": Decimal("100.00"),
        }
        values.update(overrides)
        return Invoice.objects.create(**values)

    def _open_with_storno(self) -> InvoiceReplacement:
        return InvoiceReplacement.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            original_invoice=self.original,
            storno_invoice=self.storno,
            reason="wrong buyer",
            opened_by=self.actor,
            opened_at=timezone.now(),
        )

    def _completed_case(self) -> InvoiceReplacement:
        case = InvoiceReplacement.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            original_invoice=self.original,
            storno_invoice=self.storno,
            replacement_invoice=self.replacement,
            status=InvoiceReplacement.Status.COMPLETED,
            reason="wrong buyer",
            opened_by=self.actor,
            opened_at=timezone.now(),
            completed_by=self.actor,
            completed_at=timezone.now(),
        )
        InvoiceReplacementRecipient.objects.create(
            tenant=self.tenant,
            case=case,
            company_name="PRO AUTOMATIKA",
            email="domagoj.perjanec@pro-automatika.hr",
        )
        return case

    @patch("apps.billing.services.invoice_replacement_delivery.send_invoice_email_to")
    def test_storno_uses_original_email_recipient(self, mock_send):
        mock_send.return_value = {
            "status": "sent",
            "invoice_id": self.storno.pk,
            "recipient": "dario@example.com",
        }
        case = self._open_with_storno()
        result = send_replacement_storno_email(case=case)
        mock_send.assert_called_once_with(self.storno.pk, "dario@example.com")
        self.assertEqual(result["status"], "sent")

    def test_storno_without_invoice_is_rejected(self):
        case = InvoiceReplacement.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            original_invoice=self.original,
            reason="wrong buyer",
            opened_by=self.actor,
            opened_at=timezone.now(),
        )
        with self.assertRaises(InvoiceReplacementError) as ctx:
            send_replacement_storno_email(case=case)
        self.assertEqual(ctx.exception.reason, ReplacementRejectReason.STORNO_MISSING.value)

    def test_storno_without_original_email_skips(self):
        self.original.email_recipient = ""
        self.original.save(update_fields=["email_recipient", "updated_at"])
        case = self._open_with_storno()
        result = send_replacement_storno_email(case=case)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "no_recipient")

    @patch("apps.billing.services.invoice_replacement_delivery.send_invoice_email_to")
    def test_replacement_uses_frozen_recipient_email(self, mock_send):
        mock_send.return_value = {
            "status": "sent",
            "invoice_id": self.replacement.pk,
            "recipient": "domagoj.perjanec@pro-automatika.hr",
        }
        case = self._completed_case()
        result = send_replacement_invoice_email(case=case)
        mock_send.assert_called_once_with(
            self.replacement.pk,
            "domagoj.perjanec@pro-automatika.hr",
        )
        self.assertEqual(result["status"], "sent")

    def test_replacement_without_invoice_is_rejected(self):
        case = self._open_with_storno()
        with self.assertRaises(InvoiceReplacementError) as ctx:
            send_replacement_invoice_email(case=case)
        self.assertEqual(ctx.exception.reason, ReplacementRejectReason.REPLACEMENT_MISSING.value)
