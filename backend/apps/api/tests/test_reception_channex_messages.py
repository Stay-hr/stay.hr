from datetime import date
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.integrations.channex.booking_service import channex_external_id
from apps.integrations.models import ChannexMessage, IntegrationConfig
from apps.properties.models import Property, Unit
from apps.reservations.models import Reservation
from apps.tenants.models import ChannelManager, Tenant, TenantMembership, TenantReceptionSettings

User = get_user_model()


class ReceptionChannexMessagesTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="demo", name="Demo")
        TenantReceptionSettings.objects.create(
            tenant=self.tenant,
            channel_manager=ChannelManager.CHANNEX,
        )
        self.property = Property.objects.create(
            tenant=self.tenant,
            slug="channex-demo",
            name="Demo",
            timezone="Europe/Zagreb",
        )
        self.unit = Unit.objects.create(
            tenant=self.tenant,
            property=self.property,
            code="BCOM-STUDIO",
            name="Studio",
        )
        self.integration = IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.CHANNEX,
            is_active=True,
        )
        self.integration.set_config_dict(
            {
                "property_id": "prop-id",
                "sync_property_slug": "channex-demo",
            }
        )
        self.integration.save()
        self.booking_id = "channex-booking-1"
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            external_id=channex_external_id(self.booking_id),
            import_source="channex",
            check_in=date(2026, 6, 20),
            check_out=date(2026, 6, 21),
            booker_name="Guest Test",
            status=Reservation.Status.EXPECTED,
        )
        self.manual_reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            import_source="manual",
            check_in=date(2026, 7, 1),
            check_out=date(2026, 7, 2),
            booker_name="Manual Guest",
            status=Reservation.Status.EXPECTED,
        )

        self.staff = User.objects.create_user(
            username="evan",
            password="secret-pass",
            is_staff=True,
        )
        TenantMembership.objects.create(user=self.staff, tenant=self.tenant)
        self.client = APIClient()

    def _login(self):
        response = self.client.post(
            "/api/v1/auth/reception-login/",
            {"username": "evan", "password": "secret-pass"},
            format="json",
            HTTP_HOST="app.stay.hr",
        )
        self.assertEqual(response.status_code, 200)

    def test_get_lists_stored_messages(self):
        ChannexMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            reservation=self.reservation,
            channex_booking_id=self.booking_id,
            channex_message_id="stored-msg-1",
            direction=ChannexMessage.Direction.INBOUND,
            sender=ChannexMessage.Sender.GUEST,
            body="Existing message",
        )
        self._login()
        response = self.client.get(
            f"/api/v1/reception/reservations/{self.reservation.pk}/channex-messages/",
            HTTP_HOST="app.stay.hr",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["reservation_id"], self.reservation.pk)
        self.assertEqual(len(data["messages"]), 1)
        self.assertEqual(data["messages"][0]["body"], "Existing message")

    @patch("apps.integrations.channex.message_service.ChannexClient")
    def test_get_does_not_sync_from_channex_when_empty(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        self._login()
        response = self.client.get(
            f"/api/v1/reception/reservations/{self.reservation.pk}/channex-messages/",
            HTTP_HOST="app.stay.hr",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["messages"], [])
        mock_client.list_booking_messages.assert_not_called()
        self.assertFalse(ChannexMessage.objects.filter(channex_message_id="remote-msg-1").exists())

    @patch("apps.integrations.channex.message_service.ChannexClient")
    def test_post_sends_outbound_message(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.send_booking_message.return_value = {
            "data": {
                "id": "outbound-msg-1",
                "attributes": {
                    "message": "Reply from property",
                    "sender": "property",
                    "booking_id": self.booking_id,
                },
            }
        }
        mock_client_cls.return_value = mock_client

        self._login()
        response = self.client.post(
            f"/api/v1/reception/reservations/{self.reservation.pk}/channex-messages/",
            {"message": "Reply from property"},
            format="json",
            HTTP_HOST="app.stay.hr",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["sender"], ChannexMessage.Sender.PROPERTY)
        mock_client.send_booking_message.assert_called_once_with(
            self.booking_id,
            "Reply from property",
        )

    def test_post_rejects_non_channex_reservation(self):
        self._login()
        response = self.client.post(
            f"/api/v1/reception/reservations/{self.manual_reservation.pk}/channex-messages/",
            {"message": "Hello"},
            format="json",
            HTTP_HOST="app.stay.hr",
        )
        self.assertEqual(response.status_code, 400)
