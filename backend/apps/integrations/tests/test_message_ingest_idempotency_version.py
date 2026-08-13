"""ADR 0019 Phase B: ingest identity keys are idempotent; version bumps only on new UI rows."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from apps.communications.guest_email_ingest import ParsedGuestEmail, ingest_parsed_email
from apps.communications.models import GuestInboundMessage
from apps.integrations.channex.booking_service import channex_external_id
from apps.integrations.channex.message_service import (
    relink_unlinked_channex_messages,
    sync_booking_messages_from_channex,
    upsert_channex_message_from_payload,
)
from apps.integrations.channex.webhook_service import record_channex_webhook
from apps.integrations.models import ChannexMessage, IntegrationConfig, WhatsAppMessage
from apps.integrations.whatsapp.webhook_service import (
    ParsedInboundMessage,
    record_inbound_whatsapp_message,
)
from apps.properties.models import Property
from apps.reservations.models import Reservation, ReservationVersion, ReservationVersionScope
from apps.tenants.models import ChannelManager, Tenant, TenantReceptionSettings


class MessageIngestIdempotencyVersionTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="idemp-tenant", name="Idemp")
        TenantReceptionSettings.objects.create(
            tenant=self.tenant,
            channel_manager=ChannelManager.CHANNEX,
        )
        self.property = Property.objects.create(
            tenant=self.tenant,
            slug="idemp-property",
            name="Idemp Property",
        )
        self.booking_id = "booking-idemp-1"
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            external_id=channex_external_id(self.booking_id),
            import_source="channex",
            booking_code="5238895494",
            check_in=date(2026, 8, 13),
            check_out=date(2026, 8, 14),
            booker_name="Idemp Guest",
            status=Reservation.Status.EXPECTED,
        )
        self.channex = IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.CHANNEX,
            is_active=True,
        )
        self.whatsapp = IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.WHATSAPP,
            routing_key="7794189252778687",
            is_active=True,
        )
        self.whatsapp.set_config_dict(
            {
                "phone_number_id": "7794189252778687",
                "waba_id": "123456789",
                "auto_reply": False,
            }
        )
        self.whatsapp.save()

    def _messages_version(self, reservation=None) -> int:
        reservation = reservation or self.reservation
        row = ReservationVersion.objects.filter(
            reservation_id=reservation.pk,
            scope=ReservationVersionScope.MESSAGES,
        ).first()
        return row.version if row else 0

    def test_channex_duplicate_id_is_one_row_and_one_version_bump(self):
        payload = {
            "id": "channex-idemp-1",
            "message": "Hello",
            "sender": "guest",
            "booking_id": self.booking_id,
        }
        record_channex_webhook(
            integration_row=self.channex,
            tenant=self.tenant,
            event="message",
            property_id="prop-1",
            body={"payload": payload},
        )
        record_channex_webhook(
            integration_row=self.channex,
            tenant=self.tenant,
            event="message",
            property_id="prop-1",
            body={"payload": payload},
        )
        self.assertEqual(
            ChannexMessage.objects.filter(channex_message_id="channex-idemp-1").count(),
            1,
        )
        self.assertEqual(self._messages_version(), 1)

    @patch("apps.integrations.channex.message_service.ChannexClient")
    def test_channex_webhook_then_sync_same_id_does_not_bump_again(self, mock_client_cls):
        payload = {
            "id": "channex-idemp-sync",
            "message": "Hello again",
            "sender": "guest",
            "booking_id": self.booking_id,
        }
        record_channex_webhook(
            integration_row=self.channex,
            tenant=self.tenant,
            event="message",
            property_id="prop-1",
            body={"payload": payload},
        )
        self.assertEqual(self._messages_version(), 1)

        mock_client = MagicMock()
        mock_client.list_booking_messages.return_value = {"data": [payload]}
        mock_client_cls.return_value = mock_client
        sync_booking_messages_from_channex(self.channex, self.reservation)

        self.assertEqual(
            ChannexMessage.objects.filter(channex_message_id="channex-idemp-sync").count(),
            1,
        )
        self.assertEqual(self._messages_version(), 1)

    def test_channex_empty_invisible_row_does_not_touch_version(self):
        row, created = upsert_channex_message_from_payload(
            tenant=self.tenant,
            integration=self.channex,
            payload={
                "id": "channex-empty",
                "message": "   ",
                "sender": "guest",
                "booking_id": self.booking_id,
            },
            reservation=self.reservation,
        )
        self.assertTrue(created)
        self.assertEqual(row.body, "")
        self.assertEqual(self._messages_version(), 0)

    def test_channex_message_id_unique_constraint(self):
        defaults = dict(
            tenant=self.tenant,
            integration=self.channex,
            reservation=self.reservation,
            channex_booking_id=self.booking_id,
            direction=ChannexMessage.Direction.INBOUND,
            sender=ChannexMessage.Sender.GUEST,
            body="one",
        )
        ChannexMessage.objects.create(channex_message_id="dup-channex-pk", **defaults)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ChannexMessage.objects.create(channex_message_id="dup-channex-pk", **defaults)

    def test_relink_touches_version_when_row_becomes_visible(self):
        ChannexMessage.objects.create(
            tenant=self.tenant,
            integration=self.channex,
            reservation=None,
            channex_booking_id=self.booking_id,
            channex_message_id="orphan-visible",
            direction=ChannexMessage.Direction.INBOUND,
            sender=ChannexMessage.Sender.GUEST,
            body="Late link",
            raw_payload={"booking_id": self.booking_id},
        )
        self.assertEqual(self._messages_version(), 0)

        updated = relink_unlinked_channex_messages(self.tenant)
        self.assertEqual(updated, 1)
        self.assertEqual(self._messages_version(), 1)

        relink_unlinked_channex_messages(self.tenant)
        self.assertEqual(self._messages_version(), 1)

    @patch("apps.communications.guest_invoice_inbound.maybe_handle_guest_invoice_inbound", return_value=None)
    @patch("apps.communications.guest_parking_inbound.maybe_handle_guest_parking_inbound")
    @patch("apps.communications.guest_arrival_inbound.maybe_handle_guest_arrival_inbound", return_value=None)
    def test_email_message_id_dedup_does_not_bump_again(self, _arrival, _parking, _invoice):
        parsed = ParsedGuestEmail(
            message_id="<idemp-email-1@example.com>",
            raw_from="Guest <guest@example.com>",
            from_email="guest@example.com",
            subject="Re: Booking",
            body_text="Hello from guest",
            booking_code=self.reservation.booking_code,
            received_at=timezone.now(),
        )
        first = ingest_parsed_email(self.tenant, parsed, notify=False)
        second = ingest_parsed_email(self.tenant, parsed, notify=False)
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(GuestInboundMessage.objects.count(), 1)
        self.assertEqual(self._messages_version(), 1)

    @patch("apps.communications.guest_invoice_inbound.maybe_handle_guest_invoice_inbound", return_value=None)
    @patch("apps.communications.guest_parking_inbound.maybe_handle_guest_parking_inbound")
    @patch("apps.communications.guest_arrival_inbound.maybe_handle_guest_arrival_inbound", return_value=None)
    def test_email_blank_message_id_is_not_fabricated(self, _arrival, _parking, _invoice):
        now = timezone.now()
        first = ingest_parsed_email(
            self.tenant,
            ParsedGuestEmail(
                message_id="",
                raw_from="Guest <guest@example.com>",
                from_email="guest@example.com",
                subject="Re: Booking",
                body_text="First blank id",
                booking_code=self.reservation.booking_code,
                received_at=now,
            ),
            notify=False,
        )
        second = ingest_parsed_email(
            self.tenant,
            ParsedGuestEmail(
                message_id="",
                raw_from="Guest <guest@example.com>",
                from_email="guest@example.com",
                subject="Re: Booking",
                body_text="Second blank id",
                booking_code=self.reservation.booking_code,
                received_at=now,
            ),
            notify=False,
        )
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(first.message_id, "")
        self.assertEqual(second.message_id, "")
        self.assertEqual(GuestInboundMessage.objects.count(), 2)
        self.assertEqual(self._messages_version(), 2)

    @patch("apps.integrations.whatsapp.webhook_service.process_inbound_message.delay")
    def test_whatsapp_wamid_duplicate_does_not_requeue(self, mock_delay):
        parsed = ParsedInboundMessage(
            phone_number_id="7794189252778687",
            wa_id="385981112223",
            wamid="wamid.idemp.1",
            message_type="text",
            body="Hello WA",
            profile_name="Guest",
            raw_message={"id": "wamid.idemp.1", "type": "text"},
        )
        first = record_inbound_whatsapp_message(
            integration_row=self.whatsapp,
            parsed=parsed,
        )
        second = record_inbound_whatsapp_message(
            integration_row=self.whatsapp,
            parsed=parsed,
        )
        self.assertEqual(first["status"], "queued")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(WhatsAppMessage.objects.filter(wamid="wamid.idemp.1").count(), 1)
        mock_delay.assert_called_once()

    def test_whatsapp_wamid_unique_constraint(self):
        defaults = dict(
            tenant=self.tenant,
            integration=self.whatsapp,
            wa_id="385981112223",
            phone_number_id="7794189252778687",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="text",
            body="one",
        )
        WhatsAppMessage.objects.create(wamid="wamid.dup.pk", **defaults)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                WhatsAppMessage.objects.create(wamid="wamid.dup.pk", **defaults)
