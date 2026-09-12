import inspect
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.billing.exceptions import ReplacementInProgress
from apps.billing.models import Invoice, InvoiceReplacement, TenantFiscalSettings
from apps.billing.services.issue import issue_guest_invoice
from apps.billing.tests.helpers import make_guest
from apps.properties.models import Property
from apps.reservations.checkout import perform_reservation_checkout
from apps.reservations.models import EvisitorGuestStatus, Reservation
from apps.tenants.models import RECEPTION_DEVICE_SCOPES, ApiApplication, Tenant


class ReplacementInProgressWiringTests(TestCase):
    def test_issue_checks_gap_before_existing_or_sequence(self):
        source = inspect.getsource(issue_guest_invoice)
        self.assertIn("has_open_post_storno_gap", source)
        self.assertIn("ReplacementInProgress", source)
        self.assertIn("resolve_effective_invoice", source)
        self.assertLess(
            source.index("has_open_post_storno_gap"),
            source.index("resolve_effective_invoice(reservation)"),
        )
        self.assertLess(
            source.index("ReplacementInProgress"),
            source.index("_next_invoice_number"),
        )


class ReplacementGapIssueTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Replacement Gap Tenant",
            slug="replacement-gap-issue",
        )
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="P",
            slug="p",
        )
        self.settings = TenantFiscalSettings.objects.create(
            tenant=self.tenant,
            is_vat_registered=True,
            issuer_oib="12345678901",
            issuer_name="Issuer",
            business_premise_code="PP1",
            payment_device_code="1",
            invoice_sequence=2,
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
        actor = get_user_model().objects.create_user(
            username="gap-admin",
            password="x",
            is_staff=True,
        )
        original = Invoice.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            invoice_number="1-PP1-1",
            sequence_number=1,
            issued_at=datetime(2026, 9, 12, 11, 3, 0),
            buyer_name="Guest Guest",
            payment_method=Invoice.PaymentMethod.BOOKING,
            subtotal=Decimal("88.50"),
            vat_amount=Decimal("11.50"),
            total=Decimal("100.00"),
        )
        storno = Invoice.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            invoice_number="2-PP1-1",
            sequence_number=2,
            issued_at=datetime(2026, 9, 12, 11, 4, 0),
            buyer_name="Guest Guest",
            payment_method=Invoice.PaymentMethod.BOOKING,
            subtotal=Decimal("-88.50"),
            vat_amount=Decimal("-11.50"),
            total=Decimal("-100.00"),
        )
        InvoiceReplacement.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            original_invoice=original,
            storno_invoice=storno,
            reason="wrong buyer",
            opened_by=actor,
            opened_at=timezone.now(),
        )

    def test_issue_blocks_gap_without_consuming_sequence(self):
        with self.assertRaises(ReplacementInProgress):
            issue_guest_invoice(self.reservation)
        self.settings.refresh_from_db()
        self.assertEqual(self.settings.invoice_sequence, 2)
        self.assertEqual(Invoice.objects.filter(reservation=self.reservation).count(), 2)


class CheckoutReplacementGapTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Replacement Gap Checkout",
            slug="replacement-gap-checkout",
        )
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="P",
            slug="p",
        )
        TenantFiscalSettings.objects.create(
            tenant=self.tenant,
            is_vat_registered=True,
            issuer_oib="12345678901",
            issuer_name="Issuer",
            business_premise_code="PP1",
            payment_device_code="1",
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            check_in=date(2026, 9, 10),
            check_out=date(2026, 9, 12),
            status=Reservation.Status.CHECKED_IN,
            booker_name="Guest Guest",
            booker_email="guest@example.com",
            amount=Decimal("100.00"),
        )
        make_guest(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="Guest",
            last_name="Guest",
            is_primary=True,
            evisitor_status=EvisitorGuestStatus.SENT,
        )

    @patch("apps.reservations.checkout.checkout_reservation_guests_in_evisitor")
    @patch("apps.billing.tasks.send_invoice_email_task.delay")
    @patch("apps.billing.tasks.fiscalize_invoice.delay")
    @patch("apps.billing.services.issue.issue_guest_invoice")
    def test_gap_lets_checkout_finish_without_new_invoice(
        self,
        mock_issue,
        mock_fiscalize,
        mock_email,
        mock_evisitor_checkout,
    ):
        mock_evisitor_checkout.return_value = []
        mock_issue.side_effect = ReplacementInProgress()

        perform_reservation_checkout(self.reservation)

        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.status, Reservation.Status.CHECKED_OUT)
        mock_issue.assert_called_once()
        mock_fiscalize.assert_not_called()
        mock_email.assert_not_called()


class ManualInvoicePostGapTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Replacement Gap API",
            slug="replacement-gap-api",
        )
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="P",
            slug="p",
        )
        TenantFiscalSettings.objects.create(
            tenant=self.tenant,
            is_vat_registered=True,
            issuer_oib="12345678901",
            issuer_name="Issuer",
            business_premise_code="PP1",
            payment_device_code="1",
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
        _app, raw_token = ApiApplication.create_with_token(
            tenant=self.tenant,
            name="Tablet",
            scopes=RECEPTION_DEVICE_SCOPES,
        )
        self.client = APIClient()
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {raw_token}"}

    @patch("apps.billing.tasks.send_invoice_email_task.delay")
    @patch("apps.billing.tasks.fiscalize_invoice.delay")
    @patch("apps.billing.services.issue.issue_guest_invoice")
    def test_post_returns_409_replacement_in_progress(
        self,
        mock_issue,
        mock_fiscalize,
        mock_email,
    ):
        mock_issue.side_effect = ReplacementInProgress()

        response = self.client.post(
            f"/api/v1/reception/reservations/{self.reservation.pk}/invoice/",
            {},
            format="json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertEqual(body["status"], "error")
        self.assertEqual(body["reason"], "replacement_in_progress")
        mock_issue.assert_called_once()
        mock_fiscalize.assert_not_called()
        mock_email.assert_not_called()
