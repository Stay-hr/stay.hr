"""Phase 4 provider adapter tests (ADR 0010)."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.communications.messaging.bootstrap import (
    bootstrap_messaging_engine,
    reset_messaging_engine_for_tests,
)
from apps.communications.messaging.context import TriggerContext
from apps.communications.messaging.intents import MessageDefinitionKey
from apps.communications.messaging.models import (
    MessageDispatch,
    MessageDispatchStatus,
    MessageErrorCategory,
    MessageScheduleStrategy,
    MessageTriggerKind,
)
from apps.communications.messaging.providers.booking import BookingProvider
from apps.communications.messaging.providers.common import categorize_send_exception
from apps.communications.messaging.providers.email import EmailProvider
from apps.communications.messaging.providers.factory import build_live_providers
from apps.communications.messaging.providers.registry import provider_registry
from apps.communications.messaging.providers.whatsapp import WhatsAppProvider
from apps.communications.messaging.triggers import Trigger
from apps.communications.models import (
    GuestMessageChannel,
    GuestOutboundMessage,
    GuestOutboundMessageStatus,
)
from apps.integrations.whatsapp.client import WhatsAppApiError
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant


class LiveProviderRegistrationTests(SimpleTestCase):
    def setUp(self):
        reset_messaging_engine_for_tests()

    def tearDown(self):
        reset_messaging_engine_for_tests()
        bootstrap_messaging_engine(force=True, validate=True)

    def test_bootstrap_registers_live_adapters(self):
        bootstrap_messaging_engine(force=True, validate=True)
        booking = provider_registry.get("booking")
        email = provider_registry.get("email")
        whatsapp = provider_registry.get("whatsapp")
        self.assertIsInstance(booking, BookingProvider)
        self.assertIsInstance(email, EmailProvider)
        self.assertIsInstance(whatsapp, WhatsAppProvider)
        self.assertTrue(whatsapp.capabilities.supports_templates)

    def test_build_live_providers_names(self):
        names = tuple(p.name for p in build_live_providers())
        self.assertEqual(names, ("booking", "email", "whatsapp"))


class ErrorCategorizationTests(SimpleTestCase):
    def test_whatsapp_rate_limit(self):
        exc = WhatsAppApiError("WhatsApp API error 429: rate limited")
        category, code, retryable = categorize_send_exception(exc)
        self.assertEqual(category, MessageErrorCategory.RATE_LIMIT)
        self.assertTrue(retryable)
        self.assertTrue(code)

    def test_value_error_is_validation(self):
        category, code, retryable = categorize_send_exception(
            ValueError("booking_channel_unavailable")
        )
        self.assertEqual(category, MessageErrorCategory.VALIDATION)
        self.assertEqual(code, "booking_channel_unavailable")
        self.assertFalse(retryable)


class ProviderAdapterSendTests(TestCase):
    def setUp(self):
        reset_messaging_engine_for_tests()
        bootstrap_messaging_engine(force=True, validate=True)
        self.tenant = Tenant.objects.create(
            name="Adapter Tenant",
            slug="adapter-tenant",
            default_language="en",
        )
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Villa",
            slug="villa-adapter",
            timezone="Europe/Zagreb",
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booker_name="Ada Guest",
            booker_email="ada@example.com",
            booker_phone="+385911111111",
            status=Reservation.Status.EXPECTED,
            check_in=timezone.localdate() + timedelta(days=7),
            check_out=timezone.localdate() + timedelta(days=10),
        )

    def tearDown(self):
        reset_messaging_engine_for_tests()
        bootstrap_messaging_engine(force=True, validate=True)

    def _ctx(self) -> TriggerContext:
        return TriggerContext(
            reservation_id=self.reservation.pk,
            tenant_id=self.tenant.pk,
            property_id=self.property.pk,
            trigger=Trigger.time(source="test"),
            now=timezone.now(),
        )

    def _dispatch(self, **overrides) -> MessageDispatch:
        now = timezone.now()
        defaults = dict(
            tenant=self.tenant,
            reservation=self.reservation,
            definition_key=MessageDefinitionKey.CHECKIN_INFO,
            plan_key="pre_arrival",
            trigger=MessageTriggerKind.TIME,
            due_at=now,
            timezone="Europe/Zagreb",
            local_due_at=now,
            schedule_strategy=MessageScheduleStrategy.FIXED_TIME,
            status=MessageDispatchStatus.DISPATCHING,
            rendered_body="Please complete check-in.",
            rendered_subject="Check-in",
            language="en",
            template_version="checkin_info@v1",
            render_checksum="abc",
        )
        defaults.update(overrides)
        return MessageDispatch.objects.create(**defaults)

    def test_email_empty_body_validation(self):
        dispatch = self._dispatch(rendered_body="")
        result = EmailProvider().send(dispatch, self._ctx())
        self.assertFalse(result.success)
        self.assertEqual(result.error_category, MessageErrorCategory.VALIDATION)
        self.assertEqual(result.error_code, "empty_body")

    @patch("apps.communications.messaging.providers.email.send_guest_message")
    def test_email_success_uses_snapshot(self, mock_send):
        outbound = GuestOutboundMessage(
            pk=42,
            tenant=self.tenant,
            reservation=self.reservation,
            channel=GuestMessageChannel.EMAIL,
            body_text="Please complete check-in.",
            status=GuestOutboundMessageStatus.SENT,
            to_email="ada@example.com",
        )
        mock_send.return_value = outbound
        dispatch = self._dispatch()
        result = EmailProvider().send(dispatch, self._ctx())
        self.assertTrue(result.success)
        self.assertEqual(result.outbound_message_id, 42)
        kwargs = mock_send.call_args.kwargs
        self.assertEqual(kwargs["body_text"], "Please complete check-in.")
        self.assertEqual(kwargs["subject"], "Check-in")
        self.assertEqual(kwargs["channel"], GuestMessageChannel.EMAIL)

    @patch(
        "apps.communications.messaging.providers.booking.build_message_channels",
        return_value={"booking": {"available": False}},
    )
    def test_booking_unavailable_falls_through(self, _mock_channels):
        dispatch = self._dispatch()
        result = BookingProvider().send(dispatch, self._ctx())
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "booking_channel_unavailable")
        self.assertEqual(result.error_category, MessageErrorCategory.VALIDATION)
        self.assertFalse(result.retryable)

    @patch("apps.communications.messaging.providers.booking.send_guest_message")
    @patch(
        "apps.communications.messaging.providers.booking.build_message_channels",
        return_value={"booking": {"available": True}},
    )
    def test_booking_success(self, _mock_channels, mock_send):
        row = MagicMock()
        row.channex_message_id = "chx-123"
        row.provider_message_id = ""
        mock_send.return_value = row
        dispatch = self._dispatch()
        result = BookingProvider().send(dispatch, self._ctx())
        self.assertTrue(result.success)
        self.assertEqual(result.provider_message_id, "chx-123")

    @patch(
        "apps.communications.messaging.providers.whatsapp.send_welcome_template_for_reservation"
    )
    def test_whatsapp_welcome_maps_sent(self, mock_welcome):
        mock_welcome.return_value = {
            "status": "sent",
            "reservation_id": self.reservation.pk,
            "wamid": "wamid.ABC",
        }
        dispatch = self._dispatch(
            definition_key=MessageDefinitionKey.WELCOME,
            rendered_body="Welcome — WhatsApp template send.",
            rendered_subject="",
            template_version="welcome@v1",
        )
        result = WhatsAppProvider().send(dispatch, self._ctx())
        self.assertTrue(result.success)
        self.assertEqual(result.provider_message_id, "wamid.ABC")
        mock_welcome.assert_called_once_with(self.reservation)

    @patch(
        "apps.communications.messaging.providers.whatsapp.send_welcome_template_for_reservation"
    )
    def test_whatsapp_welcome_maps_skip_reason(self, mock_welcome):
        mock_welcome.return_value = {
            "status": "skipped",
            "reason": "no_phone",
            "reservation_id": self.reservation.pk,
        }
        dispatch = self._dispatch(
            definition_key=MessageDefinitionKey.WELCOME,
            rendered_body="Welcome — WhatsApp template send.",
        )
        result = WhatsAppProvider().send(dispatch, self._ctx())
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "no_phone")
        self.assertEqual(result.error_category, MessageErrorCategory.VALIDATION)

    @patch("apps.communications.messaging.providers.whatsapp.send_guest_message")
    def test_whatsapp_non_welcome_uses_text_path(self, mock_send):
        outbound = GuestOutboundMessage(
            pk=7,
            tenant=self.tenant,
            reservation=self.reservation,
            channel=GuestMessageChannel.WHATSAPP,
            body_text="Hello",
            status=GuestOutboundMessageStatus.SENT,
            to_phone="+385911111111",
            provider_message_id="wamid.TXT",
        )
        mock_send.return_value = outbound
        dispatch = self._dispatch(
            definition_key=MessageDefinitionKey.CHECKIN_INFO,
            rendered_body="Hello",
        )
        result = WhatsAppProvider().send(dispatch, self._ctx())
        self.assertTrue(result.success)
        self.assertEqual(result.provider_message_id, "wamid.TXT")
        self.assertEqual(
            mock_send.call_args.kwargs["channel"], GuestMessageChannel.WHATSAPP
        )
