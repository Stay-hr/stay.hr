from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.billing.models import Invoice, InvoiceReplacement, InvoiceReplacementRecipient
from apps.billing.services.invoice_replacement_service import (
    open_replacement_case,
    update_replacement_recipient,
)
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import RECEPTION_DEVICE_SCOPES, ApiApplication, Tenant


def _ready_fields() -> dict:
    return {
        "company_name": "PRO AUTOMATIKA",
        "tax_id": "87357644223",
        "tax_id_country": "HR",
        "country": "HR",
        "address": "Novo naselje 19E",
        "postal_code": "22214",
        "city": "Bilice",
        "email": "domagoj.perjanec@pro-automatika.hr",
        "source": "staff",
        "source_excerpt": "R1 request from booking message",
    }


class InvoiceReplacementApiTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Replacement Admin API",
            slug="invoice-replacement-api",
        )
        self.other_tenant = Tenant.objects.create(
            name="Other Tenant",
            slug="invoice-replacement-api-other",
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
            username="replacement-api-admin",
            password="x",
            is_staff=True,
        )
        self.original = Invoice.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            invoice_number="259-ROOMS-1",
            sequence_number=259,
            issued_at=datetime(2026, 5, 27, 7, 21, 0),
            buyer_name="DARIO PREZEC",
            payment_method=Invoice.PaymentMethod.BOOKING,
            subtotal=Decimal("88.50"),
            vat_amount=Decimal("11.50"),
            total=Decimal("100.00"),
            email_recipient="dario@example.com",
        )
        self.client = APIClient()
        _, self.admin_write = ApiApplication.create_with_token(
            tenant=self.tenant,
            name="Admin write",
            scopes=["admin:read", "admin:write"],
        )
        _, self.admin_read = ApiApplication.create_with_token(
            tenant=self.tenant,
            name="Admin read",
            scopes=["admin:read"],
        )
        _, self.reception = ApiApplication.create_with_token(
            tenant=self.tenant,
            name="Tablet",
            scopes=RECEPTION_DEVICE_SCOPES,
        )
        _, self.other_admin = ApiApplication.create_with_token(
            tenant=self.other_tenant,
            name="Other admin",
            scopes=["admin:read", "admin:write"],
        )

    def _auth(self, token: str) -> dict:
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def _list_url(self) -> str:
        return "/api/v1/admin/invoice-replacements/"

    def _detail_url(self, case_id: int) -> str:
        return f"/api/v1/admin/invoice-replacements/{case_id}/"

    def test_reception_token_is_forbidden(self):
        response = self.client.get(self._list_url(), **self._auth(self.reception))
        self.assertEqual(response.status_code, 403)

    def test_admin_token_is_denied_on_reception_invoice(self):
        response = self.client.get(
            f"/api/v1/reception/reservations/{self.reservation.pk}/invoice/",
            **self._auth(self.admin_write),
        )
        self.assertEqual(response.status_code, 403)

    def test_admin_read_can_list_but_cannot_open(self):
        list_response = self.client.get(self._list_url(), **self._auth(self.admin_read))
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(list_response.json(), [])

        open_response = self.client.post(
            self._list_url(),
            {
                "original_invoice_id": self.original.pk,
                "reason": "Wrong buyer",
                "original_issuer_oib": "12345678901",
                "original_issuer_oib_source": "259-ROOMS-1 PDF header",
            },
            format="json",
            **self._auth(self.admin_read),
        )
        self.assertEqual(open_response.status_code, 403)

    def test_open_update_verify_cancel_and_idempotent_open(self):
        payload = {
            "original_invoice_id": self.original.pk,
            "reason": "Wrong buyer on issued invoice",
            "original_issuer_oib": "12345678901",
            "original_issuer_oib_source": "259-ROOMS-1 PDF header",
        }
        created = self.client.post(
            self._list_url(),
            payload,
            format="json",
            **self._auth(self.admin_write),
        )
        self.assertEqual(created.status_code, 201, created.content)
        body = created.json()
        case_id = body["id"]
        self.assertEqual(body["status"], InvoiceReplacement.Status.OPEN)
        self.assertEqual(body["original_invoice"]["invoice_number"], "259-ROOMS-1")
        self.assertEqual(body["opened_by"], "admin-api.invoice-replacement-api")
        self.assertIsNone(body["storno_invoice"])

        again = self.client.post(
            self._list_url(),
            payload,
            format="json",
            **self._auth(self.admin_write),
        )
        self.assertEqual(again.status_code, 201)
        self.assertEqual(again.json()["id"], case_id)

        forbidden = self.client.patch(
            f"{self._detail_url(case_id)}recipient/",
            {"status": "completed", "company_name": "X"},
            format="json",
            **self._auth(self.admin_write),
        )
        self.assertEqual(forbidden.status_code, 400)
        self.assertEqual(forbidden.json()["reason"], "field_not_writable")

        patched = self.client.patch(
            f"{self._detail_url(case_id)}recipient/",
            _ready_fields(),
            format="json",
            **self._auth(self.admin_write),
        )
        self.assertEqual(patched.status_code, 200, patched.content)
        self.assertEqual(patched.json()["recipient"]["company_name"], "PRO AUTOMATIKA")
        self.assertEqual(
            patched.json()["recipient"]["identity_confidence"],
            InvoiceReplacementRecipient.IdentityConfidence.UNVERIFIED,
        )

        verified = self.client.post(
            f"{self._detail_url(case_id)}verify/",
            {},
            format="json",
            **self._auth(self.admin_write),
        )
        self.assertEqual(verified.status_code, 200, verified.content)
        self.assertEqual(
            verified.json()["recipient"]["identity_confidence"],
            InvoiceReplacementRecipient.IdentityConfidence.VERIFIED,
        )

        cancelled = self.client.post(
            f"{self._detail_url(case_id)}cancel/",
            {"cancel_reason": "opened by mistake"},
            format="json",
            **self._auth(self.admin_write),
        )
        self.assertEqual(cancelled.status_code, 200, cancelled.content)
        self.assertEqual(cancelled.json()["status"], InvoiceReplacement.Status.CANCELLED)

    def test_other_tenant_cannot_see_case(self):
        case = open_replacement_case(
            original=self.original,
            actor=self.actor,
            reason="Wrong buyer",
            original_issuer_oib="12345678901",
            original_issuer_oib_source="259-ROOMS-1 PDF header",
        )
        response = self.client.get(
            self._detail_url(case.pk),
            **self._auth(self.other_admin),
        )
        self.assertEqual(response.status_code, 404)

    @patch("apps.api.admin_invoice_replacement_views.issue_replacement_storno")
    def test_storno_endpoint_returns_case(self, mock_storno):
        case = open_replacement_case(
            original=self.original,
            actor=self.actor,
            reason="Wrong buyer",
            original_issuer_oib="12345678901",
            original_issuer_oib_source="259-ROOMS-1 PDF header",
        )
        update_replacement_recipient(case=case, fields=_ready_fields())
        storno = Invoice.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            invoice_number="260-ROOMS-1",
            sequence_number=260,
            issued_at=datetime(2026, 9, 13, 8, 0, 0),
            buyer_name="DARIO PREZEC",
            payment_method=Invoice.PaymentMethod.BOOKING,
            subtotal=Decimal("-88.50"),
            vat_amount=Decimal("-11.50"),
            total=Decimal("-100.00"),
        )
        case.storno_invoice = storno
        case.save()
        mock_storno.return_value = storno

        response = self.client.post(
            f"{self._detail_url(case.pk)}storno/",
            {},
            format="json",
            **self._auth(self.admin_write),
        )
        self.assertEqual(response.status_code, 200, response.content)
        mock_storno.assert_called_once()
        self.assertEqual(response.json()["storno_invoice"]["invoice_number"], "260-ROOMS-1")

    @patch("apps.api.admin_invoice_replacement_views.send_replacement_storno_email")
    def test_send_storno_uses_delivery_command(self, mock_send):
        case = open_replacement_case(
            original=self.original,
            actor=self.actor,
            reason="Wrong buyer",
            original_issuer_oib="12345678901",
            original_issuer_oib_source="259-ROOMS-1 PDF header",
        )
        storno = Invoice.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            invoice_number="260-ROOMS-1",
            sequence_number=260,
            issued_at=datetime(2026, 9, 13, 8, 0, 0),
            buyer_name="DARIO PREZEC",
            payment_method=Invoice.PaymentMethod.BOOKING,
            subtotal=Decimal("-88.50"),
            vat_amount=Decimal("-11.50"),
            total=Decimal("-100.00"),
        )
        case.storno_invoice = storno
        case.save()
        mock_send.return_value = {
            "status": "sent",
            "recipient": "dario@example.com",
            "invoice_id": storno.pk,
        }
        response = self.client.post(
            f"{self._detail_url(case.pk)}send-storno/",
            {},
            format="json",
            **self._auth(self.admin_write),
        )
        self.assertEqual(response.status_code, 200, response.content)
        mock_send.assert_called_once()
        self.assertEqual(response.json()["recipient"], "dario@example.com")

    @patch("apps.api.admin_invoice_replacement_views.send_replacement_invoice_email")
    def test_send_replacement_uses_delivery_command(self, mock_send):
        case = open_replacement_case(
            original=self.original,
            actor=self.actor,
            reason="Wrong buyer",
            original_issuer_oib="12345678901",
            original_issuer_oib_source="259-ROOMS-1 PDF header",
        )
        storno = Invoice.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            invoice_number="260-ROOMS-1",
            sequence_number=260,
            issued_at=datetime(2026, 9, 13, 8, 0, 0),
            buyer_name="DARIO PREZEC",
            payment_method=Invoice.PaymentMethod.BOOKING,
            subtotal=Decimal("-88.50"),
            vat_amount=Decimal("-11.50"),
            total=Decimal("-100.00"),
        )
        replacement = Invoice.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            invoice_number="261-ROOMS-1",
            sequence_number=261,
            issued_at=datetime(2026, 9, 13, 8, 5, 0),
            buyer_name="PRO AUTOMATIKA",
            payment_method=Invoice.PaymentMethod.BOOKING,
            subtotal=Decimal("88.50"),
            vat_amount=Decimal("11.50"),
            total=Decimal("100.00"),
        )
        case.storno_invoice = storno
        case.replacement_invoice = replacement
        case.status = InvoiceReplacement.Status.COMPLETED
        case.completed_by = self.actor
        case.completed_at = timezone.now()
        case.save()
        mock_send.return_value = {
            "status": "sent",
            "recipient": "domagoj.perjanec@pro-automatika.hr",
            "invoice_id": replacement.pk,
        }
        response = self.client.post(
            f"{self._detail_url(case.pk)}send-replacement/",
            {},
            format="json",
            **self._auth(self.admin_write),
        )
        self.assertEqual(response.status_code, 200, response.content)
        mock_send.assert_called_once()
        self.assertEqual(
            response.json()["recipient"],
            "domagoj.perjanec@pro-automatika.hr",
        )

    def test_send_storno_without_storno_is_conflict(self):
        case = open_replacement_case(
            original=self.original,
            actor=self.actor,
            reason="Wrong buyer",
            original_issuer_oib="12345678901",
            original_issuer_oib_source="259-ROOMS-1 PDF header",
        )
        response = self.client.post(
            f"{self._detail_url(case.pk)}send-storno/",
            {},
            format="json",
            **self._auth(self.admin_write),
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["reason"], "storno_missing")
