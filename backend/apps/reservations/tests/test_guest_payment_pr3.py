from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from apps.billing.models import TenantFiscalSettings
from apps.properties.models import Property
from apps.properties.share_service import ShareService, ShareServiceError
from apps.reservations.guest_payment_access import (
    build_payment_reference,
    ensure_active_payment_access,
    evaluate_payment_access,
    revoke_payment_access,
)
from apps.reservations.guest_payment_context import build_guest_payment_context
from apps.reservations.models import GuestPaymentAccessCreatedFrom, Reservation
from apps.tenants.models import RECEPTION_DEVICE_SCOPES, ApiApplication, Tenant


class GuestPaymentAccessTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Pay Tenant", slug="pay-tenant")
        self.other_tenant = Tenant.objects.create(name="Other Pay", slug="other-pay")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Pay Property",
            slug="pay-property",
        )
        self.other_property = Property.objects.create(
            tenant=self.other_tenant,
            name="Foreign Property",
            slug="foreign-property",
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="UZ-400",
            check_in=date(2026, 8, 24),
            check_out=date(2026, 8, 28),
            adults_count=1,
            booker_name="Hrvoje Hrčka",
            amount=Decimal("400.00"),
            booker_country="HR",
        )
        TenantFiscalSettings.objects.create(
            tenant=self.tenant,
            issuer_name="Šupina Poljica d.o.o.",
            issuer_iban="HR1234567890123456789",
            issuer_oib="12345678901",
            business_premise_code="PP1",
            payment_device_code="1",
        )

    def test_build_payment_reference_uses_booking_code(self):
        self.assertEqual(build_payment_reference(self.reservation), "UZ-400")

    def test_build_payment_reference_fallback_without_booking_code(self):
        self.reservation.booking_code = ""
        self.reservation.save(update_fields=["booking_code", "updated_at"])
        self.assertEqual(build_payment_reference(self.reservation), f"STAY-{self.reservation.pk}")

    def test_ensure_active_payment_access_is_idempotent(self):
        first = ensure_active_payment_access(
            self.reservation,
            created_from=GuestPaymentAccessCreatedFrom.RECEPTION_MANUAL,
        )
        second = ensure_active_payment_access(
            self.reservation,
            created_from=GuestPaymentAccessCreatedFrom.EMAIL,
        )
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.token, second.token)
        self.assertEqual(first.created_from, GuestPaymentAccessCreatedFrom.RECEPTION_MANUAL)

    def test_evaluate_payment_access_canceled_unavailable(self):
        access = ensure_active_payment_access(
            self.reservation,
            created_from=GuestPaymentAccessCreatedFrom.SYSTEM,
        )
        self.reservation.status = Reservation.Status.CANCELED
        self.reservation.save(update_fields=["status", "updated_at"])
        result = evaluate_payment_access(access)
        self.assertFalse(result.allowed)
        self.assertEqual(result.http_status, 410)
        self.assertEqual(result.gate_status, "unavailable")

    def test_evaluate_payment_access_revoked(self):
        access = ensure_active_payment_access(
            self.reservation,
            created_from=GuestPaymentAccessCreatedFrom.SYSTEM,
        )
        revoke_payment_access(access)
        result = evaluate_payment_access(access)
        self.assertFalse(result.allowed)
        self.assertEqual(result.gate_status, "revoked")

    def test_build_guest_payment_context_payload(self):
        access = ensure_active_payment_access(
            self.reservation,
            created_from=GuestPaymentAccessCreatedFrom.SYSTEM,
        )
        ctx = build_guest_payment_context(access)
        self.assertEqual(ctx["payment_amount"], "400.00")
        self.assertEqual(ctx["payment_reference"], "UZ-400")
        self.assertEqual(ctx["iban"], "HR1234567890123456789")
        self.assertTrue(ctx["includes_tourist_tax"])


class GuestPaymentPublicAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.tenant = Tenant.objects.create(name="Pay API", slug="pay-api")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="API Pay Property",
            slug="api-pay-property",
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="PAY-API-1",
            check_in=date.today(),
            check_out=date.today() + timedelta(days=3),
            adults_count=1,
            booker_name="API Guest",
            amount=Decimal("250.00"),
            booker_country="HR",
        )
        TenantFiscalSettings.objects.create(
            tenant=self.tenant,
            issuer_name="Issuer d.o.o.",
            issuer_iban="HR9911223344556677889",
            issuer_oib="12345678901",
            business_premise_code="PP1",
            payment_device_code="1",
        )
        self.access = ensure_active_payment_access(
            self.reservation,
            created_from=GuestPaymentAccessCreatedFrom.SYSTEM,
        )
        self.token = str(self.access.token)

    def test_get_payment_returns_instructions(self):
        url = reverse("public-guest-payment", kwargs={"token": self.token})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "active")
        self.assertEqual(data["payment_amount"], "250.00")
        self.assertEqual(data["payment_reference"], "PAY-API-1")
        self.assertEqual(data["iban"], "HR9911223344556677889")

    def test_get_payment_canceled_410(self):
        self.reservation.status = Reservation.Status.CANCELED
        self.reservation.save(update_fields=["status", "updated_at"])
        url = reverse("public-guest-payment", kwargs={"token": self.token})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.json()["status"], "unavailable")

    def test_cross_tenant_token_does_not_leak_other_property(self):
        other_tenant = Tenant.objects.create(name="Foreign", slug="foreign-pay")
        other_property = Property.objects.create(
            tenant=other_tenant,
            name="Secret Property",
            slug="secret-property",
        )
        other_reservation = Reservation.objects.create(
            tenant=other_tenant,
            property=other_property,
            booking_code="SECRET-1",
            check_in=date.today(),
            check_out=date.today() + timedelta(days=1),
            adults_count=1,
            booker_name="Secret Guest",
            amount=Decimal("999.00"),
        )
        foreign_access = ensure_active_payment_access(
            other_reservation,
            created_from=GuestPaymentAccessCreatedFrom.SYSTEM,
        )

        url = reverse("public-guest-payment", kwargs={"token": foreign_access.token})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["property_name"], "Secret Property")
        self.assertNotEqual(data["property_name"], self.property.name)
        self.assertEqual(data["payment_reference"], "SECRET-1")


class GuestPaymentShareServiceTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Share Pay", slug="share-pay")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Share Pay Property",
            slug="share-pay-property",
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="SHARE-PAY-1",
            check_in=date(2026, 8, 24),
            check_out=date(2026, 8, 28),
            adults_count=1,
            booker_name="Share Guest",
            booker_email="share@example.com",
            amount=Decimal("400.00"),
            status=Reservation.Status.EXPECTED,
        )

    @override_settings(RECEPTION_PROPERTY_SETTINGS=True)
    @patch(
        "apps.properties.share_service.send_guest_payment_link",
        return_value={
            "status": "sent",
            "payment_url": "https://booking.example/pay/tok",
            "access_id": 3,
            "draft_id": 4,
        },
    )
    def test_share_payment_reservation(self, mock_send):
        result = ShareService.share(
            self.property,
            {
                "kind": "payment",
                "target": "reservation",
                "reservation_id": self.reservation.pk,
                "channel": "email",
            },
        )
        self.assertEqual(result.kind, "payment")
        self.assertEqual(result.status, "sent")
        self.assertEqual(result.payment_url, "https://booking.example/pay/tok")
        mock_send.assert_called_once()

    @override_settings(RECEPTION_PROPERTY_SETTINGS=True)
    def test_share_payment_requires_channel(self):
        with self.assertRaises(ShareServiceError) as ctx:
            ShareService.share(
                self.property,
                {
                    "kind": "payment",
                    "target": "reservation",
                    "reservation_id": self.reservation.pk,
                },
            )
        self.assertEqual(ctx.exception.code, "channel_required")


class ReceptionPaymentInstructionsSendAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.tenant = Tenant.objects.create(name="Recv Pay", slug="recv-pay")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Recv Property",
            slug="recv-property",
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="RECV-PAY-1",
            check_in=date(2026, 8, 24),
            check_out=date(2026, 8, 28),
            adults_count=1,
            booker_name="Recv Guest",
            booker_email="recv@example.com",
            amount=Decimal("400.00"),
            status=Reservation.Status.EXPECTED,
        )
        self.app, self.raw_token = ApiApplication.create_with_token(
            tenant=self.tenant,
            name="Recv tablet",
            scopes=RECEPTION_DEVICE_SCOPES,
        )
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.raw_token}"}
        self.url = reverse(
            "reception-reservation-payment-instructions-send",
            kwargs={"pk": self.reservation.pk},
        )

    @patch(
        "apps.api.reception_payment_views.send_guest_payment_link",
        return_value={
            "status": "sent",
            "payment_url": "https://booking.example/pay/abc",
            "access_id": 1,
            "draft_id": 2,
        },
    )
    def test_send_payment_instructions(self, mock_send):
        response = self.client.post(
            self.url,
            data={"channel": "email"},
            content_type="application/json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "sent")
        self.assertEqual(data["payment_url"], "https://booking.example/pay/abc")
        mock_send.assert_called_once()

    def test_send_payment_invalid_channel_400(self):
        response = self.client.post(
            self.url,
            data={"channel": "booking"},
            content_type="application/json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 400)
