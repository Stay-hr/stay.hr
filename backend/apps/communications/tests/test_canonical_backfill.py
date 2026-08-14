"""ADR 0019 Phase D3: canonical backfill from timeline merge groups."""

from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from apps.communications.canonical_backfill import (
    BackfillCutoff,
    persist_cutoff,
    run_canonical_backfill,
)
from apps.communications.canonical_store import record_canonical_source
from apps.communications.models import (
    CanonicalConversationBackfill,
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
from apps.integrations.models import ChannexMessage, WhatsAppMessage
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant


class CanonicalBackfillTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="d3-store", name="D3 Store")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="P",
            slug="p-d3",
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
        self.body = "Ok merci du mail"
        self.now = timezone.now()

    def _channex(self, *, message_id: str, body: str | None = None, **kwargs) -> ChannexMessage:
        defaults = dict(
            tenant=self.tenant,
            reservation=self.reservation,
            channex_booking_id="bk-d3",
            channex_message_id=message_id,
            direction=ChannexMessage.Direction.INBOUND,
            sender=ChannexMessage.Sender.GUEST,
            body=body if body is not None else self.body,
        )
        defaults.update(kwargs)
        return ChannexMessage.objects.create(**defaults)

    def _inbound(self, *, message_id: str, body: str | None = None) -> GuestInboundMessage:
        return GuestInboundMessage.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            channel=GuestMessageChannel.EMAIL,
            body_text=body if body is not None else self.body,
            message_id=message_id,
            received_at=self.now,
        )

    def _apply(self, **kwargs):
        return run_canonical_backfill(self.tenant, **kwargs)

    def test_channex_then_imap_one_message_two_sources(self):
        self._channex(message_id="ch-first")
        self._inbound(message_id="imap-after")
        report = self._apply()
        self.assertEqual(report.blocking_count, 0)
        messages = GuestMessage.objects.filter(conversation__reservation=self.reservation)
        self.assertEqual(messages.count(), 1)
        message = messages.get()
        self.assertEqual(message.channel, GuestMessageChannel.BOOKING)
        self.assertEqual(message.sources.count(), 2)

    def test_imap_then_channex_one_message_channel_booking(self):
        self._inbound(message_id="imap-first")
        self._channex(message_id="ch-after")
        self._apply()
        message = GuestMessage.objects.get()
        self.assertEqual(message.channel, GuestMessageChannel.BOOKING)
        self.assertEqual(message.sources.count(), 2)

    def test_partial_d2_heals_missing_imap_source(self):
        channex = self._channex(message_id="ch-partial")
        record_canonical_source(channex)
        self.assertEqual(GuestMessage.objects.count(), 1)
        self._inbound(message_id="imap-partial")
        self._apply()
        self.assertEqual(GuestMessage.objects.count(), 1)
        self.assertEqual(GuestMessageSource.objects.count(), 2)

    def test_unrouted_whatsapp_skipped(self):
        WhatsAppMessage.objects.create(
            tenant=self.tenant,
            reservation=None,
            wamid="wamid.unrouted.d3",
            wa_id="385991111111",
            direction=WhatsAppMessage.Direction.INBOUND,
            body=self.body,
        )
        report = self._apply()
        self.assertEqual(Conversation.objects.count(), 0)
        self.assertEqual(GuestMessage.objects.count(), 0)
        self.assertGreaterEqual(report.skipped_unrouted_whatsapp, 1)

    def test_idempotent_rerun_same_pks(self):
        self._channex(message_id="ch-idemp")
        first = self._apply()
        source_ids = list(GuestMessageSource.objects.values_list("pk", flat=True))
        message_ids = list(GuestMessage.objects.values_list("pk", flat=True))
        second = self._apply()
        self.assertEqual(second.created_messages, 0)
        self.assertEqual(second.created_sources, 0)
        self.assertEqual(list(GuestMessageSource.objects.values_list("pk", flat=True)), source_ids)
        self.assertEqual(list(GuestMessage.objects.values_list("pk", flat=True)), message_ids)
        self.assertEqual(first.groups, second.groups)

    def test_dry_run_writes_nothing(self):
        self._channex(message_id="ch-dry")
        self._inbound(message_id="imap-dry")
        report = self._apply(dry_run=True)
        self.assertEqual(report.would_create_messages, 1)
        self.assertEqual(report.would_create_sources, 2)
        self.assertEqual(GuestMessage.objects.count(), 0)
        self.assertEqual(GuestMessageSource.objects.count(), 0)
        self.assertFalse(CanonicalConversationBackfill.objects.filter(tenant=self.tenant).exists())

    def test_split_canonical_no_hide(self):
        channex = self._channex(message_id="ch-split")
        inbound = self._inbound(message_id="imap-split")
        conversation = Conversation.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
        )
        first = GuestMessage.objects.create(
            tenant=self.tenant,
            conversation=conversation,
            direction=GuestMessageDirection.INBOUND,
            channel=GuestMessageChannel.BOOKING,
            body=self.body,
            occurred_at=self.now,
        )
        second = GuestMessage.objects.create(
            tenant=self.tenant,
            conversation=conversation,
            direction=GuestMessageDirection.INBOUND,
            channel=GuestMessageChannel.EMAIL,
            body=self.body,
            occurred_at=self.now,
        )
        GuestMessageSource.objects.create(
            tenant=self.tenant,
            message=first,
            provider=GuestMessageSourceProvider.CHANNEX,
            provider_message_id="ch-split",
            channex_message=channex,
        )
        GuestMessageSource.objects.create(
            tenant=self.tenant,
            message=second,
            provider=GuestMessageSourceProvider.IMAP,
            provider_message_id="imap-split",
            inbound_message=inbound,
        )
        report = self._apply()
        codes = {item["code"] for item in report.anomalies["blocking"]}
        self.assertIn("split_canonical", codes)
        self.assertEqual(GuestMessage.objects.filter(is_visible=True).count(), 2)

    def test_outbound_whatsapp_mirror_two_sources(self):
        outbound = GuestOutboundMessage.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            channel=GuestMessageChannel.WHATSAPP,
            body_text=self.body,
            status=GuestOutboundMessageStatus.SENT,
        )
        wa = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            wamid="wamid.out.d3",
            wa_id="385991234567",
            direction=WhatsAppMessage.Direction.OUTBOUND,
            body=self.body,
        )
        WhatsAppMessage.objects.filter(pk=wa.pk).update(created_at=outbound.created_at)
        wa.refresh_from_db()
        report = self._apply()
        self.assertEqual(report.blocking_count, 0)
        self.assertEqual(GuestMessage.objects.count(), 1)
        message = GuestMessage.objects.get()
        self.assertEqual(message.sources.count(), 2)
        self.assertTrue(message.sources.filter(outbound_message=outbound).exists())
        self.assertTrue(message.sources.filter(whatsapp_message=wa).exists())

    def test_raw_after_cutoff_excluded(self):
        persist_cutoff(
            self.tenant,
            BackfillCutoff(
                at=timezone.now(),
                channex_id=0,
                whatsapp_id=0,
                inbound_id=0,
                outbound_id=0,
            ),
        )
        self._channex(message_id="ch-after-cutoff")
        report = self._apply()
        self.assertEqual(GuestMessageSource.objects.count(), 0)
        self.assertGreaterEqual(report.raw_after_cutoff, 1)

    def test_mark_complete_rejected_with_scope_flags(self):
        with self.assertRaises(CommandError):
            call_command(
                "backfill_canonical_guest_messages",
                tenant_slug="d3-store",
                mark_complete=True,
                reservation_id=self.reservation.pk,
            )
        with self.assertRaises(CommandError):
            call_command(
                "backfill_canonical_guest_messages",
                tenant_slug="d3-store",
                mark_complete=True,
                resume_after_reservation_id=0,
            )
        with self.assertRaises(CommandError):
            call_command(
                "backfill_canonical_guest_messages",
                tenant_slug="d3-store",
                mark_complete=True,
                dry_run=True,
            )

    def test_cross_group_canonical_is_blocking(self):
        ch_a = self._channex(message_id="ch-a", body="First distinct group body")
        ch_b = self._channex(message_id="ch-b", body="Second distinct group body here")
        conversation = Conversation.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
        )
        message = GuestMessage.objects.create(
            tenant=self.tenant,
            conversation=conversation,
            direction=GuestMessageDirection.INBOUND,
            channel=GuestMessageChannel.BOOKING,
            body="shared",
            occurred_at=self.now,
        )
        GuestMessageSource.objects.create(
            tenant=self.tenant,
            message=message,
            provider=GuestMessageSourceProvider.CHANNEX,
            provider_message_id="ch-a",
            channex_message=ch_a,
        )
        GuestMessageSource.objects.create(
            tenant=self.tenant,
            message=message,
            provider=GuestMessageSourceProvider.CHANNEX,
            provider_message_id="ch-b",
            channex_message=ch_b,
        )
        report = self._apply()
        codes = {item["code"] for item in report.anomalies["blocking"]}
        self.assertIn("cross_group_canonical", codes)

    def test_invisible_message_not_unhidden(self):
        channex = self._channex(message_id="ch-hidden")
        conversation = Conversation.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
        )
        message = GuestMessage.objects.create(
            tenant=self.tenant,
            conversation=conversation,
            direction=GuestMessageDirection.INBOUND,
            channel=GuestMessageChannel.BOOKING,
            body=self.body,
            occurred_at=self.now,
            is_visible=False,
        )
        GuestMessageSource.objects.create(
            tenant=self.tenant,
            message=message,
            provider=GuestMessageSourceProvider.CHANNEX,
            provider_message_id="ch-hidden",
            channex_message=channex,
        )
        report = self._apply()
        codes = {item["code"] for item in report.anomalies["blocking"]}
        self.assertIn("group_message_invisible", codes)
        message.refresh_from_db()
        self.assertFalse(message.is_visible)

    def test_provider_identity_conflict_fail_closed(self):
        raw_a = self._channex(message_id="ch-conflict-a", body="Conflict body A long enough")
        raw_b = self._channex(message_id="ch-conflict-b", body="Conflict body B long enough")
        conversation = Conversation.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
        )
        message = GuestMessage.objects.create(
            tenant=self.tenant,
            conversation=conversation,
            direction=GuestMessageDirection.INBOUND,
            channel=GuestMessageChannel.BOOKING,
            body="x",
            occurred_at=self.now,
        )
        GuestMessageSource.objects.create(
            tenant=self.tenant,
            message=message,
            provider=GuestMessageSourceProvider.CHANNEX,
            provider_message_id=None,
            channex_message=raw_a,
        )
        other = GuestMessage.objects.create(
            tenant=self.tenant,
            conversation=conversation,
            direction=GuestMessageDirection.INBOUND,
            channel=GuestMessageChannel.BOOKING,
            body="y",
            occurred_at=self.now,
        )
        GuestMessageSource.objects.create(
            tenant=self.tenant,
            message=other,
            provider=GuestMessageSourceProvider.CHANNEX,
            provider_message_id="ch-conflict-a",
            channex_message=raw_b,
        )
        report = self._apply()
        codes = {item["code"] for item in report.anomalies["blocking"]}
        self.assertIn("provider_identity_conflict", codes)

    def test_mark_complete_after_clean_apply(self):
        self._channex(message_id="ch-complete")
        self._apply()
        out = StringIO()
        call_command(
            "backfill_canonical_guest_messages",
            tenant_slug="d3-store",
            mark_complete=True,
            stdout=out,
        )
        row = CanonicalConversationBackfill.objects.get(tenant=self.tenant)
        self.assertIsNotNone(row.completed_at)
        self.assertIsNotNone(row.cutoff_channex_id)
