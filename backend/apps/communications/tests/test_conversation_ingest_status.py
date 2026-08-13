"""ADR 0019 Phase B: last webhook / last poll / ingest lag per channel."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone as dt_timezone
from unittest.mock import MagicMock, patch

from django.test import TestCase

from apps.communications.conversation_ingest_status import (
    conversation_ingest_snapshot,
    mark_conversation_ingest,
    reset_conversation_ingest_for_tests,
)
from apps.communications.guest_email_ingest import poll_tenant_guest_inbox
from apps.core.system_status import build_system_status_payload
from apps.integrations.channex.booking_service import channex_external_id
from apps.integrations.channex.message_tasks import sync_channex_messages_for_upcoming_checkins
from apps.integrations.channex.webhook_service import record_channex_webhook
from apps.integrations.models import IntegrationConfig, WhatsAppMessage
from apps.integrations.whatsapp.webhook_service import (
    ParsedInboundMessage,
    record_inbound_whatsapp_message,
)
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import ChannelManager, Tenant, TenantReceptionSettings


class ConversationIngestSnapshotTests(TestCase):
    def setUp(self):
        reset_conversation_ingest_for_tests()

    def tearDown(self):
        reset_conversation_ingest_for_tests()

    def test_empty_snapshot_has_nulls_and_no_lag(self):
        t0 = datetime(2026, 8, 13, 12, 0, tzinfo=dt_timezone.utc)
        snap = conversation_ingest_snapshot(now=t0)
        self.assertEqual(snap["metrics_scope"], "cluster")
        for channel in ("channex", "whatsapp", "email"):
            block = snap["channels"][channel]
            self.assertIsNone(block["last_webhook_at"])
            self.assertIsNone(block["last_poll_at"])
            self.assertIsNone(block["ingest_lag_seconds"])

    def test_lag_is_seconds_since_latest_stamp_per_channel(self):
        t0 = datetime(2026, 8, 13, 12, 0, tzinfo=dt_timezone.utc)
        mark_conversation_ingest("channex", "webhook", at=t0)
        mark_conversation_ingest("channex", "poll", at=t0 + timedelta(seconds=30))
        mark_conversation_ingest("whatsapp", "webhook", at=t0)
        mark_conversation_ingest("email", "poll", at=t0 + timedelta(seconds=10))

        snap = conversation_ingest_snapshot(now=t0 + timedelta(seconds=90))
        channex = snap["channels"]["channex"]
        self.assertEqual(channex["last_webhook_at"], t0.isoformat())
        self.assertEqual(channex["last_poll_at"], (t0 + timedelta(seconds=30)).isoformat())
        self.assertEqual(channex["ingest_lag_seconds"], 60)

        self.assertEqual(snap["channels"]["whatsapp"]["ingest_lag_seconds"], 90)
        self.assertIsNone(snap["channels"]["whatsapp"]["last_poll_at"])
        self.assertEqual(snap["channels"]["email"]["ingest_lag_seconds"], 80)
        self.assertIsNone(snap["channels"]["email"]["last_webhook_at"])

    def test_system_status_conversation_is_separate_from_messaging(self):
        t0 = datetime(2026, 8, 13, 12, 0, tzinfo=dt_timezone.utc)
        mark_conversation_ingest("channex", "webhook", at=t0)
        payload = build_system_status_payload()
        self.assertEqual(payload["schema_version"], 4)
        self.assertIn("conversation", payload)
        self.assertNotIn("last_webhook_at", payload["messaging"])
        self.assertNotIn("channels", payload["messaging"])
        conversation = payload["conversation"]
        self.assertEqual(conversation["metrics_scope"], "cluster")
        self.assertEqual(
            conversation["channels"]["channex"]["last_webhook_at"],
            t0.isoformat(),
        )
        self.assertIn("definitions", payload["messaging"])
        self.assertIn("outbox", payload["messaging"])


class ConversationIngestHookTests(TestCase):
    def setUp(self):
        reset_conversation_ingest_for_tests()
        self.tenant = Tenant.objects.create(slug="uzorita", name="Uzorita")
        settings_row = TenantReceptionSettings.objects.create(
            tenant=self.tenant,
            channel_manager=ChannelManager.CHANNEX,
            guest_contact_email="room_reservations@uzorita.hr",
        )
        settings_row.set_guest_smtp_password("secret")
        settings_row.save()
        self.property = Property.objects.create(
            tenant=self.tenant,
            slug="uzorita",
            name="Uzorita",
            timezone="Europe/Zagreb",
        )
        self.booking_id = "booking-obs-1"
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            external_id=channex_external_id(self.booking_id),
            import_source="channex",
            check_in=date(2026, 8, 13),
            check_out=date(2026, 8, 14),
            booker_name="Obs Guest",
            status=Reservation.Status.EXPECTED,
        )
        self.channex = IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.CHANNEX,
            is_active=True,
        )
        self.channex.set_config_dict({"property_id": "prop-obs", "sync_property_slug": "uzorita"})
        self.channex.save()
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

    def tearDown(self):
        reset_conversation_ingest_for_tests()

    def test_channex_webhook_duplicate_still_stamps_last_webhook(self):
        body = {
            "payload": {
                "id": "msg-obs-1",
                "message": "Hello",
                "sender": "guest",
                "booking_id": self.booking_id,
                "attachments": [],
                "have_attachment": False,
            }
        }
        record_channex_webhook(
            integration_row=self.channex,
            tenant=self.tenant,
            event="message",
            property_id="prop-obs",
            body=body,
        )
        first = conversation_ingest_snapshot()["channels"]["channex"]["last_webhook_at"]
        self.assertIsNotNone(first)
        record_channex_webhook(
            integration_row=self.channex,
            tenant=self.tenant,
            event="message",
            property_id="prop-obs",
            body=body,
        )
        second = conversation_ingest_snapshot()["channels"]["channex"]
        self.assertIsNotNone(second["last_webhook_at"])
        self.assertGreaterEqual(second["last_webhook_at"], first)
        self.assertIsNone(second["last_poll_at"])

    @patch(
        "apps.integrations.channex.message_tasks.relink_unlinked_channex_messages",
        return_value=0,
    )
    @patch(
        "apps.integrations.channex.message_tasks.sync_booking_messages_from_channex",
        return_value=[],
    )
    @patch("apps.integrations.channex.message_tasks.get_active_channex_integration")
    def test_channex_reconcile_cycle_stamps_last_poll_once(
        self,
        mock_integration,
        mock_sync,
        mock_relink,
    ):
        mock_integration.return_value = MagicMock()
        sync_channex_messages_for_upcoming_checkins(tenant_slug="uzorita")
        first = conversation_ingest_snapshot()["channels"]["channex"]["last_poll_at"]
        self.assertIsNotNone(first)
        sync_channex_messages_for_upcoming_checkins(tenant_slug="uzorita")
        second = conversation_ingest_snapshot()["channels"]["channex"]
        self.assertIsNotNone(second["last_poll_at"])
        self.assertGreaterEqual(second["last_poll_at"], first)
        self.assertIsNone(second["last_webhook_at"])
        self.assertEqual(mock_sync.call_count, 2)

    @patch("apps.integrations.whatsapp.webhook_service.process_inbound_message.delay")
    def test_whatsapp_duplicate_webhook_stamps_without_poll(self, mock_delay):
        parsed = ParsedInboundMessage(
            phone_number_id="7794189252778687",
            wa_id="385981112223",
            wamid="wamid.obs.1",
            message_type="text",
            body="Hello WA",
            profile_name="Guest",
            raw_message={"id": "wamid.obs.1", "type": "text"},
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
        self.assertEqual(WhatsAppMessage.objects.filter(wamid="wamid.obs.1").count(), 1)
        mock_delay.assert_called_once()
        block = conversation_ingest_snapshot()["channels"]["whatsapp"]
        self.assertIsNotNone(block["last_webhook_at"])
        self.assertIsNone(block["last_poll_at"])

    def test_whatsapp_missing_wamid_does_not_stamp(self):
        parsed = ParsedInboundMessage(
            phone_number_id="7794189252778687",
            wa_id="385981112223",
            wamid="",
            message_type="text",
            body="Hello WA",
            profile_name="Guest",
            raw_message={"id": "", "type": "text"},
        )
        result = record_inbound_whatsapp_message(
            integration_row=self.whatsapp,
            parsed=parsed,
        )
        self.assertEqual(result["status"], "ignored")
        block = conversation_ingest_snapshot()["channels"]["whatsapp"]
        self.assertIsNone(block["last_webhook_at"])

    @patch("apps.communications.guest_email_ingest._connect_imap")
    def test_email_imap_select_stamps_last_poll(self, mock_connect):
        client = MagicMock()
        client.uid.return_value = ("OK", [b""])
        mock_connect.return_value = client
        result = poll_tenant_guest_inbox(self.tenant)
        self.assertEqual(result.ingested, 0)
        client.select.assert_called_once_with("INBOX")
        block = conversation_ingest_snapshot()["channels"]["email"]
        self.assertIsNotNone(block["last_poll_at"])
        self.assertIsNone(block["last_webhook_at"])

    @patch("apps.communications.guest_email_ingest._connect_imap")
    def test_email_imap_connect_failure_does_not_stamp(self, mock_connect):
        mock_connect.side_effect = OSError("imap down")
        result = poll_tenant_guest_inbox(self.tenant)
        self.assertEqual(result.errors, 1)
        block = conversation_ingest_snapshot()["channels"]["email"]
        self.assertIsNone(block["last_poll_at"])
