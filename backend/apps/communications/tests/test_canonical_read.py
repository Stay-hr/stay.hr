"""ADR 0019 Phase D4: tenant-gated canonical GET and inbox."""

from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.communications.canonical_backfill import run_canonical_backfill
from apps.communications.canonical_read import (
    compare_timeline_parity,
    disable_canonical_read,
    enable_canonical_read,
    tenant_reads_canonical,
    validate_canonical_read,
)
from apps.communications.guest_message_timeline import (
    WA_ID_OFFSET,
    raw_timeline_for_reservation,
    timeline_for_reservation,
)
from apps.communications.guest_message_translate import translate_guest_message
from apps.communications.message_threads_service import list_message_threads_for_tenant
from apps.communications.models import (
    CanonicalConversationBackfill,
    Conversation,
    GuestInboundMessage,
    GuestMessage,
    GuestMessageChannel,
    GuestMessageDirection,
    GuestMessageSource,
    GuestMessageSourceProvider,
    GuestMessageThreadState,
    GuestOutboundMessage,
    GuestOutboundMessageStatus,
)
from apps.integrations.models import ChannexMessage, WhatsAppMessage
from apps.properties.models import Property
from apps.reservations.models import (
    DocumentIntakeJob,
    DocumentIntakeJobSource,
    DocumentIntakeJobStatus,
    Reservation,
)
from apps.tenants.models import ApiApplication, Tenant


def _without_canonical_id(items: list[dict]) -> list[dict]:
    return [{k: v for k, v in item.items() if k != "canonical_id"} for item in items]


class CanonicalReadTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="d4-store", name="D4 Store")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="P",
            slug="p-d4",
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
        self.app, _token = ApiApplication.create_with_token(
            tenant=self.tenant,
            name="Reception tablet",
            scopes=["reception:read"],
        )

    def _reservation(self, *, slug_suffix: str, booker: str) -> Reservation:
        today = timezone.localdate()
        return Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booker_name=booker,
            booking_code=f"BK-{slug_suffix}",
            check_in=today,
            check_out=today + timedelta(days=1),
            status=Reservation.Status.EXPECTED,
        )

    def _channex(self, reservation=None, **kwargs) -> ChannexMessage:
        reservation = reservation or self.reservation
        defaults = dict(
            tenant=self.tenant,
            reservation=reservation,
            channex_booking_id="bk-d4",
            channex_message_id=kwargs.pop("message_id", "ch-d4"),
            direction=ChannexMessage.Direction.INBOUND,
            sender=ChannexMessage.Sender.GUEST,
            body=kwargs.pop("body", self.body),
        )
        defaults.update(kwargs)
        return ChannexMessage.objects.create(**defaults)

    def _inbound(self, reservation=None, **kwargs) -> GuestInboundMessage:
        reservation = reservation or self.reservation
        return GuestInboundMessage.objects.create(
            tenant=self.tenant,
            reservation=reservation,
            channel=GuestMessageChannel.EMAIL,
            body_text=kwargs.pop("body", self.body),
            message_id=kwargs.pop("message_id", "imap-d4"),
            from_email=kwargs.pop("from_email", "guest@guest.booking.com"),
            received_at=self.now,
            **kwargs,
        )

    def _complete(self):
        run_canonical_backfill(self.tenant)
        run_canonical_backfill(self.tenant, mark_complete=True)

    def _enable(self):
        return enable_canonical_read(self.tenant)

    def _assert_parity(self, reservation=None):
        reservation = reservation or self.reservation
        raw = raw_timeline_for_reservation(reservation)
        canonical = timeline_for_reservation(reservation, read_canonical=True)
        self.assertTrue(all("canonical_id" in item for item in canonical))
        self.assertEqual(_without_canonical_id(raw), _without_canonical_id(canonical))

    def test_flag_off_raw_timeline_has_no_canonical_id(self):
        self._channex()
        timeline = timeline_for_reservation(self.reservation)
        self.assertEqual(len(timeline), 1)
        self.assertNotIn("canonical_id", timeline[0])
        self.assertFalse(tenant_reads_canonical(self.tenant))

    def test_completed_without_flag_stays_raw(self):
        self._channex()
        self._complete()
        self.assertIsNotNone(
            CanonicalConversationBackfill.objects.get(tenant=self.tenant).completed_at
        )
        timeline = timeline_for_reservation(self.reservation)
        self.assertNotIn("canonical_id", timeline[0])
        self.assertFalse(tenant_reads_canonical(self.tenant))

    def test_enable_without_complete_errors(self):
        self._channex()
        with self.assertRaises(CommandError):
            call_command(
                "set_canonical_guest_message_read",
                tenant_slug="d4-store",
                enable=True,
            )
        self.assertFalse(tenant_reads_canonical(self.tenant))

    def test_enable_blocked_keeps_flag_off(self):
        self._channex()
        self._complete()
        conversation = Conversation.objects.get(reservation=self.reservation)
        GuestMessage.objects.create(
            tenant=self.tenant,
            conversation=conversation,
            direction=GuestMessageDirection.INBOUND,
            channel=GuestMessageChannel.EMAIL,
            body="orphan visible row",
            occurred_at=self.now,
            is_visible=True,
        )
        with self.assertRaises(ValueError):
            enable_canonical_read(self.tenant)
        self.assertFalse(tenant_reads_canonical(self.tenant))
        row = CanonicalConversationBackfill.objects.get(tenant=self.tenant)
        self.assertIsNone(row.read_canonical_at)
        self.assertIsNotNone(row.completed_at)

    def test_disable_clears_only_read_fields(self):
        self._channex()
        self._complete()
        self._enable()
        row = CanonicalConversationBackfill.objects.get(tenant=self.tenant)
        completed_at = row.completed_at
        cutoff = row.cutoff_channex_id
        disable_canonical_read(self.tenant)
        row.refresh_from_db()
        self.assertIsNone(row.read_canonical_at)
        self.assertEqual(row.read_canonical_by, "")
        self.assertEqual(row.read_snapshot, {})
        self.assertEqual(row.completed_at, completed_at)
        self.assertEqual(row.cutoff_channex_id, cutoff)
        self.assertNotIn("canonical_id", timeline_for_reservation(self.reservation)[0])

    def test_channex_imap_parity(self):
        self._channex(message_id="ch-merge")
        self._inbound(message_id="imap-merge")
        self._complete()
        self._enable()
        self._assert_parity()
        item = timeline_for_reservation(self.reservation)[0]
        self.assertEqual(item["source"], "booking")
        self.assertEqual(sorted(item["channels"]), ["booking", "email"])
        self.assertEqual(item["canonical_id"], GuestMessage.objects.get().pk)

    def test_outbound_whatsapp_mirror_primary_is_whatsapp(self):
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
            wamid="wamid.out.d4",
            wa_id="385991234567",
            direction=WhatsAppMessage.Direction.OUTBOUND,
            body=self.body,
        )
        WhatsAppMessage.objects.filter(pk=wa.pk).update(created_at=outbound.created_at)
        self._complete()
        self._enable()
        self._assert_parity()
        item = timeline_for_reservation(self.reservation)[0]
        self.assertEqual(item["source"], "whatsapp")
        self.assertEqual(item["id"], WA_ID_OFFSET + wa.pk)
        self.assertEqual(item["channels"], ["whatsapp"])

    def test_email_inbound_from_email(self):
        self._inbound(message_id="imap-only", from_email="a@b.com")
        self._complete()
        self._enable()
        self._assert_parity()
        self.assertEqual(timeline_for_reservation(self.reservation)[0]["from_email"], "a@b.com")

    def test_outbound_email_sent_by_name(self):
        GuestOutboundMessage.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            channel=GuestMessageChannel.EMAIL,
            body_text=self.body,
            status=GuestOutboundMessageStatus.SENT,
            api_application=self.app,
        )
        self._complete()
        self._enable()
        self._assert_parity()
        self.assertEqual(
            timeline_for_reservation(self.reservation)[0]["sent_by_name"],
            "Reception tablet",
        )

    def test_whatsapp_inbound(self):
        WhatsAppMessage.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            wamid="wamid.in.d4",
            wa_id="385991234567",
            direction=WhatsAppMessage.Direction.INBOUND,
            body=self.body,
        )
        self._complete()
        self._enable()
        self._assert_parity()

    def test_channex_attachment_media(self):
        self._channex(message_id="ch-media", body="", have_attachment=True)
        self._complete()
        self._enable()
        self._assert_parity()
        item = timeline_for_reservation(self.reservation)[0]
        self.assertEqual(item["message_type"], "image")

    def test_failed_outbound_status(self):
        GuestOutboundMessage.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            channel=GuestMessageChannel.EMAIL,
            body_text=self.body,
            status=GuestOutboundMessageStatus.FAILED,
            error_message="smtp boom",
        )
        self._complete()
        self._enable()
        self._assert_parity()
        self.assertEqual(timeline_for_reservation(self.reservation)[0]["status"], "failed")

    def test_document_intake_job_id(self):
        wa = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            wamid="wamid.img.d4",
            wa_id="385991234567",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="image",
            body="",
        )
        job = DocumentIntakeJob.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            whatsapp_message=wa,
            source=DocumentIntakeJobSource.WHATSAPP,
            status=DocumentIntakeJobStatus.DONE,
        )
        self._complete()
        self._enable()
        self._assert_parity()
        item = timeline_for_reservation(self.reservation)[0]
        self.assertEqual(item["document_intake_job_id"], job.pk)

    def test_invisible_message_excluded(self):
        channex = self._channex(message_id="ch-hidden")
        self._complete()
        message = GuestMessage.objects.get()
        message.is_visible = False
        message.save(update_fields=["is_visible"])
        GuestMessageSource.objects.filter(channex_message=channex).update(message=message)
        enabled_timeline = timeline_for_reservation(self.reservation, read_canonical=True)
        self.assertEqual(enabled_timeline, [])

    def test_unrouted_whatsapp_absent(self):
        WhatsAppMessage.objects.create(
            tenant=self.tenant,
            reservation=None,
            wamid="wamid.unrouted.d4",
            wa_id="385991111111",
            direction=WhatsAppMessage.Direction.INBOUND,
            body=self.body,
        )
        self._complete()
        self._enable()
        self.assertEqual(timeline_for_reservation(self.reservation), [])

    def test_translate_uses_synthetic_id_after_enable(self):
        wa = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            wamid="wamid.tr.d4",
            wa_id="385991234567",
            direction=WhatsAppMessage.Direction.INBOUND,
            body="Hello there friend",
        )
        self._complete()
        self._enable()
        from unittest.mock import patch

        with patch(
            "apps.communications.guest_message_translate.translation_available",
            return_value=True,
        ), patch(
            "apps.communications.guest_message_translate.translate_text",
            return_value="Bok",
        ):
            result = translate_guest_message(
                reservation=self.reservation,
                timeline_id=WA_ID_OFFSET + wa.pk,
                target_lang="hr",
            )
        self.assertEqual(result["translated"], "Bok")

    def test_inbox_contract_parity(self):
        other = self._reservation(slug_suffix="2", booker="Other")
        self._channex(message_id="ch-one")
        self._inbound(reservation=other, message_id="imap-two", body="Second thread body")
        GuestMessageThreadState.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            reply_dismissed_at=timezone.now(),
        )
        self._complete()
        raw_page, raw_total, raw_nrc = list_message_threads_for_tenant(
            self.tenant, page=1, page_size=1, read_canonical=False
        )
        self._enable()
        canon_page, canon_total, canon_nrc = list_message_threads_for_tenant(
            self.tenant, page=1, page_size=1, read_canonical=True
        )
        self.assertEqual(raw_total, canon_total)
        self.assertEqual(raw_nrc, canon_nrc)
        self.assertEqual(
            [row["reservation_id"] for row in raw_page],
            [row["reservation_id"] for row in canon_page],
        )
        for raw_row, canon_row in zip(raw_page, canon_page):
            self.assertEqual(raw_row, canon_row)
        filtered_raw, *_ = list_message_threads_for_tenant(
            self.tenant, needs_reply_only=True, read_canonical=False
        )
        filtered_canon, *_ = list_message_threads_for_tenant(
            self.tenant, needs_reply_only=True, read_canonical=True
        )
        self.assertEqual(filtered_raw, filtered_canon)

    def test_inbox_query_count_does_not_scale_per_thread(self):
        for index in range(6):
            reservation = self._reservation(slug_suffix=f"q{index}", booker=f"G{index}")
            self._channex(
                reservation=reservation,
                message_id=f"ch-q{index}",
                body=f"{self.body} {index}",
            )
        self._complete()
        self._enable()
        with CaptureQueriesContext(connection) as ctx:
            list_message_threads_for_tenant(self.tenant, page=1, page_size=25)
        read_flag_queries = [
            query["sql"]
            for query in ctx.captured_queries
            if "read_canonical_at" in query["sql"]
        ]
        self.assertLessEqual(len(read_flag_queries), 1)
        self.assertLess(len(ctx.captured_queries), 25)

    def test_command_status_and_enable_disable(self):
        self._channex()
        self._complete()
        out = StringIO()
        call_command(
            "set_canonical_guest_message_read",
            tenant_slug="d4-store",
            enable=True,
            stdout=out,
        )
        self.assertIn("canonical-read-enabled", out.getvalue())
        self.assertTrue(tenant_reads_canonical(self.tenant))
        row = CanonicalConversationBackfill.objects.get(tenant=self.tenant)
        self.assertIsNotNone(row.read_canonical_at)
        self.assertTrue(row.read_snapshot)
        call_command(
            "set_canonical_guest_message_read",
            tenant_slug="d4-store",
            disable=True,
            stdout=StringIO(),
        )
        row.refresh_from_db()
        self.assertIsNone(row.read_canonical_at)
        self.assertIsNotNone(row.completed_at)

    def test_validate_and_parity_commands_are_read_only(self):
        self._channex()
        self._complete()
        before = CanonicalConversationBackfill.objects.get(tenant=self.tenant)
        call_command(
            "set_canonical_guest_message_read",
            tenant_slug="d4-store",
            validate=True,
            stdout=StringIO(),
        )
        call_command(
            "set_canonical_guest_message_read",
            tenant_slug="d4-store",
            parity=True,
            stdout=StringIO(),
        )
        after = CanonicalConversationBackfill.objects.get(tenant=self.tenant)
        self.assertEqual(before.read_canonical_at, after.read_canonical_at)
        self.assertEqual(compare_timeline_parity(self.tenant)["blocking_count"], 0)
        self.assertEqual(validate_canonical_read(self.tenant)["blocking_count"], 0)
