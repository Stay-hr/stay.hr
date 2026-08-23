from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.communications.guest_message_timeline import (
    merge_timeline_duplicates,
    timeline_for_reservation,
    whatsapp_display_body,
)
from apps.communications.models import (
    GuestInboundMessage,
    GuestMessageChannel,
    GuestMessageDraft,
    GuestMessageIntent,
    GuestOutboundMessage,
    GuestOutboundMessageStatus,
)
from apps.integrations.models import ChannexMessage, IntegrationConfig, WhatsAppMessage
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant


class WhatsAppDisplayBodyTests(TestCase):
    def test_image_without_body(self):
        class Row:
            body = ""
            message_type = "image"

        self.assertEqual(whatsapp_display_body(Row()), "📷 Dokument poslan")

    def test_serialize_whatsapp_includes_media_url_for_intake_job(self):
        from apps.communications.guest_message_timeline import serialize_whatsapp
        from apps.reservations.models import DocumentIntakeJob, DocumentIntakeJobSource, DocumentIntakeJobStatus

        tenant = Tenant.objects.create(slug="media-test", name="Media Test")
        prop = Property.objects.create(tenant=tenant, name="P", slug="p")
        reservation = Reservation.objects.create(
            tenant=tenant,
            property=prop,
            booker_name="Guest",
            check_in=timezone.localdate(),
            check_out=timezone.localdate() + timedelta(days=1),
            status=Reservation.Status.EXPECTED,
        )
        integration = IntegrationConfig.objects.create(
            tenant=tenant,
            provider=IntegrationConfig.Provider.WHATSAPP,
            routing_key="123",
            is_active=True,
        )
        wa = WhatsAppMessage.objects.create(
            tenant_id=tenant.pk,
            integration=integration,
            reservation=reservation,
            wamid="wamid.media.test",
            wa_id="385991234567",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="image",
            body="",
        )
        job = DocumentIntakeJob.objects.create(
            tenant_id=tenant.pk,
            reservation=reservation,
            whatsapp_message=wa,
            source=DocumentIntakeJobSource.WHATSAPP,
            status=DocumentIntakeJobStatus.DONE,
        )
        payload = serialize_whatsapp(wa)
        self.assertEqual(payload["document_intake_job_id"], job.pk)
        self.assertIn(f"/document-intake/jobs/{job.pk}/images/0/", payload["media_url"])
        self.assertEqual(payload["media_kind"], "image")

    def test_text_body_preserved(self):
        class Row:
            body = "Bok"
            message_type = "text"

        self.assertEqual(whatsapp_display_body(Row()), "Bok")

    def test_reaction_uses_payload_emoji(self):
        class Row:
            body = ""
            message_type = "reaction"
            raw_payload = {
                "type": "reaction",
                "reaction": {"emoji": "👍", "message_id": "wamid.x"},
            }

        self.assertEqual(whatsapp_display_body(Row()), "👍")

    def test_reaction_without_emoji_uses_neutral_label(self):
        class Row:
            body = ""
            message_type = "reaction"
            raw_payload = {"type": "reaction", "reaction": {}}

        self.assertEqual(whatsapp_display_body(Row()), "Reakcija")
        self.assertNotEqual(whatsapp_display_body(Row()), "Poruka (WhatsApp)")
        self.assertNotEqual(whatsapp_display_body(Row()), "👍")

    def test_serialize_whatsapp_reaction_body_is_emoji(self):
        from apps.communications.guest_message_timeline import serialize_whatsapp

        tenant = Tenant.objects.create(slug="reaction-test", name="Reaction Test")
        prop = Property.objects.create(tenant=tenant, name="P", slug="p-rx")
        reservation = Reservation.objects.create(
            tenant=tenant,
            property=prop,
            booker_name="Guest",
            check_in=timezone.localdate(),
            check_out=timezone.localdate() + timedelta(days=1),
            status=Reservation.Status.EXPECTED,
        )
        integration = IntegrationConfig.objects.create(
            tenant=tenant,
            provider=IntegrationConfig.Provider.WHATSAPP,
            routing_key="reaction-key",
            is_active=True,
        )
        wa = WhatsAppMessage.objects.create(
            tenant_id=tenant.pk,
            integration=integration,
            reservation=reservation,
            wamid="wamid.reaction.test",
            wa_id="385991234567",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="reaction",
            body="",
            raw_payload={
                "type": "reaction",
                "reaction": {"emoji": "❤️", "message_id": "wamid.out"},
            },
        )
        payload = serialize_whatsapp(wa)
        self.assertEqual(payload["message_type"], "reaction")
        self.assertEqual(payload["body_text"], "❤️")


class TimelineDedupTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="uzorita", name="Uzorita")
        self.property = Property.objects.create(tenant=self.tenant, name="Uzorita", slug="uzorita")
        self.integration = IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.WHATSAPP,
            routing_key="1068791909660300",
            is_active=True,
        )
        self.channex_integration = IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.CHANNEX,
            is_active=True,
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booker_name="Ante Vrcan",
            booker_phone="+385976713511",
            check_in=timezone.localdate(),
            check_out=timezone.localdate() + timedelta(days=2),
            status=Reservation.Status.EXPECTED,
        )

    def test_whatsapp_api_outbound_deduped_against_guest_outbound(self):
        now = timezone.now()
        body = "Hvala vam na poslanim dokumentima!"
        draft = GuestMessageDraft.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            intent=GuestMessageIntent.REPLY,
            hint="checkin ready",
            llm_body_text=body,
            final_body_text=body,
            channel=GuestMessageChannel.WHATSAPP,
            sent_at=now,
        )
        GuestOutboundMessage.objects.create(
            tenant_id=self.tenant.pk,
            reservation=self.reservation,
            draft=draft,
            channel=GuestMessageChannel.WHATSAPP,
            body_text=body,
            status=GuestOutboundMessageStatus.SENT,
            to_phone=self.reservation.booker_phone,
            created_at=now,
        )
        WhatsAppMessage.objects.create(
            tenant_id=self.tenant.pk,
            integration=self.integration,
            reservation=self.reservation,
            wamid="wamid.outbound.dedup.test",
            wa_id="385976713511",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.OUTBOUND,
            message_type="text",
            body=body,
            created_at=now,
        )

        timeline = timeline_for_reservation(self.reservation)
        thank_you = [row for row in timeline if body in row["body_text"]]

        self.assertEqual(len(thank_you), 1)
        self.assertEqual(thank_you[0]["source"], "whatsapp")
        self.assertEqual(thank_you[0]["direction"], "outbound")
        self.assertEqual(thank_you[0]["status"], "sent")
        self.assertEqual(thank_you[0]["channels"], ["whatsapp"])

    def test_channex_and_whatsapp_outbound_merged(self):
        now = timezone.now()
        body = "Poštovana Anka Dorić, hvala na poruci."
        channex = ChannexMessage.objects.create(
            tenant=self.tenant,
            integration=self.channex_integration,
            reservation=self.reservation,
            channex_booking_id="booking-843",
            channex_message_id="channex-out-843",
            direction=ChannexMessage.Direction.OUTBOUND,
            sender=ChannexMessage.Sender.PROPERTY,
            body=body,
        )
        ChannexMessage.objects.filter(pk=channex.pk).update(created_at=now)
        wa = WhatsAppMessage.objects.create(
            tenant_id=self.tenant.pk,
            integration=self.integration,
            reservation=self.reservation,
            wamid="wamid.outbound.843",
            wa_id="385976713511",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.OUTBOUND,
            message_type="text",
            body=body,
        )
        WhatsAppMessage.objects.filter(pk=wa.pk).update(created_at=now + timedelta(seconds=60))

        timeline = timeline_for_reservation(self.reservation)
        matches = [row for row in timeline if body in row["body_text"]]

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["channels"], ["booking", "whatsapp"])
        self.assertEqual(matches[0]["source"], "booking")

    def test_different_outbound_bodies_not_merged(self):
        now = timezone.now()
        channex = ChannexMessage.objects.create(
            tenant=self.tenant,
            integration=self.channex_integration,
            reservation=self.reservation,
            channex_booking_id="booking-844",
            channex_message_id="channex-out-844",
            direction=ChannexMessage.Direction.OUTBOUND,
            sender=ChannexMessage.Sender.PROPERTY,
            body="First message",
        )
        ChannexMessage.objects.filter(pk=channex.pk).update(created_at=now)
        wa = WhatsAppMessage.objects.create(
            tenant_id=self.tenant.pk,
            integration=self.integration,
            reservation=self.reservation,
            wamid="wamid.outbound.844",
            wa_id="385976713511",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.OUTBOUND,
            message_type="text",
            body="Second message",
        )
        WhatsAppMessage.objects.filter(pk=wa.pk).update(created_at=now + timedelta(seconds=30))

        timeline = timeline_for_reservation(self.reservation)
        self.assertEqual(len(timeline), 2)

    def test_inbound_email_and_channex_merged(self):
        now = timezone.now()
        body = "Hvala na odgovoru. Parking nam je potreban."
        inbound = GuestInboundMessage.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            channel=GuestMessageChannel.EMAIL,
            body_text=body,
            received_at=now,
        )
        channex = ChannexMessage.objects.create(
            tenant=self.tenant,
            integration=self.channex_integration,
            reservation=self.reservation,
            channex_booking_id="booking-845",
            channex_message_id="channex-in-845",
            direction=ChannexMessage.Direction.INBOUND,
            sender=ChannexMessage.Sender.GUEST,
            body=body,
        )
        ChannexMessage.objects.filter(pk=channex.pk).update(created_at=now + timedelta(seconds=30))

        timeline = timeline_for_reservation(self.reservation)
        matches = [row for row in timeline if body in row["body_text"]]

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["channels"], ["email", "booking"])
        self.assertEqual(matches[0]["direction"], "inbound")

    def test_inbound_subset_body_merged(self):
        now = timezone.now()
        short_body = "Parking nam je potreban."
        long_body = f"Hvala na odgovoru. {short_body}"
        GuestInboundMessage.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            channel=GuestMessageChannel.EMAIL,
            body_text=short_body,
            received_at=now,
        )
        channex = ChannexMessage.objects.create(
            tenant=self.tenant,
            integration=self.channex_integration,
            reservation=self.reservation,
            channex_booking_id="booking-846",
            channex_message_id="channex-in-846",
            direction=ChannexMessage.Direction.INBOUND,
            sender=ChannexMessage.Sender.GUEST,
            body=long_body,
        )
        ChannexMessage.objects.filter(pk=channex.pk).update(created_at=now + timedelta(seconds=20))

        timeline = timeline_for_reservation(self.reservation)
        self.assertEqual(len(timeline), 1)
        self.assertEqual(timeline[0]["channels"], ["email", "booking"])
        self.assertEqual(timeline[0]["body_text"], long_body)

    def test_inbound_email_and_channex_merged_with_mail_relay_lag(self):
        now = timezone.now()
        body = "Parking nam je potreban. Hvala"
        inbound = GuestInboundMessage.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            channel=GuestMessageChannel.EMAIL,
            body_text=body,
            received_at=now + timedelta(seconds=420),
        )
        channex = ChannexMessage.objects.create(
            tenant=self.tenant,
            integration=self.channex_integration,
            reservation=self.reservation,
            channex_booking_id="booking-847",
            channex_message_id="channex-in-847",
            direction=ChannexMessage.Direction.INBOUND,
            sender=ChannexMessage.Sender.GUEST,
            body=body,
        )
        ChannexMessage.objects.filter(pk=channex.pk).update(created_at=now)

        timeline = timeline_for_reservation(self.reservation)
        matches = [row for row in timeline if body in row["body_text"]]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["channels"], ["booking", "email"])

    def test_merge_timeline_duplicates_adds_single_channel_list(self):
        item = {
            "id": 1,
            "source": "outbound",
            "direction": "outbound",
            "channel": "email",
            "body_text": "Hello",
            "created_at": timezone.now().isoformat(),
        }
        merged = merge_timeline_duplicates([item])
        self.assertEqual(merged[0]["channels"], ["email"])

    def test_whatsapp_handoff_kept_when_no_whatsapp_message_row(self):
        body = "Bok — check-in info"
        draft = GuestMessageDraft.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            intent=GuestMessageIntent.CHECKIN,
            llm_body_text=body,
            final_body_text=body,
            channel=GuestMessageChannel.WHATSAPP,
        )
        GuestOutboundMessage.objects.create(
            tenant_id=self.tenant.pk,
            reservation=self.reservation,
            draft=draft,
            channel=GuestMessageChannel.WHATSAPP,
            body_text=body,
            status=GuestOutboundMessageStatus.HANDOFF_WHATSAPP,
            to_phone=self.reservation.booker_phone,
            wa_me_url="https://wa.me/385976713511?text=hi",
        )

        timeline = timeline_for_reservation(self.reservation)
        handoff = [row for row in timeline if body in row["body_text"]]

        self.assertEqual(len(handoff), 1)
        self.assertEqual(handoff[0]["source"], "outbound")
        self.assertEqual(handoff[0]["status"], "handoff_whatsapp")
        self.assertEqual(handoff[0]["channels"], ["whatsapp"])
