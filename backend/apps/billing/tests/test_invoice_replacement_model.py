from datetime import date, datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from apps.billing.models import Invoice, InvoiceReplacement, InvoiceReplacementRecipient
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant


class InvoiceReplacementModelTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Replacement Tenant",
            slug="invoice-replacement-model",
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
            username="replacement-admin",
            password="x",
            is_staff=True,
        )
        self.original = self._add_invoice(sequence_number=1)

    def _add_invoice(self, *, sequence_number: int) -> Invoice:
        return Invoice.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            invoice_number=f"{sequence_number}-ROOMS-1",
            sequence_number=sequence_number,
            issued_at=datetime(2026, 9, 12, 11, 3, 0),
            buyer_name="DARIO PREZEC",
            payment_method=Invoice.PaymentMethod.BOOKING,
            subtotal=Decimal("88.50"),
            vat_amount=Decimal("11.50"),
            total=Decimal("100.00"),
        )

    def _open_case(self, **overrides) -> InvoiceReplacement:
        fields = dict(
            tenant=self.tenant,
            reservation=self.reservation,
            original_invoice=self.original,
            reason="Wrong buyer on issued invoice",
            opened_by=self.actor,
            opened_at=timezone.now(),
        )
        fields.update(overrides)
        return InvoiceReplacement.objects.create(**fields)

    def test_open_case_persists_without_invoices(self):
        case = self._open_case()
        self.assertEqual(case.status, InvoiceReplacement.Status.OPEN)
        self.assertIsNone(case.storno_invoice_id)
        self.assertIsNone(case.replacement_invoice_id)

    def test_one_open_case_per_reservation(self):
        self._open_case()
        with self.assertRaises((ValidationError, IntegrityError)):
            InvoiceReplacement.objects.create(
                tenant=self.tenant,
                reservation=self.reservation,
                original_invoice=self.original,
                reason="second",
                opened_by=self.actor,
                opened_at=timezone.now(),
            )

    def test_completed_requires_both_links_and_audit(self):
        case = self._open_case()
        case.status = InvoiceReplacement.Status.COMPLETED
        case.completed_by = self.actor
        case.completed_at = timezone.now()
        with self.assertRaises((ValidationError, IntegrityError)):
            case.save()

    def test_cancelled_requires_reason_and_forbids_storno(self):
        case = self._open_case()
        storno = self._add_invoice(sequence_number=2)
        case.status = InvoiceReplacement.Status.CANCELLED
        case.cancelled_by = self.actor
        case.cancelled_at = timezone.now()
        case.cancel_reason = "opened by mistake"
        case.storno_invoice = storno
        with self.assertRaises((ValidationError, IntegrityError)):
            case.save()

    def test_cancelled_happy_path(self):
        case = self._open_case()
        case.status = InvoiceReplacement.Status.CANCELLED
        case.cancelled_by = self.actor
        case.cancelled_at = timezone.now()
        case.cancel_reason = "opened by mistake"
        case.save()
        case.refresh_from_db()
        self.assertEqual(case.status, InvoiceReplacement.Status.CANCELLED)

    def test_storno_cannot_be_original(self):
        case = self._open_case()
        case.storno_invoice = self.original
        with self.assertRaises((ValidationError, IntegrityError)):
            case.save()

    def test_opened_audit_is_immutable(self):
        case = self._open_case()
        original_at = case.opened_at
        case.opened_at = datetime(2020, 1, 1)
        with self.assertRaises(ValidationError):
            case.save()
        case.refresh_from_db()
        self.assertEqual(case.opened_at, original_at)

    def test_issuer_oib_evidence_is_write_once(self):
        case = self._open_case(
            original_issuer_oib="12345678901",
            original_issuer_oib_source="259-ROOMS-1 PDF",
            original_issuer_oib_recorded_by=self.actor,
            original_issuer_oib_recorded_at=timezone.now(),
        )
        case.original_issuer_oib = "99999999999"
        with self.assertRaises(ValidationError):
            case.save()

    def test_recipient_provenance_is_write_once(self):
        case = self._open_case()
        recipient = InvoiceReplacementRecipient.objects.create(
            tenant=self.tenant,
            case=case,
            company_name="PRO AUTOMATIKA",
            source=InvoiceReplacementRecipient.Source.STAFF,
            source_excerpt="R1 request",
        )
        recipient.source_excerpt = "changed"
        with self.assertRaises(ValidationError):
            recipient.save()

    def test_verified_requires_audit_pair(self):
        case = self._open_case()
        with self.assertRaises((ValidationError, IntegrityError)):
            InvoiceReplacementRecipient.objects.create(
                tenant=self.tenant,
                case=case,
                identity_confidence=InvoiceReplacementRecipient.IdentityConfidence.VERIFIED,
                company_name="PRO AUTOMATIKA",
                tax_id="87357644223",
                tax_id_country="HR",
                country="HR",
                address="Novo naselje 19E",
                postal_code="22214",
                city="Bilice",
                email="billing@example.com",
            )

    def test_original_must_belong_to_same_reservation(self):
        other = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            check_in=date(2026, 9, 10),
            check_out=date(2026, 9, 12),
            status=Reservation.Status.CHECKED_OUT,
            booker_name="Other",
            amount=Decimal("80.00"),
        )
        foreign = Invoice.objects.create(
            tenant=self.tenant,
            reservation=other,
            invoice_number="9-ROOMS-1",
            sequence_number=9,
            issued_at=datetime(2026, 9, 12, 11, 3, 0),
            buyer_name="Other",
            payment_method=Invoice.PaymentMethod.BOOKING,
            subtotal=Decimal("10.00"),
            vat_amount=Decimal("0.00"),
            total=Decimal("10.00"),
        )
        with self.assertRaises(ValidationError):
            InvoiceReplacement.objects.create(
                tenant=self.tenant,
                reservation=self.reservation,
                original_invoice=foreign,
                reason="wrong",
                opened_by=self.actor,
                opened_at=timezone.now(),
            )
