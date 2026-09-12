from datetime import date, datetime
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from apps.billing.models import BillingRecipient, Invoice
from apps.billing.services.billing_recipient_service import create_open_recipient
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import RECEPTION_DEVICE_SCOPES, ApiApplication, Tenant


class BillingRecipientApiTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Recipient API Tenant",
            slug="billing-recipient-api",
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
        self.app, self.raw_token = ApiApplication.create_with_token(
            tenant=self.tenant,
            name="Tablet",
            scopes=RECEPTION_DEVICE_SCOPES,
        )
        self.client = APIClient()
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.raw_token}"}

    def _url(self, reservation=None) -> str:
        reservation = reservation or self.reservation
        return f"/api/v1/reception/reservations/{reservation.pk}/billing-recipient/"

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
            "source": "staff",
        }

    def test_get_missing_open_recipient_is_404(self):
        response = self.client.get(self._url(), **self.auth)
        self.assertEqual(response.status_code, 404)

    def test_post_incomplete_is_requested(self):
        response = self.client.post(
            self._url(),
            {"company_name": "Example GmbH", "source": "staff"},
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body["status"], BillingRecipient.Status.REQUESTED)
        self.assertEqual(body["identity_confidence"], BillingRecipient.IdentityConfidence.UNVERIFIED)
        self.assertIsNone(body["ready_at"])
        self.assertIsNotNone(body["requested_at"])
        self.assertEqual(body["reservation_id"], self.reservation.pk)
        self.assertEqual(body["company_name"], "Example GmbH")
        self.assertNotIn("applied_invoice_id", body)

    def test_post_complete_is_ready(self):
        response = self.client.post(
            self._url(),
            self._ready_fields(),
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body["status"], BillingRecipient.Status.READY)
        self.assertEqual(body["tax_id_country"], "DE")
        self.assertEqual(body["country"], "DE")
        self.assertIsNotNone(body["ready_at"])
        self.assertEqual(
            body["identity_confidence"],
            BillingRecipient.IdentityConfidence.UNVERIFIED,
        )

        fetched = self.client.get(self._url(), **self.auth)
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json()["id"], body["id"])
        self.assertEqual(fetched.json()["status"], BillingRecipient.Status.READY)

    def test_post_rejects_status_and_identity_confidence(self):
        response = self.client.post(
            self._url(),
            {
                **self._ready_fields(),
                "status": "applied",
                "identity_confidence": "verified",
            },
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["reason"], "field_not_writable")
        self.assertFalse(BillingRecipient.objects.exists())

    def test_post_empty_is_rejected(self):
        response = self.client.post(self._url(), {}, format="json", **self.auth)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["reason"], "empty_request")

    def test_post_second_open_row_conflicts(self):
        create_open_recipient(self.reservation, {"company_name": "A"})
        response = self.client.post(
            self._url(),
            {"company_name": "B"},
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["reason"], "open_already_exists")

    def test_patch_promotes_and_demotes(self):
        created = self.client.post(
            self._url(),
            {"company_name": "Example GmbH"},
            format="json",
            **self.auth,
        )
        requested_at = created.json()["requested_at"]

        ready = self.client.patch(
            self._url(),
            self._ready_fields(),
            format="json",
            **self.auth,
        )
        self.assertEqual(ready.status_code, 200, ready.content)
        self.assertEqual(ready.json()["status"], BillingRecipient.Status.READY)
        self.assertEqual(ready.json()["requested_at"], requested_at)
        self.assertIsNotNone(ready.json()["ready_at"])

        demoted = self.client.patch(
            self._url(),
            {"email": ""},
            format="json",
            **self.auth,
        )
        self.assertEqual(demoted.status_code, 200)
        self.assertEqual(demoted.json()["status"], BillingRecipient.Status.REQUESTED)
        self.assertIsNone(demoted.json()["ready_at"])
        self.assertEqual(demoted.json()["requested_at"], requested_at)

    def test_patch_rejects_status(self):
        create_open_recipient(self.reservation, self._ready_fields())
        response = self.client.patch(
            self._url(),
            {"status": "applied"},
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["reason"], "field_not_writable")
        row = BillingRecipient.objects.get()
        self.assertEqual(row.status, BillingRecipient.Status.READY)

    def test_applied_row_is_not_exposed_or_editable(self):
        invoice = Invoice.objects.create(
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
        applied = BillingRecipient(
            tenant=self.tenant,
            reservation=self.reservation,
            status=BillingRecipient.Status.APPLIED,
            applied_invoice=invoice,
            **{
                **self._ready_fields(),
                "tax_id_country": "DE",
                "country": "DE",
            },
        )
        applied.save(allow_apply=True)

        self.assertEqual(self.client.get(self._url(), **self.auth).status_code, 404)
        patch = self.client.patch(
            self._url(),
            {"city": "Hamburg"},
            format="json",
            **self.auth,
        )
        self.assertEqual(patch.status_code, 404)
        applied.refresh_from_db()
        self.assertEqual(applied.city, "Berlin")
        self.assertEqual(applied.status, BillingRecipient.Status.APPLIED)

    def test_other_tenant_reservation_is_404(self):
        other_tenant = Tenant.objects.create(name="Other", slug="billing-recipient-api-other")
        other_property = Property.objects.create(
            tenant=other_tenant,
            name="O",
            slug="o",
        )
        other_reservation = Reservation.objects.create(
            tenant=other_tenant,
            property=other_property,
            check_in=date(2026, 9, 10),
            check_out=date(2026, 9, 12),
            status=Reservation.Status.CHECKED_OUT,
            booker_name="Other Guest",
            amount=Decimal("80.00"),
        )
        create_open_recipient(other_reservation, {"company_name": "Secret GmbH"})

        self.assertEqual(self.client.get(self._url(other_reservation), **self.auth).status_code, 404)
        self.assertEqual(
            self.client.post(
                self._url(other_reservation),
                {"company_name": "Leaked"},
                format="json",
                **self.auth,
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.patch(
                self._url(other_reservation),
                {"city": "Hamburg"},
                format="json",
                **self.auth,
            ).status_code,
            404,
        )
        self.assertEqual(
            BillingRecipient.objects.filter(reservation=other_reservation).count(),
            1,
        )

    def test_read_token_cannot_write(self):
        _app, raw = ApiApplication.create_with_token(
            tenant=self.tenant,
            name="Read tablet",
            scopes=["reception:read"],
        )
        auth = {"HTTP_AUTHORIZATION": f"Bearer {raw}"}
        self.assertEqual(self.client.get(self._url(), **auth).status_code, 404)
        self.assertEqual(
            self.client.post(
                self._url(),
                {"company_name": "Example GmbH"},
                format="json",
                **auth,
            ).status_code,
            403,
        )

    def test_does_not_write_reservation_buyer_snapshot(self):
        self.client.post(self._url(), self._ready_fields(), format="json", **self.auth)
        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.buyer_company_name, "")
        self.assertEqual(self.reservation.buyer_oib, "")
        self.assertEqual(self.reservation.buyer_address, "")
        self.assertEqual(Invoice.objects.count(), 0)
