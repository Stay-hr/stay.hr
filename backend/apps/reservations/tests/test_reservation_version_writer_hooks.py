from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.communications.guest_email_ingest import ingest_parsed_email, ParsedGuestEmail
from apps.communications.guest_message_send import (
    _send_whatsapp_handoff,
    send_guest_email_with_timeline_record,
)
from apps.communications.guest_message_whatsapp_v2 import _handle_transient_failure, WhatsAppSendPendingError
from apps.communications.models import (
    GuestMessageChannel,
    GuestMessageDraft,
    GuestMessageIntent,
    GuestOutboundDeliveryStatus,
    GuestOutboundMessage,
    GuestOutboundMessageStatus,
)
from apps.integrations.channex.booking_service import channex_external_id
from apps.integrations.channex.message_service import upsert_channex_message_from_payload
from apps.integrations.models import ChannexMessage, IntegrationConfig, WhatsAppMessage
from apps.integrations.whatsapp.tasks import process_inbound_message
from apps.integrations.whatsapp.webhook_service import apply_outbound_status_update
from apps.properties.models import Property
from apps.reservations.models import Reservation, ReservationVersion, ReservationVersionScope
from apps.tenants.models import Tenant, TenantReceptionSettings


class ReservationVersionWriterHooksTestCase(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Hooks Tenant", slug="hooks-tenant")
        TenantReceptionSettings.objects.create(
            tenant=self.tenant,
            guest_contact_email="room_reservations@hooks.test",
        )
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Hooks Property",
            slug="hooks-property",
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="5238895494",
            external_id=channex_external_id("booking-uuid-456"),
            import_source="channex",
            check_in=timezone.localdate(),
            check_out=timezone.localdate(),
            booker_name="Hook Guest",
            booker_phone="+385981234567",
            amount=Decimal("100.00"),
        )
        self.integration = IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.WHATSAPP,
            routing_key="7794189252778687",
            is_active=True,
        )
        self.integration.set_config_dict(
            {
                "phone_number_id": "7794189252778687",
                "waba_id": "123456789",
            }
        )
        self.integration.save()
        self.channex_integration = IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.CHANNEX,
            is_active=True,
        )

    def _messages_version(self) -> int:
        row = ReservationVersion.objects.filter(
            reservation_id=self.reservation.pk,
            scope=ReservationVersionScope.MESSAGES,
        ).first()
        return row.version if row else 0

    @patch("apps.communications.guest_invoice_inbound.maybe_handle_guest_invoice_inbound", return_value=None)
    @patch("apps.communications.guest_parking_inbound.maybe_handle_guest_parking_inbound")
    @patch("apps.communications.guest_arrival_inbound.maybe_handle_guest_arrival_inbound", return_value=None)
    def test_email_inbound_touches_messages_version(self, _arrival, _parking, _invoice):
        parsed = ParsedGuestEmail(
            message_id="email-hook-1",
            raw_from="Guest <guest@example.com>",
            from_email="guest@example.com",
            subject="Re: Booking",
            body_text="Hello from guest",
            booking_code=self.reservation.booking_code,
            received_at=timezone.now(),
        )

        ingest_parsed_email(self.tenant, parsed, notify=False)

        self.assertEqual(self._messages_version(), 1)

    @patch("apps.communications.guest_message_send.send_guest_text_email")
    def test_email_outbound_touches_on_sent(self, send_mock):
        send_mock.return_value = {"sent": True, "to": "guest@example.com"}
        self.reservation.booker_email = "guest@example.com"
        self.reservation.save(update_fields=["booker_email"])

        send_guest_email_with_timeline_record(
            self.reservation,
            "Outbound body",
        )

        self.assertEqual(self._messages_version(), 1)

    @patch("apps.communications.guest_message_send.send_guest_text_email")
    def test_email_outbound_does_not_touch_on_failure(self, send_mock):
        send_mock.return_value = {"sent": False, "reason": "smtp_not_configured"}
        self.reservation.booker_email = "guest@example.com"
        self.reservation.save(update_fields=["booker_email"])

        send_guest_email_with_timeline_record(
            self.reservation,
            "Outbound body",
        )

        self.assertEqual(self._messages_version(), 0)

    def test_whatsapp_handoff_touches_messages_version(self):
        draft = GuestMessageDraft.objects.create(
            tenant_id=self.tenant.pk,
            reservation=self.reservation,
            intent=GuestMessageIntent.REPLY,
            llm_body_text="Handoff text",
        )

        _send_whatsapp_handoff(
            reservation=self.reservation,
            draft=draft,
            body_text="Handoff text",
            api_application=None,
            handoff_reason="integration_not_configured",
        )

        self.assertEqual(self._messages_version(), 1)

    def test_channex_message_touches_on_new_visible_row(self):
        row, created = upsert_channex_message_from_payload(
            tenant=self.tenant,
            integration=self.channex_integration,
            payload={
                "id": "msg-hook-1",
                "message": "Hello from guest",
                "sender": "guest",
                "booking_id": "booking-uuid-456",
            },
            reservation=self.reservation,
        )

        self.assertTrue(created)
        self.assertEqual(row.body, "Hello from guest")
        self.assertEqual(self._messages_version(), 1)

    def test_channex_message_does_not_touch_on_duplicate(self):
        payload = {
            "id": "msg-hook-dup",
            "message": "Duplicate",
            "sender": "guest",
            "booking_id": "booking-uuid-456",
        }
        upsert_channex_message_from_payload(
            tenant=self.tenant,
            integration=self.channex_integration,
            payload=payload,
            reservation=self.reservation,
        )
        self.assertEqual(self._messages_version(), 1)

        _, created = upsert_channex_message_from_payload(
            tenant=self.tenant,
            integration=self.channex_integration,
            payload=payload,
            reservation=self.reservation,
        )

        self.assertFalse(created)
        self.assertEqual(self._messages_version(), 1)

    def test_delivered_status_webhook_does_not_touch(self):
        outbound = GuestOutboundMessage.objects.create(
            tenant_id=self.tenant.pk,
            reservation=self.reservation,
            channel=GuestMessageChannel.WHATSAPP,
            body_text="Sent message",
            status=GuestOutboundMessageStatus.SENT,
            provider_message_id="wamid-status-1",
            delivery_status=GuestOutboundDeliveryStatus.SENT,
        )

        result = apply_outbound_status_update(wamid=outbound.provider_message_id, status="delivered")

        self.assertEqual(result["status"], "updated")
        self.assertEqual(self._messages_version(), 0)

    def test_read_status_webhook_does_not_touch(self):
        outbound = GuestOutboundMessage.objects.create(
            tenant_id=self.tenant.pk,
            reservation=self.reservation,
            channel=GuestMessageChannel.WHATSAPP,
            body_text="Sent message",
            status=GuestOutboundMessageStatus.SENT,
            provider_message_id="wamid-status-2",
            delivery_status=GuestOutboundDeliveryStatus.DELIVERED,
        )

        apply_outbound_status_update(wamid=outbound.provider_message_id, status="read")

        self.assertEqual(self._messages_version(), 0)

    def test_pending_send_retry_metadata_does_not_touch(self):
        outbound = GuestOutboundMessage.objects.create(
            tenant_id=self.tenant.pk,
            reservation=self.reservation,
            channel=GuestMessageChannel.WHATSAPP,
            body_text="Retry me",
            status=GuestOutboundMessageStatus.PENDING_SEND,
            to_phone=self.reservation.booker_phone,
        )

        with self.assertRaises(WhatsAppSendPendingError):
            _handle_transient_failure(outbound=outbound, exc=RuntimeError("transient"))

        self.assertEqual(self._messages_version(), 0)


