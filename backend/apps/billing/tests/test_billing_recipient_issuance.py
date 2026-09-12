from datetime import date, datetime
from decimal import Decimal

from django.test import TestCase

from apps.billing.models import BillingRecipient, Invoice
from apps.billing.services.billing_recipient import BillingRecipientIssuanceDecision
from apps.billing.services.billing_recipient_issuance import (
    resolve_billing_recipient_issuance,
)
from apps.billing.services.billing_recipient_service import create_open_recipient
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant


class BillingRecipientIssuanceDecisionTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Recipient Issuance Tenant",
            slug="billing-recipient-issuance",
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

    def _add_invoice(self, reservation=None) -> Invoice:
        reservation = reservation or self.reservation
        return Invoice.objects.create(
            tenant=self.tenant,
            reservation=reservation,
            invoice_number="1-ROOMS-1",
            sequence_number=1,
            issued_at=datetime(2026, 9, 12, 11, 3, 0),
            buyer_name="Guest Guest",
            payment_method=Invoice.PaymentMethod.CARD,
            subtotal=Decimal("88.50"),
            vat_amount=Decimal("11.50"),
            total=Decimal("100.00"),
        )

    def test_no_recipient_is_guest(self):
        self.assertEqual(
            resolve_billing_recipient_issuance(self.reservation),
            BillingRecipientIssuanceDecision.GUEST,
        )
        self.assertEqual(Invoice.objects.count(), 0)

    def test_requested_is_hard_block(self):
        row = create_open_recipient(self.reservation, {"company_name": "Example GmbH"})
        self.assertEqual(
            resolve_billing_recipient_issuance(self.reservation),
            BillingRecipientIssuanceDecision.BLOCK_REQUESTED,
        )
        row.refresh_from_db()
        self.assertEqual(row.status, BillingRecipient.Status.REQUESTED)
        self.assertEqual(Invoice.objects.count(), 0)

    def test_ready_unverified_is_apply_ready(self):
        row = create_open_recipient(self.reservation, self._ready_fields())
        self.assertEqual(
            row.identity_confidence,
            BillingRecipient.IdentityConfidence.UNVERIFIED,
        )
        self.assertEqual(
            resolve_billing_recipient_issuance(self.reservation),
            BillingRecipientIssuanceDecision.APPLY_READY,
        )
        row.refresh_from_db()
        self.assertEqual(row.status, BillingRecipient.Status.READY)
        self.assertEqual(Invoice.objects.count(), 0)

    def test_existing_invoice_is_guest_without_apply(self):
        self._add_invoice()
        self.assertEqual(
            resolve_billing_recipient_issuance(self.reservation),
            BillingRecipientIssuanceDecision.GUEST,
        )

    def test_existing_invoice_ignores_open_requested(self):
        create_open_recipient(self.reservation, {"company_name": "Example GmbH"})
        self._add_invoice()
        self.assertEqual(
            resolve_billing_recipient_issuance(self.reservation),
            BillingRecipientIssuanceDecision.GUEST,
        )
        self.assertEqual(Invoice.objects.count(), 1)

    def test_existing_invoice_ignores_open_ready(self):
        create_open_recipient(self.reservation, self._ready_fields())
        self._add_invoice()
        self.assertEqual(
            resolve_billing_recipient_issuance(self.reservation),
            BillingRecipientIssuanceDecision.GUEST,
        )
        self.assertEqual(Invoice.objects.count(), 1)

    def test_other_reservation_ready_is_out_of_scope(self):
        other = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            check_in=date(2026, 9, 13),
            check_out=date(2026, 9, 14),
            status=Reservation.Status.CHECKED_OUT,
            booker_name="Other Guest",
            amount=Decimal("80.00"),
        )
        create_open_recipient(other, self._ready_fields())
        self.assertEqual(
            resolve_billing_recipient_issuance(self.reservation),
            BillingRecipientIssuanceDecision.GUEST,
        )
        self.assertEqual(
            resolve_billing_recipient_issuance(other),
            BillingRecipientIssuanceDecision.APPLY_READY,
        )
