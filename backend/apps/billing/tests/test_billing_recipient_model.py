from datetime import date, datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase

from apps.billing.models import BillingRecipient, Invoice
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant


class BillingRecipientModelTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Recipient Tenant",
            slug="billing-recipient-model",
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

    def _requested(self, **overrides) -> BillingRecipient:
        fields = dict(
            tenant=self.tenant,
            reservation=self.reservation,
            company_name="PRO AUTOMATIKA",
            source_excerpt="Molio bih R1 račun.",
            status=BillingRecipient.Status.REQUESTED,
        )
        fields.update(overrides)
        return BillingRecipient.objects.create(**fields)

    def _ready_fields(self) -> dict:
        return dict(
            company_name="PRO AUTOMATIKA",
            tax_id="87357644223",
            tax_id_country="HR",
            country="HR",
            address="Novo naselje 19E",
            postal_code="22214",
            city="Bilice",
            email="billing@example.com",
        )

    def test_requested_row_persists_without_applying(self):
        row = self._requested()
        self.assertEqual(row.status, BillingRecipient.Status.REQUESTED)
        self.assertIsNone(row.applied_invoice_id)
        self.assertEqual(row.identity_confidence, BillingRecipient.IdentityConfidence.UNVERIFIED)

    def test_empty_row_is_rejected(self):
        with self.assertRaises(ValidationError):
            BillingRecipient.objects.create(
                tenant=self.tenant,
                reservation=self.reservation,
            )

    def test_ready_requires_complete_fields(self):
        with self.assertRaises(ValidationError):
            BillingRecipient.objects.create(
                tenant=self.tenant,
                reservation=self.reservation,
                company_name="PRO AUTOMATIKA",
                status=BillingRecipient.Status.READY,
            )

    def test_complete_ready_row_is_allowed(self):
        row = BillingRecipient.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            status=BillingRecipient.Status.READY,
            **self._ready_fields(),
        )
        self.assertEqual(row.status, BillingRecipient.Status.READY)
        self.assertIsNone(row.applied_invoice_id)

    def test_ordinary_edit_cannot_mark_applied(self):
        row = BillingRecipient.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            status=BillingRecipient.Status.READY,
            **self._ready_fields(),
        )
        row.status = BillingRecipient.Status.APPLIED
        with self.assertRaises(ValidationError) as ctx:
            row.save()
        self.assertIn("APPLIED", str(ctx.exception))
        row.refresh_from_db()
        self.assertEqual(row.status, BillingRecipient.Status.READY)
        self.assertIsNone(row.applied_invoice_id)

    def test_create_as_applied_is_rejected(self):
        invoice = Invoice.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            invoice_number="1-ROOMS-1",
            sequence_number=1,
            issued_at=datetime(2026, 9, 12, 11, 3, 0),
            buyer_name="Guest Guest",
            payment_method=Invoice.PaymentMethod.BOOKING,
            subtotal=Decimal("88.50"),
            vat_amount=Decimal("11.50"),
            total=Decimal("100.00"),
        )
        with self.assertRaises(ValidationError):
            BillingRecipient.objects.create(
                tenant=self.tenant,
                reservation=self.reservation,
                status=BillingRecipient.Status.APPLIED,
                applied_invoice=invoice,
                **self._ready_fields(),
            )

    def test_one_open_row_per_reservation(self):
        self._requested()
        with self.assertRaises((ValidationError, IntegrityError)):
            BillingRecipient.objects.create(
                tenant=self.tenant,
                reservation=self.reservation,
                company_name="Other Co",
                status=BillingRecipient.Status.REQUESTED,
            )

    def test_db_rejects_applied_without_invoice(self):
        row = self._requested()
        with self.assertRaises(IntegrityError):
            BillingRecipient.objects.filter(pk=row.pk).update(
                status=BillingRecipient.Status.APPLIED,
            )

    def test_tenant_must_match_reservation(self):
        other = Tenant.objects.create(name="Other", slug="billing-recipient-other")
        row = BillingRecipient(
            tenant=other,
            reservation=self.reservation,
            company_name="PRO AUTOMATIKA",
        )
        with self.assertRaises(ValidationError):
            row.save()

    def test_requested_at_is_immutable(self):
        row = self._requested()
        original = row.requested_at
        row.requested_at = datetime(2020, 1, 1)
        with self.assertRaises(ValidationError):
            row.save()
        row.refresh_from_db()
        self.assertEqual(row.requested_at, original)
