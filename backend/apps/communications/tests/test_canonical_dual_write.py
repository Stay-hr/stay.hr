"""ADR 0019 Phase D2: dual-write identity, heuristic merge, and transaction guardrails."""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.communications import canonical_store as canonical_store_mod
from apps.communications.canonical_store import (
    CanonicalSourceConversationMismatch,
    create_with_canonical,
    link_raw_reservation,
    record_canonical_source,
)
from apps.communications.guest_email_ingest import ParsedGuestEmail, ingest_parsed_email
from apps.communications.models import (
    Conversation,
    GuestInboundMessage,
    GuestMessage,
    GuestMessageChannel,
    GuestMessageDirection,
    GuestMessageSource,
    GuestMessageSourceProvider,
    GuestOutboundMessage,
    GuestOutboundMessageStatus,
)
from apps.integrations.channex.message_service import upsert_channex_message_from_payload
from apps.integrations.models import ChannexMessage, IntegrationConfig, WhatsAppMessage
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant


class CanonicalDualWriteTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="d2-store", name="D2 Store")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="P",
            slug="p-d2",
        )
        today = timezone.localdate()
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booker_name="Guest",
            booking_code="5238895494",
            check_in=today,
            check_out=today + timedelta(days=1),
            status=Reservation.Status.EXPECTED,
        )
        self.integration = IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.CHANNEX,
            is_active=True,
        )
        self.body = "Ok merci du mail"
        self.now = timezone.now()

    def _channex_orm(self, *, message_id: str, **kwargs) -> ChannexMessage:
        defaults = dict(
            tenant=self.tenant,
            reservation=self.reservation,
            channex_booking_id="bk-d2",
            channex_message_id=message_id,
            direction=ChannexMessage.Direction.INBOUND,
            sender=ChannexMessage.Sender.GUEST,
            body=self.body,
        )
        defaults.update(kwargs)
        return ChannexMessage.objects.create(**defaults)

    def _parsed(self, *, message_id: str, body: str | None = None) -> ParsedGuestEmail:
        return ParsedGuestEmail(
            message_id=message_id,
            raw_from="Guest <guest@guest.booking.com>",
            from_email="guest@guest.booking.com",
            subject="Reservation 5238895494",
            body_text=body if body is not None else self.body,
            booking_code=self.reservation.booking_code,
            received_at=self.now,
        )

    def _upsert_channex(self, *, message_id: str, body: str | None = None, **kwargs):
        payload = {
            "id": message_id,
            "message": body if body is not None else self.body,
            "sender": "guest",
            "booking_id": "bk-d2",
        }
        payload.update(kwargs)
        return upsert_channex_message_from_payload(
            tenant=self.tenant,
            integration=self.integration,
            payload=payload,
            reservation=self.reservation,
        )

    def test_existing_raw_without_source_is_healed_on_reupsert(self):
        raw = self._channex_orm(message_id="heal-channex")
        self.assertEqual(GuestMessageSource.objects.count(), 0)

        row, created = self._upsert_channex(message_id="heal-channex")
        self.assertFalse(created)
        self.assertEqual(row.pk, raw.pk)
        self.assertEqual(ChannexMessage.objects.filter(channex_message_id="heal-channex").count(), 1)
        source = GuestMessageSource.objects.get(channex_message=raw)
        self.assertEqual(source.provider, GuestMessageSourceProvider.CHANNEX)
        self.assertEqual(source.provider_message_id, "heal-channex")
        self.assertEqual(source.tenant_id, self.reservation.tenant_id)

    @patch("apps.core.tasks.notify_guest_message_inbound.delay")
    def test_imap_then_channex_one_message_channel_booking(self, _notify):
        inbound = ingest_parsed_email(self.tenant, self._parsed(message_id="imap-first"))
        self.assertIsNotNone(inbound)
        self._upsert_channex(message_id="channex-after-imap")

        messages = GuestMessage.objects.filter(
            conversation__reservation=self.reservation,
            direction=GuestMessageDirection.INBOUND,
        )
        self.assertEqual(messages.count(), 1)
        message = messages.get()
        self.assertEqual(message.channel, GuestMessageChannel.BOOKING)
        self.assertEqual(message.sources.count(), 2)
        self.assertTrue(message.sources.filter(inbound_message=inbound).exists())
        self.assertTrue(message.sources.filter(channex_message__channex_message_id="channex-after-imap").exists())

    @patch("apps.core.tasks.notify_guest_message_inbound.delay")
    def test_channex_then_imap_one_message_channel_booking(self, _notify):
        self._upsert_channex(message_id="channex-first")
        inbound = ingest_parsed_email(self.tenant, self._parsed(message_id="imap-after-channex"))
        self.assertIsNotNone(inbound)

        messages = GuestMessage.objects.filter(
            conversation__reservation=self.reservation,
            direction=GuestMessageDirection.INBOUND,
        )
        self.assertEqual(messages.count(), 1)
        message = messages.get()
        self.assertEqual(message.channel, GuestMessageChannel.BOOKING)
        self.assertEqual(message.sources.count(), 2)

    def test_parallel_same_provider_id_one_message_one_source(self):
        raw = self._channex_orm(message_id="race-id")
        conversation = Conversation.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
        )
        existing = GuestMessage.objects.create(
            tenant=self.tenant,
            conversation=conversation,
            direction=GuestMessageDirection.INBOUND,
            channel=GuestMessageChannel.BOOKING,
            body=self.body,
            occurred_at=self.now,
        )
        GuestMessageSource.objects.create(
            tenant=self.tenant,
            message=existing,
            provider=GuestMessageSourceProvider.CHANNEX,
            provider_message_id="race-id",
            channex_message=raw,
        )
        original_lookup = canonical_store_mod._lookup_source
        calls = {"n": 0}

        def lookup_miss_once(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return None
            return original_lookup(*args, **kwargs)

        with patch.object(canonical_store_mod, "_lookup_source", side_effect=lookup_miss_once):
            result = record_canonical_source(raw)

        self.assertEqual(result.pk, existing.pk)
        self.assertEqual(GuestMessage.objects.filter(conversation=conversation).count(), 1)
        self.assertEqual(GuestMessageSource.objects.filter(channex_message=raw).count(), 1)

    def test_source_in_other_conversation_fails_closed_and_rolls_back_relink(self):
        other = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booker_name="Other",
            booking_code="999000111",
            check_in=self.reservation.check_in,
            check_out=self.reservation.check_out,
            status=Reservation.Status.EXPECTED,
        )
        raw = self._channex_orm(message_id="mismatch-id")
        record_canonical_source(raw)
        source_id = GuestMessageSource.objects.get(channex_message=raw).pk

        with self.assertLogs("apps.communications.canonical_store", level="ERROR") as logs:
            with self.assertRaises(CanonicalSourceConversationMismatch):
                link_raw_reservation(raw, other)

        self.assertTrue(
            any("canonical_source_conversation_mismatch" in line for line in logs.output)
        )
        raw.refresh_from_db()
        self.assertEqual(raw.reservation_id, self.reservation.pk)
        self.assertFalse(Conversation.objects.filter(reservation=other).exists())
        source = GuestMessageSource.objects.get(pk=source_id)
        self.assertEqual(source.message.conversation.reservation_id, self.reservation.pk)

    def test_canonical_failure_rolls_back_new_raw_write(self):
        with patch(
            "apps.integrations.channex.message_service.record_canonical_source",
            side_effect=RuntimeError("canonical boom"),
        ):
            with self.assertRaises(RuntimeError):
                self._upsert_channex(message_id="d2-rollback")
        self.assertFalse(ChannexMessage.objects.filter(channex_message_id="d2-rollback").exists())
        self.assertEqual(GuestMessage.objects.count(), 0)
        self.assertEqual(GuestMessageSource.objects.count(), 0)

    def test_helper_hit_does_not_touch_or_notify(self):
        row, created = self._upsert_channex(message_id="no-touch")
        self.assertTrue(created)
        with (
            patch("apps.integrations.channex.message_service.touch_reservation_version") as touch,
            patch("apps.core.tasks.notify_guest_message_inbound.delay") as notify,
        ):
            again, created_again = self._upsert_channex(message_id="no-touch")
            self.assertFalse(created_again)
            self.assertEqual(again.pk, row.pk)
            touch.assert_not_called()
            notify.assert_not_called()
            record_canonical_source(row)
            touch.assert_not_called()
            notify.assert_not_called()
        self.assertEqual(GuestMessageSource.objects.filter(channex_message=row).count(), 1)

    def test_unrouted_whatsapp_skips_canonical(self):
        wa = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            reservation=None,
            wamid="wamid.unrouted.d2",
            wa_id="385991111111",
            direction=WhatsAppMessage.Direction.INBOUND,
            body=self.body,
        )
        self.assertIsNone(record_canonical_source(wa))
        self.assertEqual(Conversation.objects.count(), 0)
        self.assertEqual(GuestMessage.objects.count(), 0)
        self.assertEqual(GuestMessageSource.objects.count(), 0)

    def test_synthetic_provider_id_stored_as_null(self):
        raw = self._channex_orm(message_id="local-outbound:1:99")
        message = record_canonical_source(raw)
        source = message.sources.get()
        self.assertIsNone(source.provider_message_id)
        self.assertEqual(source.channex_message_id, raw.pk)

    def test_outbound_and_whatsapp_heuristic_one_message(self):
        outbound = create_with_canonical(
            GuestOutboundMessage,
            tenant=self.tenant,
            reservation=self.reservation,
            channel=GuestMessageChannel.WHATSAPP,
            body_text=self.body,
            status=GuestOutboundMessageStatus.SENT,
        )
        wa = create_with_canonical(
            WhatsAppMessage,
            tenant=self.tenant,
            reservation=self.reservation,
            wamid="wamid.out.d2",
            wa_id="385991234567",
            direction=WhatsAppMessage.Direction.OUTBOUND,
            body=self.body,
        )
        messages = GuestMessage.objects.filter(conversation__reservation=self.reservation)
        self.assertEqual(messages.count(), 1)
        message = messages.get()
        self.assertEqual(message.direction, GuestMessageDirection.OUTBOUND)
        self.assertEqual(message.channel, GuestMessageChannel.WHATSAPP)
        self.assertEqual(message.sources.count(), 2)
        self.assertTrue(message.sources.filter(outbound_message=outbound).exists())
        self.assertTrue(message.sources.filter(whatsapp_message=wa).exists())

    def test_canonical_tenant_follows_reservation_not_waba_tenant(self):
        platform = Tenant.objects.create(
            slug="d2-platform",
            name="Platform WABA",
            is_system=True,
        )
        wa = WhatsAppMessage.objects.create(
            tenant=platform,
            reservation=self.reservation,
            wamid="wamid.cross.tenant",
            wa_id="385998888888",
            direction=WhatsAppMessage.Direction.INBOUND,
            body=self.body,
        )
        message = record_canonical_source(wa)
        self.assertEqual(message.tenant_id, self.reservation.tenant_id)
        self.assertEqual(message.conversation.tenant_id, self.reservation.tenant_id)
        source = message.sources.get()
        self.assertEqual(source.tenant_id, self.reservation.tenant_id)
        self.assertNotEqual(source.tenant_id, platform.pk)

    @patch("apps.core.tasks.notify_guest_message_inbound.delay")
    def test_imap_duplicate_still_returns_none_after_heal(self, _notify):
        inbound = GuestInboundMessage.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            channel=GuestMessageChannel.EMAIL,
            body_text=self.body,
            message_id="imap-heal",
            received_at=self.now,
        )
        second = ingest_parsed_email(self.tenant, self._parsed(message_id="imap-heal"))
        self.assertIsNone(second)
        self.assertEqual(GuestInboundMessage.objects.count(), 1)
        self.assertEqual(GuestMessageSource.objects.filter(inbound_message=inbound).count(), 1)

    def test_link_unlinked_raw_creates_canonical(self):
        raw = self._channex_orm(message_id="relink-d2", reservation=None)
        self.assertIsNone(record_canonical_source(raw))
        link_raw_reservation(raw, self.reservation)
        raw.refresh_from_db()
        self.assertEqual(raw.reservation_id, self.reservation.pk)
        self.assertEqual(GuestMessageSource.objects.filter(channex_message=raw).count(), 1)
