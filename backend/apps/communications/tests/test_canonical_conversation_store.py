"""ADR 0019 Phase D1: Conversation / GuestMessage / GuestMessageSource identity."""

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.utils import timezone

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
from apps.integrations.models import ChannexMessage, WhatsAppMessage
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant


class CanonicalConversationStoreIdentityTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="d1-store", name="D1 Store")
        self.other_tenant = Tenant.objects.create(slug="d1-other", name="D1 Other")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="P",
            slug="p",
        )
        today = timezone.localdate()
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booker_name="Guest",
            check_in=today,
            check_out=today + timedelta(days=1),
            status=Reservation.Status.EXPECTED,
        )
        self.conversation = Conversation.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
        )
        self.occurred_at = timezone.now()
        self.message = GuestMessage.objects.create(
            tenant=self.tenant,
            conversation=self.conversation,
            direction=GuestMessageDirection.INBOUND,
            channel=GuestMessageChannel.BOOKING,
            body="Dolazimo oko 18h",
            occurred_at=self.occurred_at,
        )

    def _channex(self, *, message_id: str, body: str = "Dolazimo oko 18h") -> ChannexMessage:
        return ChannexMessage.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            channex_booking_id="bk-1",
            channex_message_id=message_id,
            direction=ChannexMessage.Direction.INBOUND,
            sender=ChannexMessage.Sender.GUEST,
            body=body,
        )

    def _inbound(self, *, message_id: str = "", body: str = "Dolazimo oko 18h") -> GuestInboundMessage:
        return GuestInboundMessage.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            channel=GuestMessageChannel.BOOKING,
            body_text=body,
            message_id=message_id,
        )

    def test_one_logical_message_can_have_channex_and_imap_sources(self):
        channex = self._channex(message_id="ch_123")
        inbound = self._inbound(message_id="<booking-xyz@mail.booking.com>")
        channex_source = GuestMessageSource.objects.create(
            tenant=self.tenant,
            message=self.message,
            provider=GuestMessageSourceProvider.CHANNEX,
            provider_message_id="ch_123",
            channex_message=channex,
        )
        imap_source = GuestMessageSource.objects.create(
            tenant=self.tenant,
            message=self.message,
            provider=GuestMessageSourceProvider.IMAP,
            provider_message_id="<booking-xyz@mail.booking.com>",
            inbound_message=inbound,
        )

        self.assertEqual(self.message.sources.count(), 2)
        self.assertEqual(channex_source.message_id, self.message.pk)
        self.assertEqual(imap_source.message_id, self.message.pk)
        field_names = {field.name for field in GuestMessage._meta.get_fields()}
        self.assertNotIn("provider", field_names)
        self.assertNotIn("provider_message_id", field_names)
        self.assertNotIn("merged_into", field_names)
        self.assertNotIn("merged_into_id", field_names)
        self.assertNotIn("wamid", field_names)
        self.assertNotIn("channex_message_id", field_names)
        self.assertNotIn("raw_payload", field_names)
        self.assertIn("channel", field_names)
        self.assertIn("is_visible", field_names)
        self.assertTrue(self.message.is_visible)

    def test_conversation_unique_per_tenant_reservation(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Conversation.objects.create(
                    tenant=self.tenant,
                    reservation=self.reservation,
                )

    def test_conversation_tenant_must_match_reservation(self):
        other_reservation = Reservation.objects.create(
            tenant=self.other_tenant,
            property=Property.objects.create(
                tenant=self.other_tenant,
                name="Other",
                slug="other",
            ),
            booker_name="Other",
            check_in=self.reservation.check_in,
            check_out=self.reservation.check_out,
            status=Reservation.Status.EXPECTED,
        )
        mismatch = Conversation(
            tenant=self.tenant,
            reservation=other_reservation,
        )
        with self.assertRaises(ValidationError) as ctx:
            mismatch.full_clean()
        self.assertIn("tenant", ctx.exception.message_dict)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                mismatch.save()

    def test_guest_message_tenant_must_match_conversation(self):
        mismatch = GuestMessage(
            tenant=self.other_tenant,
            conversation=self.conversation,
            direction=GuestMessageDirection.INBOUND,
            channel=GuestMessageChannel.BOOKING,
            body="x",
            occurred_at=self.occurred_at,
        )
        with self.assertRaises(ValidationError) as ctx:
            mismatch.full_clean()
        self.assertIn("tenant", ctx.exception.message_dict)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                mismatch.save()

    def test_source_tenant_must_match_message_conversation(self):
        channex = self._channex(message_id="ch_tenant")
        mismatch = GuestMessageSource(
            tenant=self.other_tenant,
            message=self.message,
            provider=GuestMessageSourceProvider.CHANNEX,
            provider_message_id="ch_tenant",
            channex_message=channex,
        )
        with self.assertRaises(ValidationError) as ctx:
            mismatch.full_clean()
        self.assertIn("tenant", ctx.exception.message_dict)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                mismatch.save()

    def test_empty_provider_message_id_rejected_by_validation_and_db(self):
        channex = self._channex(message_id="ch_blank")
        source = GuestMessageSource(
            tenant=self.tenant,
            message=self.message,
            provider=GuestMessageSourceProvider.CHANNEX,
            provider_message_id="",
            channex_message=channex,
        )
        with self.assertRaises(ValidationError) as ctx:
            source.full_clean()
        self.assertIn("provider_message_id", ctx.exception.message_dict)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                source.save()

    def test_null_provider_message_id_allowed_for_imap_without_message_id(self):
        inbound = self._inbound(message_id="")
        source = GuestMessageSource.objects.create(
            tenant=self.tenant,
            message=self.message,
            provider=GuestMessageSourceProvider.IMAP,
            provider_message_id=None,
            inbound_message=inbound,
        )
        self.assertIsNone(source.provider_message_id)

    def test_duplicate_provider_message_id_rejected(self):
        first = self._channex(message_id="ch_dup_a")
        second = self._channex(message_id="ch_dup_b")
        GuestMessageSource.objects.create(
            tenant=self.tenant,
            message=self.message,
            provider=GuestMessageSourceProvider.CHANNEX,
            provider_message_id="shared-ext-id",
            channex_message=first,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                GuestMessageSource.objects.create(
                    tenant=self.tenant,
                    message=self.message,
                    provider=GuestMessageSourceProvider.CHANNEX,
                    provider_message_id="shared-ext-id",
                    channex_message=second,
                )

    def test_two_raw_fks_on_one_source_rejected(self):
        channex = self._channex(message_id="ch_two_fk")
        inbound = self._inbound(message_id="<two-fk@mail.booking.com>")
        source = GuestMessageSource(
            tenant=self.tenant,
            message=self.message,
            provider=GuestMessageSourceProvider.CHANNEX,
            provider_message_id="ch_two_fk",
            channex_message=channex,
            inbound_message=inbound,
        )
        with self.assertRaises(ValidationError):
            source.full_clean()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                source.save()

    def test_missing_raw_fk_rejected(self):
        source = GuestMessageSource(
            tenant=self.tenant,
            message=self.message,
            provider=GuestMessageSourceProvider.CHANNEX,
            provider_message_id="ch_none",
        )
        with self.assertRaises(ValidationError):
            source.full_clean()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                source.save()

    def test_raw_pointer_unique_within_type(self):
        channex = self._channex(message_id="ch_ptr")
        GuestMessageSource.objects.create(
            tenant=self.tenant,
            message=self.message,
            provider=GuestMessageSourceProvider.CHANNEX,
            provider_message_id="ch_ptr",
            channex_message=channex,
        )
        other_message = GuestMessage.objects.create(
            tenant=self.tenant,
            conversation=self.conversation,
            direction=GuestMessageDirection.INBOUND,
            channel=GuestMessageChannel.BOOKING,
            body="other",
            occurred_at=self.occurred_at,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                GuestMessageSource.objects.create(
                    tenant=self.tenant,
                    message=other_message,
                    provider=GuestMessageSourceProvider.CHANNEX,
                    provider_message_id="ch_ptr_other",
                    channex_message=channex,
                )

    def test_deleting_raw_row_is_protected(self):
        channex = self._channex(message_id="ch_protect")
        GuestMessageSource.objects.create(
            tenant=self.tenant,
            message=self.message,
            provider=GuestMessageSourceProvider.CHANNEX,
            provider_message_id="ch_protect",
            channex_message=channex,
        )
        with self.assertRaises(ProtectedError):
            channex.delete()
        self.assertTrue(ChannexMessage.objects.filter(pk=channex.pk).exists())

    def test_whatsapp_and_outbound_raw_pointers_are_exclusive(self):
        wa = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            wamid="wamid.d1-test",
            wa_id="385991234567",
            direction=WhatsAppMessage.Direction.INBOUND,
            body="hi",
        )
        outbound = GuestOutboundMessage.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            channel=GuestMessageChannel.WHATSAPP,
            body_text="hi",
            status=GuestOutboundMessageStatus.SENT,
        )
        GuestMessageSource.objects.create(
            tenant=self.tenant,
            message=self.message,
            provider=GuestMessageSourceProvider.WABA,
            provider_message_id="wamid.d1-test",
            whatsapp_message=wa,
        )
        GuestMessageSource.objects.create(
            tenant=self.tenant,
            message=self.message,
            provider=GuestMessageSourceProvider.STAY_OUTBOUND,
            provider_message_id=None,
            outbound_message=outbound,
        )
        self.assertEqual(self.message.sources.count(), 2)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                GuestMessageSource.objects.create(
                    tenant=self.tenant,
                    message=self.message,
                    provider=GuestMessageSourceProvider.WABA,
                    provider_message_id="wamid.other",
                    whatsapp_message=wa,
                )