@override_settings(
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class WhatsAppInboundWriterHookTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="WA Hooks Tenant", slug="wa-hooks-tenant")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="WA Hooks Property",
            slug="wa-hooks-property",
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="WA-HOOK-1",
            check_in=timezone.localdate(),
            check_out=timezone.localdate(),
            booker_name="WA Guest",
            booker_phone="+385981112223",
        )
        self.integration = IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.WHATSAPP,
            routing_key="7794189252778687",
            is_active=True,
        )
        self.integration.set_config_dict(
            {
                "phone_number_id": "7794189252778687",
                "waba_id": "123456789",
                "auto_reply": False,
            }
        )
        self.integration.save()

    def _messages_version(self) -> int:
        row = ReservationVersion.objects.filter(
            reservation_id=self.reservation.pk,
            scope=ReservationVersionScope.MESSAGES,
        ).first()
        return row.version if row else 0

    @patch("apps.integrations.whatsapp.tasks.handle_guest_autocheckin_inbound", return_value={"status": "skipped"})
    def test_whatsapp_inbound_touches_when_reservation_linked(self, _autocheckin):
        inbound = WhatsAppMessage.objects.create(
            tenant_id=self.tenant.pk,
            integration=self.integration,
            reservation=self.reservation,
            wamid="wamid-inbound-hook-1",
            wa_id="385981112223",
            phone_number_id="7794189252778687",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="text",
            body="Inbound hello",
        )

        process_inbound_message(inbound.pk)

        self.assertEqual(self._messages_version(), 1)

    @patch("apps.integrations.whatsapp.tasks.handle_guest_autocheckin_inbound", return_value={"status": "awaiting_code"})
    def test_whatsapp_inbound_does_not_touch_without_reservation(self, _autocheckin):
        inbound = WhatsAppMessage.objects.create(
            tenant_id=self.tenant.pk,
            integration=self.integration,
            reservation=None,
            wamid="wamid-inbound-unrouted",
            wa_id="385999888777",
            phone_number_id="7794189252778687",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="text",
            body="No reservation yet",
        )

        process_inbound_message(inbound.pk)

        self.assertEqual(ReservationVersion.objects.count(), 0)
