from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.communications.models import GuestMessageChannel, GuestOutboundMessage, GuestOutboundMessageStatus
from apps.integrations.models import ChannexMessage, IntegrationConfig, WhatsAppMessage
from apps.properties.models import Property, Unit
from apps.reservations.models import Guest, Reservation, ReservationUnit
from apps.tenants.models import RECEPTION_DEVICE_SCOPES, ApiApplication, Tenant


class ReceptionMessageThreadsAPITests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Uzorita", slug="uzorita")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Luxury Room Uzorita",
            slug="uzorita",
            timezone="Europe/Zagreb",
        )
        self.unit = Unit.objects.create(
            tenant=self.tenant,
            property=self.property,
            code="R1",
            name="Deluxe King Room R1",
        )
        self.app, self.raw_token = ApiApplication.create_with_token(
            tenant=self.tenant,
            name="Test tablet",
            scopes=RECEPTION_DEVICE_SCOPES,
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            external_id="5036489024",
            booking_code="5036489024",
            check_in=timezone.localdate(),
            check_out=timezone.localdate(),
            status=Reservation.Status.EXPECTED,
            booker_name="Daniela Heczko",
            booker_email="daniela@example.com",
            booker_phone="+385 91 1234567",
            amount=Decimal("180.15"),
        )
        ReservationUnit.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            unit=self.unit,
            room_name="Luxury Room Uzorita B&B",
            sort_order=0,
        )
        Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="Daniela",
            last_name="Heczko",
            email="daniela@example.com",
            is_primary=True,
        )
        self.client = APIClient()
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.raw_token}"}

    def test_list_threads_empty(self):
        response = self.client.get(
            "/api/v1/reception/message-threads/?sync=0",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["threads"], [])

    def test_list_threads_with_inbound_needs_reply(self):
        ChannexMessage.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            channex_booking_id="booking-1",
            channex_message_id="msg-in-1",
            direction=ChannexMessage.Direction.INBOUND,
            sender=ChannexMessage.Sender.GUEST,
            body="Hello from guest",
        )
        GuestOutboundMessage.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            channel=GuestMessageChannel.EMAIL,
            body_text="Earlier reply",
            status=GuestOutboundMessageStatus.SENT,
            created_at=timezone.now(),
        )
        ChannexMessage.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            channex_booking_id="booking-1",
            channex_message_id="msg-in-2",
            direction=ChannexMessage.Direction.INBOUND,
            sender=ChannexMessage.Sender.GUEST,
            body="Latest guest message",
        )

        response = self.client.get(
            "/api/v1/reception/message-threads/?sync=0",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["needs_reply_count"], 1)
        thread = data["threads"][0]
        self.assertEqual(thread["reservation_id"], self.reservation.pk)
        self.assertEqual(thread["booker_name"], "Daniela Heczko")
        self.assertEqual(thread["last_message_preview"], "Latest guest message")
        self.assertEqual(thread["last_channel"], "booking")
        self.assertEqual(thread["last_channels"], ["booking"])
        self.assertEqual(thread["last_direction"], "inbound")
        self.assertTrue(thread["needs_reply"])
        self.assertTrue(thread["arrives_today"])

    def test_list_threads_last_channels_when_merged(self):
        now = timezone.now()
        body = "Merged outbound reply"
        channex = ChannexMessage.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            channex_booking_id="booking-1",
            channex_message_id="msg-out-merged",
            direction=ChannexMessage.Direction.OUTBOUND,
            sender=ChannexMessage.Sender.PROPERTY,
            body=body,
        )
        ChannexMessage.objects.filter(pk=channex.pk).update(created_at=now)
        integration = IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.WHATSAPP,
            routing_key="1068791909660300",
            is_active=True,
        )
        wa = WhatsAppMessage.objects.create(
            tenant_id=self.tenant.pk,
            integration=integration,
            reservation=self.reservation,
            wamid="wamid.merged.out",
            wa_id="385911122233",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.OUTBOUND,
            message_type="text",
            body=body,
        )
        WhatsAppMessage.objects.filter(pk=wa.pk).update(created_at=now + timedelta(seconds=45))

        response = self.client.get(
            "/api/v1/reception/message-threads/?sync=0",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        thread = response.json()["threads"][0]
        self.assertEqual(thread["last_message_preview"], body)
        self.assertEqual(thread["last_channels"], ["booking", "whatsapp"])
        self.assertEqual(thread["last_channel"], "booking")

    def test_filter_needs_reply(self):
        other = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            check_in=timezone.localdate(),
            check_out=timezone.localdate(),
            status=Reservation.Status.EXPECTED,
            booker_name="Replied Guest",
        )
        ChannexMessage.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            channex_booking_id="b1",
            channex_message_id="m1",
            direction=ChannexMessage.Direction.INBOUND,
            sender=ChannexMessage.Sender.GUEST,
            body="Need reply",
        )
        GuestOutboundMessage.objects.create(
            tenant=self.tenant,
            reservation=other,
            channel=GuestMessageChannel.WHATSAPP,
            body_text="We replied",
            status=GuestOutboundMessageStatus.HANDOFF_WHATSAPP,
        )

        response = self.client.get(
            "/api/v1/reception/message-threads/?sync=0&needs_reply=1",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["threads"][0]["reservation_id"], self.reservation.pk)

    def test_dismiss_reply_clears_needs_reply(self):
        ChannexMessage.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            channex_booking_id="b1",
            channex_message_id="m-dismiss",
            direction=ChannexMessage.Direction.INBOUND,
            sender=ChannexMessage.Sender.GUEST,
            body="Need reply",
        )
        list_url = "/api/v1/reception/message-threads/?sync=0"
        response = self.client.get(list_url, **self.auth)
        self.assertEqual(response.json()["needs_reply_count"], 1)

        dismiss_url = (
            f"/api/v1/reception/reservations/{self.reservation.pk}/messages/dismiss-reply/"
        )
        dismiss = self.client.post(dismiss_url, **self.auth)
        self.assertEqual(dismiss.status_code, 200)
        self.assertIn("reply_dismissed_at", dismiss.json())

        response = self.client.get(list_url, **self.auth)
        data = response.json()
        self.assertEqual(data["needs_reply_count"], 0)
        self.assertFalse(data["threads"][0]["needs_reply"])

    def test_new_inbound_after_dismiss_restores_needs_reply(self):
        ChannexMessage.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            channex_booking_id="b1",
            channex_message_id="m-old",
            direction=ChannexMessage.Direction.INBOUND,
            sender=ChannexMessage.Sender.GUEST,
            body="Old",
        )
        dismiss_url = (
            f"/api/v1/reception/reservations/{self.reservation.pk}/messages/dismiss-reply/"
        )
        self.client.post(dismiss_url, **self.auth)

        ChannexMessage.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            channex_booking_id="b1",
            channex_message_id="m-new",
            direction=ChannexMessage.Direction.INBOUND,
            sender=ChannexMessage.Sender.GUEST,
            body="New after dismiss",
        )
        response = self.client.get(
            "/api/v1/reception/message-threads/?sync=0",
            **self.auth,
        )
        data = response.json()
        self.assertEqual(data["needs_reply_count"], 1)
        self.assertTrue(data["threads"][0]["needs_reply"])
        self.assertEqual(data["threads"][0]["last_message_preview"], "New after dismiss")

    @patch("apps.communications.guest_message_sync.poll_tenant_guest_inbox")
    def test_list_threads_sync_auto_skips_imap_poll(self, mock_poll):
        response = self.client.get(
            "/api/v1/reception/message-threads/?sync=auto",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        mock_poll.assert_not_called()

    @patch("apps.communications.guest_message_sync.poll_tenant_guest_inbox")
    @patch("apps.integrations.channex.message_service.list_messages_for_reservation")
    def test_list_threads_sync_one_does_not_call_providers(self, mock_channex, mock_poll):
        response = self.client.get(
            "/api/v1/reception/message-threads/?sync=1",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        mock_poll.assert_not_called()
        mock_channex.assert_not_called()
