import inspect
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from apps.billing.exceptions import BillingRecipientIssuanceDeferred
from apps.billing.models import Invoice, TenantFiscalSettings
from apps.billing.services.issue import issue_guest_invoice
from apps.billing.tests.helpers import make_guest
from apps.properties.models import Property
from apps.reservations.checkout import perform_reservation_checkout
from apps.reservations.models import EvisitorGuestStatus, Reservation
from apps.tenants.models import RECEPTION_DEVICE_SCOPES, ApiApplication, Tenant


class BillingRecipientIssuanceDeferredTests(TestCase):
    def test_code_is_billing_recipient_incomplete(self):
        self.assertEqual(
            BillingRecipientIssuanceDeferred.code,
            "billing_recipient_incomplete",
        )

    def test_issue_guest_invoice_wires_resolver_and_deferred(self):
        source = inspect.getsource(issue_guest_invoice)
        self.assertIn("BillingRecipientIssuanceDeferred", source)
        self.assertIn("resolve_billing_recipient_issuance", source)


class CheckoutDeferredInvoiceTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Deferred Checkout Tenant",
            slug="deferred-checkout",
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
    def test_deferred_issuance_lets_checkout_finish_without_invoice(
        self,
        mock_issue,
        mock_fiscalize,
        mock_email,
        mock_evisitor_checkout,
    ):
        mock_evisitor_checkout.return_value = []
        mock_issue.side_effect = BillingRecipientIssuanceDeferred()

        perform_reservation_checkout(self.reservation)

        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.status, Reservation.Status.CHECKED_OUT)
        self.assertFalse(Invoice.objects.filter(reservation=self.reservation).exists())
        mock_issue.assert_called_once()
        mock_fiscalize.assert_not_called()
        mock_email.assert_not_called()
        mock_evisitor_checkout.assert_called_once()


class ManualInvoicePostDeferredTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Deferred Invoice API",
            slug="deferred-invoice-api",
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
    def test_post_returns_409_and_does_not_issue(
        self,
        mock_issue,
        mock_fiscalize,
        mock_email,
    ):
        mock_issue.side_effect = BillingRecipientIssuanceDeferred()

        response = self.client.post(
            f"/api/v1/reception/reservations/{self.reservation.pk}/invoice/",
            {},
            format="json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertEqual(body["status"], "error")
        self.assertEqual(body["reason"], "billing_recipient_incomplete")
        self.assertFalse(Invoice.objects.filter(reservation=self.reservation).exists())
        mock_issue.assert_called_once()
        mock_fiscalize.assert_not_called()
        mock_email.assert_not_called()
