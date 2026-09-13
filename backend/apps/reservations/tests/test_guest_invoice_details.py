from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from apps.billing.models import BillingRecipient, Invoice
from apps.billing.services.billing_recipient import RecipientSource
from apps.billing.services.billing_recipient_service import create_open_recipient
from apps.communications.invoice_email import resolve_invoice_recipient
from apps.properties.models import Property
from apps.reservations.guest_invoice_details_access import (
    ensure_active_invoice_details_access,
    evaluate_invoice_details_access,
    revoke_invoice_details_access,
)
from apps.reservations.models import Guest, GuestInvoiceDetailsAccessCreatedFrom, Reservation
from apps.tenants.models import Tenant


class GuestInvoiceDetailsAccessTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Invd Tenant", slug="invd-tenant")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Invd Property",
            slug="invd-property",
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="INV-400",
            check_in=date(2026, 9, 15),
            check_out=date(2026, 9, 16),
            adults_count=1,
            booker_name="Julianna Pihlar",
            status=Reservation.Status.EXPECTED,
        )

    def test_ensure_active_is_idempotent(self):
        first = ensure_active_invoice_details_access(
            self.reservation,
            created_from=GuestInvoiceDetailsAccessCreatedFrom.RECEPTION_MANUAL,
        )
        second = ensure_active_invoice_details_access(
            self.reservation,
            created_from=GuestInvoiceDetailsAccessCreatedFrom.BOOKING,
        )
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.token, second.token)
        self.assertEqual(first.created_from, GuestInvoiceDetailsAccessCreatedFrom.RECEPTION_MANUAL)

    def test_canceled_unavailable(self):
        access = ensure_active_invoice_details_access(
            self.reservation,
            created_from=GuestInvoiceDetailsAccessCreatedFrom.SYSTEM,
        )
        self.reservation.status = Reservation.Status.CANCELED
        self.reservation.save(update_fields=["status", "updated_at"])
        result = evaluate_invoice_details_access(access)
        self.assertFalse(result.allowed)
        self.assertEqual(result.gate_status, "unavailable")

    def test_revoked(self):
        access = ensure_active_invoice_details_access(
            self.reservation,
            created_from=GuestInvoiceDetailsAccessCreatedFrom.SYSTEM,
        )
        revoke_invoice_details_access(access)
        result = evaluate_invoice_details_access(access)
        self.assertEqual(result.gate_status, "revoked")

    def test_issued_is_read_only(self):
        access = ensure_active_invoice_details_access(
            self.reservation,
            created_from=GuestInvoiceDetailsAccessCreatedFrom.SYSTEM,
        )
        Invoice.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            invoice_number="1-ROOMS-1",
            sequence_number=1,
            issued_at=datetime(2026, 9, 16, 11, 0, 0),
            buyer_name="Julianna Pihlar",
            subtotal=Decimal("88.50"),
            vat_amount=Decimal("11.50"),
            total=Decimal("100.00"),
        )
        result = evaluate_invoice_details_access(access)
        self.assertTrue(result.allowed)
        self.assertFalse(result.writable)
        self.assertEqual(result.gate_status, "issued")


class GuestInvoiceDetailsPublicAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.tenant = Tenant.objects.create(name="Invd API", slug="invd-api")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="API Invd Property",
            slug="api-invd-property",
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="INV-API-1",
            check_in=date.today(),
            check_out=date.today() + timedelta(days=1),
            adults_count=1,
            booker_name="API Guest",
            booker_email="relay@guest.booking.com",
            status=Reservation.Status.EXPECTED,
        )
        Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            name="API Guest",
            first_name="API",
            last_name="Guest",
            is_primary=True,
        )
        self.access = ensure_active_invoice_details_access(
            self.reservation,
            created_from=GuestInvoiceDetailsAccessCreatedFrom.SYSTEM,
        )
        self.url = reverse(
            "public-guest-invoice-details",
            kwargs={"token": self.access.token},
        )

    def _ready_payload(self) -> dict:
        return {
            "kind": "company",
            "company_name": "Julianna Pihlar S.P.",
            "tax_id": "96977604",
            "tax_id_country": "SI",
            "country": "SI",
            "address": "Ljubljanska 1",
            "postal_code": "1000",
            "city": "Ljubljana",
            "email": "julianna@edes.si",
        }

    def test_get_active(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "active")
        self.assertTrue(data["writable"])
        self.assertEqual(data["property_name"], "API Invd Property")
        self.assertIsNone(data["recipient"])

    def test_patch_incomplete_company_400(self):
        response = self.client.patch(
            self.url,
            {"kind": "company", "company_name": "Julianna Pihlar S.P.", "tax_id": "96977604"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["reason"], "structurally_incomplete")
        self.assertFalse(BillingRecipient.objects.exists())

    def test_patch_hr_oib_must_be_11_digits(self):
        payload = self._ready_payload()
        payload["tax_id"] = "123"
        payload["tax_id_country"] = "HR"
        payload["country"] = "HR"
        response = self.client.patch(self.url, payload, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("tax_id", response.json()["missing"])

    def test_patch_si_vat_ready(self):
        response = self.client.patch(self.url, self._ready_payload(), format="json")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["recipient"]["status"], BillingRecipient.Status.READY)
        self.assertEqual(data["recipient"]["tax_id_country"], "SI")
        self.assertEqual(data["recipient"]["source"], RecipientSource.GUEST_FORM.value)
        row = BillingRecipient.objects.get()
        self.assertEqual(row.email, "julianna@edes.si")
        self.assertEqual(row.identity_confidence, BillingRecipient.IdentityConfidence.UNVERIFIED)

    def test_patch_personal_email(self):
        response = self.client.patch(
            self.url,
            {"kind": "personal", "email": "guest@example.com"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.booker_email, "guest@example.com")
        self.assertFalse(BillingRecipient.objects.exists())

    def test_patch_personal_blocked_when_company_open(self):
        create_open_recipient(
            self.reservation,
            {"company_name": "Example d.o.o."},
            source=RecipientSource.STAFF.value,
            source_excerpt="R1",
        )
        response = self.client.patch(
            self.url,
            {"kind": "personal", "email": "guest@example.com"},
            format="json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["reason"], "company_form_in_progress")

    def test_patch_issued_410(self):
        Invoice.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            invoice_number="1-ROOMS-1",
            sequence_number=1,
            issued_at=datetime(2026, 9, 16, 11, 0, 0),
            buyer_name="API Guest",
            subtotal=Decimal("88.50"),
            vat_amount=Decimal("11.50"),
            total=Decimal("100.00"),
        )
        response = self.client.patch(self.url, self._ready_payload(), format="json")
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.json()["status"], "issued")

    def test_get_canceled_410(self):
        self.reservation.status = Reservation.Status.CANCELED
        self.reservation.save(update_fields=["status", "updated_at"])
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.json()["status"], "unavailable")


class InvoiceRecipientPrefersBillingRecipientEmailTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Pref Tenant", slug="pref-invd")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="P",
            slug="p-pref",
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booker_name="Guest",
            booker_email="booker@example.com",
            check_in=date(2026, 9, 15),
            check_out=date(2026, 9, 16),
            status=Reservation.Status.EXPECTED,
        )

    def test_ready_recipient_email_wins(self):
        create_open_recipient(
            self.reservation,
            {
                "company_name": "Example GmbH",
                "tax_id": "DE123456789",
                "tax_id_country": "DE",
                "country": "DE",
                "address": "Unter den Linden 1",
                "postal_code": "10115",
                "city": "Berlin",
                "email": "billing@example.com",
            },
        )
        self.assertEqual(resolve_invoice_recipient(self.reservation), "billing@example.com")
