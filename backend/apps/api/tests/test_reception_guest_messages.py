from decimal import Decimal
import os
from datetime import timedelta
from io import BytesIO
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIClient

from apps.integrations.tests.test_whatsapp_webhook import TEST_FERNET_KEY
from apps.integrations.whatsapp.client import WhatsAppApiError

from apps.properties.models import Property, Unit
from apps.reservations.models import Guest, Reservation, ReservationUnit
from apps.tenants.models import RECEPTION_DEVICE_SCOPES, ApiApplication, Tenant


def _tiny_jpeg(name: str = "chat.jpg") -> SimpleUploadedFile:
    buf = BytesIO()
    Image.new("RGB", (40, 40), color=(200, 180, 160)).save(buf, format="JPEG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/jpeg")


class ReceptionGuestMessagesAPITests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Uzorita", slug="uzorita")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Luxury Room Uzorita",
            slug="uzorita",
            address="Ul. bana Josipa Jelačića 58, 22000 Šibenik",
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
            check_in="2026-06-04",
            check_out="2026-06-05",
            status=Reservation.Status.EXPECTED,
            booker_name="Wolfgang Gross",
            booker_email="wolfgang@example.com",
            booker_phone="+49 170 1234567",
            amount=Decimal("180.15"),
            adults_count=2,
            payment_provider="booking.com",
        )
        ReservationUnit.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            unit=self.unit,
            room_name="Deluxe King Room R1",
            sort_order=0,
        )
        Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="Wolfgang",
            last_name="Gross",
            email="wolfgang@example.com",
            is_primary=True,
        )
        self.client = APIClient()
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.raw_token}"}
        self.base = f"/api/v1/reception/reservations/{self.reservation.id}/messages"

    def test_list_messages_empty(self):
        response = self.client.get(f"{self.base}/", **self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    @patch("apps.communications.guest_message_sync.poll_tenant_guest_inbox")
    def test_list_messages_sync_auto_skips_imap_poll(self, mock_poll):
        response = self.client.get(f"{self.base}/?sync=auto", **self.auth)
        self.assertEqual(response.status_code, 200)
        mock_poll.assert_not_called()

    @patch("apps.communications.guest_message_sync.poll_tenant_guest_inbox")
    def test_list_messages_sync_one_polls_imap(self, mock_poll):
        response = self.client.get(f"{self.base}/?sync=1", **self.auth)
        self.assertEqual(response.status_code, 200)
        mock_poll.assert_called_once_with(self.tenant)

    def test_message_channels(self):
        response = self.client.get(f"{self.base}/channels/", **self.auth)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["email"]["available"])
        self.assertEqual(data["email"]["to"], "wolfgang@example.com")
        self.assertTrue(data["whatsapp"]["available"])
        self.assertIn("api_send", data["whatsapp"])
        self.assertIn("491701234567", data["whatsapp"]["phone_wa"])
    def test_compose_checkin_fallback(self):
        os.environ.pop("GUEST_COMPOSE_LLM_API_KEY", None)
        self.reservation.booker_country = "HR"
        self.reservation.save(update_fields=["booker_country"])
        response = self.client.post(
            f"{self.base}/compose/",
            {"intent": "checkin"},
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertIn("draft_id", data)
        self.assertIn("Wolfgang", data["body_text"])
        self.assertFalse(data["llm_used"])
        body = data["body_text"]
        self.assertIn("Restaurant Uzorita", body)
        self.assertIn("Parkiranje", body)
        self.assertIn("eVisitor", body)
        self.assertNotIn("okvirno vrijeme dolaska", body.lower())
        self.assertTrue(data["channels"]["email"]["available"])
        self.assertEqual(data["channels"]["email"]["to"], "wolfgang@example.com")
        self.assertTrue(data["channels"]["whatsapp"]["available"])
        self.assertIn("491701234567", data["channels"]["whatsapp"]["phone_wa"])
        self.assertIn("body_text_tenant", data)
        self.assertIn("tenant_language", data)

    @patch("apps.communications.guest_compose.translation_available", return_value=True)
    @patch("apps.communications.guest_compose.translate_text")
    @patch.dict(os.environ, {}, clear=False)
    def test_compose_checkin_dual_language_tenant_preview(self, mock_translate, _mock_translation_available):
        os.environ.pop("GUEST_COMPOSE_LLM_API_KEY", None)
        self.tenant.default_language = "hr"
        self.tenant.save(update_fields=["default_language"])
        self.reservation.booker_country = "DE"
        self.reservation.save(update_fields=["booker_country"])
        mock_translate.return_value = "Pozdrav Wolfgang, dobrodošli."

        response = self.client.post(
            f"{self.base}/compose/",
            {"intent": "checkin"},
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["language"], "de")
        self.assertIn("Eingang", data["body_text"])
        self.assertEqual(data["tenant_language"], "hr")
        self.assertEqual(data["body_text_tenant"], "Pozdrav Wolfgang, dobrodošli.")
        mock_translate.assert_called_once()

    @patch.dict(os.environ, {}, clear=False)
    def test_compose_checkin_language_de(self):
        os.environ.pop("GUEST_COMPOSE_LLM_API_KEY", None)
        self.reservation.booker_country = "DE"
        self.reservation.save(update_fields=["booker_country"])
        response = self.client.post(
            f"{self.base}/compose/",
            {"intent": "checkin"},
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["language"], "de")
        self.assertIn("Eingang", data["body_text"])
        self.assertIn("Parken", data["body_text"])

    @patch.dict(os.environ, {}, clear=False)
    def test_compose_checkin_language_balkan_hr(self):
        os.environ.pop("GUEST_COMPOSE_LLM_API_KEY", None)
        self.reservation.booker_country = "RS"
        self.reservation.save(update_fields=["booker_country"])
        response = self.client.post(
            f"{self.base}/compose/",
            {"intent": "checkin"},
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["language"], "hr")
        self.assertIn("Parkiranje", data["body_text"])

    @patch.dict(os.environ, {}, clear=False)
    def test_compose_checkin_language_default_en(self):
        os.environ.pop("GUEST_COMPOSE_LLM_API_KEY", None)
        self.reservation.booker_country = "NL"
        self.reservation.save(update_fields=["booker_country"])
        response = self.client.post(
            f"{self.base}/compose/",
            {"intent": "checkin"},
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["language"], "en")
        self.assertIn("Parking", data["body_text"])

    @patch.dict(os.environ, {}, clear=False)
    def test_compose_reply_checkin_ready(self):
        os.environ.pop("GUEST_COMPOSE_LLM_API_KEY", None)
        self.reservation.booker_country = "DE"
        self.reservation.save(update_fields=["booker_country"])
        response = self.client.post(
            f"{self.base}/compose/",
            {"intent": "reply", "hint": "checkin ready"},
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertFalse(data["llm_used"])
        self.assertEqual(data["language"], "de")
        body = data["body_text"]
        self.assertIn("Vielen Dank", body)
        self.assertIn("Ankunftszeit", body)

    @patch.dict(os.environ, {}, clear=False)
    def test_compose_reply_checkin_ready_language_es(self):
        os.environ.pop("GUEST_COMPOSE_LLM_API_KEY", None)
        self.reservation.booker_country = "ES"
        self.reservation.save(update_fields=["booker_country"])
        response = self.client.post(
            f"{self.base}/compose/",
            {"intent": "reply", "hint": "checkin ready"},
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertFalse(data["llm_used"])
        self.assertEqual(data["language"], "es")
        self.assertIn("Gracias", data["body_text"])
        self.assertIn("llegada", data["body_text"])

    @patch.dict(os.environ, {}, clear=False)
    def test_compose_reply_checkin_ready_same_lang_as_checkin(self):
        """RO guest → en for both checkin and checkin-ready reply."""
        os.environ.pop("GUEST_COMPOSE_LLM_API_KEY", None)
        self.reservation.booker_country = "RO"
        self.reservation.save(update_fields=["booker_country"])
        checkin = self.client.post(
            f"{self.base}/compose/",
            {"intent": "checkin"},
            format="json",
            **self.auth,
        )
        reply = self.client.post(
            f"{self.base}/compose/",
            {"intent": "reply", "hint": "checkin ready"},
            format="json",
            **self.auth,
        )
        self.assertEqual(checkin.json()["language"], "en")
        self.assertEqual(reply.json()["language"], "en")
        self.assertIn("Parking", checkin.json()["body_text"])
        self.assertIn("Thank you", reply.json()["body_text"])

    @patch("apps.communications.guest_compose.complete_chat")
    @patch.dict(
        os.environ,
        {
            "GUEST_COMPOSE_LLM_PROVIDER": "openai",
            "GUEST_COMPOSE_LLM_API_KEY": "test-key",
            "GUEST_COMPOSE_LLM_MODEL": "gpt-4o-mini",
        },
        clear=False,
    )
    def test_compose_checkin_skips_llm(self, mock_complete):
        mock_complete.return_value = "LLM should not be used for checkin."
        response = self.client.post(
            f"{self.base}/compose/",
            {"intent": "checkin"},
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertFalse(data["llm_used"])
        self.assertIn("Restaurant Uzorita", data["body_text"])
        mock_complete.assert_not_called()

    @patch("apps.communications.guest_compose.complete_chat")
    @patch.dict(
        os.environ,
        {
            "GUEST_COMPOSE_LLM_PROVIDER": "openai",
            "GUEST_COMPOSE_LLM_API_KEY": "test-key",
            "GUEST_COMPOSE_LLM_MODEL": "gpt-4o-mini",
        },
        clear=False,
    )
    def test_compose_uses_llm(self, mock_complete):
        mock_complete.return_value = "Hello Wolfgang!\n\nManaged by stay.hr — https://stay.hr/"
        response = self.client.post(
            f"{self.base}/compose/",
            {"intent": "custom", "hint": "Confirm late arrival at 22:00."},
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertTrue(data["llm_used"])
        self.assertIn("Hello Wolfgang", data["body_text"])
        mock_complete.assert_called_once()

    @patch("apps.communications.guest_message_send.send_guest_text_email")
    @patch.dict(os.environ, {}, clear=False)
    def test_send_email(self, mock_send):
        os.environ.pop("GUEST_COMPOSE_LLM_API_KEY", None)
        mock_send.return_value = {"sent": True, "to": "wolfgang@example.com"}
        compose = self.client.post(
            f"{self.base}/compose/",
            {"intent": "checkin"},
            format="json",
            **self.auth,
        )
        draft_id = compose.json()["draft_id"]
        body = "Custom check-in text for Wolfgang."

        response = self.client.post(
            f"{self.base}/send/",
            {
                "draft_id": draft_id,
                "channel": "email",
                "body_text": body,
            },
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["channel"], "email")
        self.assertEqual(data["status"], "sent")
        self.assertEqual(data["body_text"], body)
        mock_send.assert_called_once()
        call_kwargs = mock_send.call_args.kwargs
        self.assertIn("body_html", call_kwargs)
        self.assertIn("<p>", call_kwargs["body_html"])

        timeline = self.client.get(f"{self.base}/", **self.auth)
        self.assertEqual(len(timeline.json()), 1)

    @patch.dict(os.environ, {}, clear=False)
    def test_send_whatsapp_handoff(self):
        os.environ.pop("GUEST_COMPOSE_LLM_API_KEY", None)
        compose = self.client.post(
            f"{self.base}/compose/",
            {"intent": "checkin"},
            format="json",
            **self.auth,
        )
        draft_id = compose.json()["draft_id"]
        body = "Bok Wolfgang! Check-in info."

        response = self.client.post(
            f"{self.base}/send/",
            {
                "draft_id": draft_id,
                "channel": "whatsapp",
                "body_text": body,
            },
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["channel"], "whatsapp")
        self.assertEqual(data["status"], "handoff_whatsapp")
        self.assertIn("wa.me/491701234567", data["wa_me_url"])
        self.assertIn("Bok%20Wolfgang", data["wa_me_url"])

    @patch.dict(os.environ, {}, clear=False)
    def test_send_whatsapp_handoff_long_body(self):
        """Regression: long check-in text must not 500 (wa_me_url was varchar 512)."""
        os.environ.pop("GUEST_COMPOSE_LLM_API_KEY", None)
        compose = self.client.post(
            f"{self.base}/compose/",
            {"intent": "checkin"},
            format="json",
            **self.auth,
        )
        draft_id = compose.json()["draft_id"]
        body = (
            "Molimo vas da nam pošaljete potrebne dokumente. "
            "Šibenik check-in u 15:00. "
        ) * 80

        response = self.client.post(
            f"{self.base}/send/",
            {
                "draft_id": draft_id,
                "channel": "whatsapp",
                "body_text": body,
            },
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        self.assertEqual(data["status"], "handoff_whatsapp")
        self.assertIn("wa.me/491701234567", data["wa_me_url"])
        self.assertGreater(len(data["wa_me_url"]), 512)

    def test_send_whatsapp_no_phone(self):
        self.reservation.booker_phone = ""
        self.reservation.save(update_fields=["booker_phone"])
        compose = self.client.post(
            f"{self.base}/compose/",
            {"intent": "checkin"},
            format="json",
            **self.auth,
        )
        draft_id = compose.json()["draft_id"]
        response = self.client.post(
            f"{self.base}/send/",
            {
                "draft_id": draft_id,
                "channel": "whatsapp",
                "body_text": "Test",
            },
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 400)

    def test_compose_requires_write_scope(self):
        read_app, read_token = ApiApplication.create_with_token(
            tenant=self.tenant,
            name="Read only",
            scopes=["reception:read"],
        )
        response = self.client.post(
            f"{self.base}/compose/",
            {"intent": "checkin"},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {read_token}",
        )
        self.assertEqual(response.status_code, 403)
        read_app.delete()

    @patch.dict(os.environ, {}, clear=False)
    def test_compose_includes_booking_channel_for_channex_reservation(self):
        from apps.integrations.channex.booking_service import channex_external_id
        from apps.integrations.models import IntegrationConfig
        from apps.tenants.models import ChannelManager, TenantReceptionSettings

        os.environ.pop("GUEST_COMPOSE_LLM_API_KEY", None)
        TenantReceptionSettings.objects.create(
            tenant=self.tenant,
            channel_manager=ChannelManager.CHANNEX,
        )
        IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.CHANNEX,
            is_active=True,
        )
        self.reservation.import_source = "channex"
        self.reservation.external_id = channex_external_id("test-booking-uuid")
        self.reservation.save(update_fields=["import_source", "external_id"])

        response = self.client.post(
            f"{self.base}/compose/",
            {"intent": "checkin"},
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertTrue(data["channels"]["booking"]["available"])

    @patch("apps.communications.guest_message_send.send_message_for_reservation")
    @patch.dict(os.environ, {}, clear=False)
    def test_send_booking_channel(self, mock_send):
        from apps.integrations.channex.booking_service import channex_external_id
        from apps.integrations.models import ChannexMessage, IntegrationConfig
        from apps.tenants.models import ChannelManager, TenantReceptionSettings

        os.environ.pop("GUEST_COMPOSE_LLM_API_KEY", None)
        TenantReceptionSettings.objects.create(
            tenant=self.tenant,
            channel_manager=ChannelManager.CHANNEX,
        )
        IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.CHANNEX,
            is_active=True,
        )
        self.reservation.import_source = "channex"
        self.reservation.external_id = channex_external_id("test-booking-uuid")
        self.reservation.save(update_fields=["import_source", "external_id"])

        mock_send.return_value = ChannexMessage.objects.create(
            tenant=self.tenant,
            channex_booking_id="test-booking-uuid",
            channex_message_id="msg-out-1",
            direction=ChannexMessage.Direction.OUTBOUND,
            sender=ChannexMessage.Sender.PROPERTY,
            body="Reply via Booking.com",
            reservation=self.reservation,
        )

        compose = self.client.post(
            f"{self.base}/compose/",
            {"intent": "reply"},
            format="json",
            **self.auth,
        )
        draft_id = compose.json()["draft_id"]
        body = "Reply via Booking.com"

        response = self.client.post(
            f"{self.base}/send/",
            {
                "draft_id": draft_id,
                "channel": "booking",
                "body_text": body,
            },
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        self.assertEqual(data["channel"], "booking")
        self.assertEqual(data["source"], "booking")
        self.assertEqual(data["status"], "sent")
        self.assertEqual(data["body_text"], body)
        mock_send.assert_called_once()

        timeline = self.client.get(f"{self.base}/", **self.auth)
        self.assertEqual(len(timeline.json()), 1)
        self.assertEqual(timeline.json()[0]["source"], "booking")

    def test_timeline_includes_channex_inbound(self):
        from apps.integrations.models import ChannexMessage

        ChannexMessage.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            channex_booking_id="booking-1",
            channex_message_id="msg-in-1",
            direction=ChannexMessage.Direction.INBOUND,
            sender=ChannexMessage.Sender.GUEST,
            body="Hello from guest",
        )

        response = self.client.get(f"{self.base}/", **self.auth)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["source"], "booking")
        self.assertEqual(data[0]["direction"], "inbound")
        self.assertEqual(data[0]["body_text"], "Hello from guest")

    @patch("apps.integrations.channex.message_service.ChannexClient")
    def test_timeline_syncs_channex_when_empty(self, mock_client_cls):
        from unittest.mock import MagicMock

        from apps.integrations.channex.booking_service import channex_external_id
        from apps.integrations.models import IntegrationConfig
        from apps.tenants.models import ChannelManager, TenantReceptionSettings

        TenantReceptionSettings.objects.create(
            tenant=self.tenant,
            channel_manager=ChannelManager.CHANNEX,
        )
        IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.CHANNEX,
            is_active=True,
        )
        self.reservation.import_source = "channex"
        booking_id = "timeline-booking-uuid"
        self.reservation.external_id = channex_external_id(booking_id)
        self.reservation.save(update_fields=["import_source", "external_id"])

        mock_client = MagicMock()
        mock_client.list_booking_messages.return_value = {
            "data": [
                {
                    "id": "remote-timeline-msg",
                    "attributes": {
                        "message": "Guest reply from Booking.com",
                        "sender": "guest",
                        "booking_id": booking_id,
                        "inserted_at": "2026-05-27T08:10:00.000000",
                    },
                }
            ]
        }
        mock_client_cls.return_value = mock_client

        response = self.client.get(f"{self.base}/", **self.auth)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["source"], "booking")
        self.assertEqual(data[0]["direction"], "inbound")
        self.assertEqual(data[0]["body_text"], "Guest reply from Booking.com")

    @patch.dict(os.environ, {}, clear=False)
    def test_compose_direct_platform_has_email_not_booking(self):
        os.environ.pop("GUEST_COMPOSE_LLM_API_KEY", None)
        self.reservation.source = "api"
        self.reservation.import_source = ""
        self.reservation.external_id = "WEB-123"
        self.reservation.save(update_fields=["source", "import_source", "external_id"])

        response = self.client.post(
            f"{self.base}/compose/",
            {"intent": "checkin"},
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertTrue(data["channels"]["email"]["available"])
        self.assertFalse(data["channels"]["booking"]["available"])
        self.assertTrue(data["channels"]["whatsapp"]["available"])

    @patch.dict(os.environ, {}, clear=False)
    def test_compose_body_text_skips_llm(self):
        os.environ.pop("GUEST_COMPOSE_LLM_API_KEY", None)
        exact = "Exact resend text without LLM regeneration."

        response = self.client.post(
            f"{self.base}/compose/",
            {"body_text": exact},
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        self.assertEqual(data["body_text"], exact)
        self.assertFalse(data["llm_used"])
        self.assertIn("draft_id", data)

    @patch("apps.communications.guest_message_send.send_message_for_reservation")
    @patch.dict(os.environ, {}, clear=False)
    def test_resend_whatsapp_to_booking(self, mock_send):
        from apps.integrations.channex.booking_service import channex_external_id
        from apps.integrations.models import ChannexMessage, IntegrationConfig
        from apps.tenants.models import ChannelManager, TenantReceptionSettings

        os.environ.pop("GUEST_COMPOSE_LLM_API_KEY", None)
        TenantReceptionSettings.objects.create(
            tenant=self.tenant,
            channel_manager=ChannelManager.CHANNEX,
        )
        IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.CHANNEX,
            is_active=True,
        )
        self.reservation.import_source = "channex"
        self.reservation.external_id = channex_external_id("test-booking-uuid")
        self.reservation.save(update_fields=["import_source", "external_id"])

        body = "Parking info resend via Channex."
        mock_send.return_value = ChannexMessage.objects.create(
            tenant=self.tenant,
            channex_booking_id="test-booking-uuid",
            channex_message_id="msg-resend-1",
            direction=ChannexMessage.Direction.OUTBOUND,
            sender=ChannexMessage.Sender.PROPERTY,
            body=body,
            reservation=self.reservation,
        )

        compose = self.client.post(
            f"{self.base}/compose/",
            {"body_text": body},
            format="json",
            **self.auth,
        )
        draft_id = compose.json()["draft_id"]

        response = self.client.post(
            f"{self.base}/send/",
            {
                "draft_id": draft_id,
                "channel": "booking",
                "body_text": body,
            },
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        self.assertEqual(data["channel"], "booking")
        self.assertEqual(data["body_text"], body)
        mock_send.assert_called_once()

    @patch("apps.communications.guest_message_send.send_guest_text_email")
    @patch.dict(os.environ, {}, clear=False)
    def test_resend_whatsapp_to_email(self, mock_send):
        os.environ.pop("GUEST_COMPOSE_LLM_API_KEY", None)
        mock_send.return_value = {"sent": True, "to": "wolfgang@example.com"}
        body = "Parking info resend via email."

        compose = self.client.post(
            f"{self.base}/compose/",
            {"body_text": body},
            format="json",
            **self.auth,
        )
        draft_id = compose.json()["draft_id"]

        response = self.client.post(
            f"{self.base}/send/",
            {
                "draft_id": draft_id,
                "channel": "email",
                "body_text": body,
            },
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        self.assertEqual(data["channel"], "email")
        self.assertEqual(data["status"], "sent")
        self.assertEqual(data["body_text"], body)
        mock_send.assert_called_once()

    @patch("apps.communications.guest_message_send.send_guest_text_email")
    def test_send_image_email(self, mock_send):
        mock_send.return_value = {"sent": True, "to": "wolfgang@example.com"}
        response = self.client.post(
            f"{self.base}/send-image/",
            {
                "file": _tiny_jpeg(),
                "channel": "email",
                "caption": "Parking map",
            },
            format="multipart",
            **self.auth,
        )
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        self.assertEqual(data["channel"], "email")
        self.assertEqual(data["status"], "sent")
        self.assertEqual(data["message_type"], "image")
        self.assertIn("/guest-outbound-messages/", data["media_url"])
        mock_send.assert_called_once()
        _args, kwargs = mock_send.call_args
        self.assertIsNotNone(kwargs.get("attachment"))

        timeline = self.client.get(f"{self.base}/", **self.auth)
        self.assertEqual(len(timeline.json()), 1)
        self.assertEqual(timeline.json()[0]["message_type"], "image")

    @patch("apps.integrations.channex.message_service.ChannexClient")
    def test_send_image_booking(self, mock_client_cls):
        from apps.integrations.channex.booking_service import channex_external_id
        from apps.integrations.models import ChannexMessage, IntegrationConfig
        from apps.tenants.models import ChannelManager, TenantReceptionSettings

        TenantReceptionSettings.objects.create(
            tenant=self.tenant,
            channel_manager=ChannelManager.CHANNEX,
        )
        IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.CHANNEX,
            is_active=True,
        )
        self.reservation.import_source = "channex"
        self.reservation.external_id = channex_external_id("test-booking-uuid")
        self.reservation.save(update_fields=["import_source", "external_id"])

        mock_client = mock_client_cls.return_value
        mock_client.upload_attachment.return_value = "attach-uuid-1"
        mock_client.send_booking_message.return_value = {
            "data": {
                "id": "outbound-image-1",
                "attributes": {
                    "message": "Room photo",
                    "sender": "property",
                    "booking_id": "test-booking-uuid",
                    "have_attachment": True,
                },
            }
        }

        response = self.client.post(
            f"{self.base}/send-image/",
            {
                "file": _tiny_jpeg(),
                "channel": "booking",
                "caption": "Room photo",
            },
            format="multipart",
            **self.auth,
        )
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        self.assertEqual(data["channel"], "booking")
        self.assertEqual(data["status"], "sent")
        self.assertEqual(data["message_type"], "image")
        self.assertIn("/channex-messages/", data["media_url"])
        mock_client.upload_attachment.assert_called_once()
        mock_client.send_booking_message.assert_called_once_with(
            "test-booking-uuid",
            "Room photo",
            attachment_id="attach-uuid-1",
        )
        self.assertTrue(
            ChannexMessage.objects.filter(channex_message_id="outbound-image-1").exists()
        )

        timeline = self.client.get(f"{self.base}/", **self.auth)
        self.assertEqual(len(timeline.json()), 1)
        self.assertEqual(timeline.json()[0]["message_type"], "image")

    def test_message_channels_includes_reply_channel(self):
        from apps.integrations.models import IntegrationConfig, WhatsAppMessage

        integration = IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.WHATSAPP,
            routing_key="1068791909660300",
            is_active=True,
        )
        WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=integration,
            reservation=self.reservation,
            wamid="wamid.in.reply-ch",
            wa_id="491701234567",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="text",
            body="Latest guest message",
            raw_payload={},
        )

        response = self.client.get(f"{self.base}/channels/", **self.auth)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["reply_channel"], "whatsapp")
        self.assertEqual(data["default_channel"], "email")

    @patch.dict(os.environ, {}, clear=False)
    def test_compose_reply_defaults_to_booking_after_inbound(self):
        from apps.integrations.channex.booking_service import channex_external_id
        from apps.integrations.models import ChannexMessage, IntegrationConfig
        from apps.tenants.models import ChannelManager, TenantReceptionSettings

        os.environ.pop("GUEST_COMPOSE_LLM_API_KEY", None)
        TenantReceptionSettings.objects.create(
            tenant=self.tenant,
            channel_manager=ChannelManager.CHANNEX,
        )
        IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.CHANNEX,
            is_active=True,
        )
        self.reservation.import_source = "channex"
        self.reservation.external_id = channex_external_id("test-booking-uuid")
        self.reservation.save(update_fields=["import_source", "external_id"])

        ChannexMessage.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            channex_booking_id="test-booking-uuid",
            channex_message_id="msg-in-reply",
            direction=ChannexMessage.Direction.INBOUND,
            sender=ChannexMessage.Sender.GUEST,
            body="Question from Booking.com",
        )

        response = self.client.post(
            f"{self.base}/compose/",
            {"intent": "reply"},
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        self.assertEqual(data["channels"]["reply_channel"], "booking")
        self.assertEqual(data["channels"]["default_channel"], "booking")

    def test_last_inbound_channel_for_reply(self):
        from apps.communications.guest_message_send import last_inbound_channel_for_reply
        from apps.integrations.models import IntegrationConfig, WhatsAppMessage

        self.assertEqual(last_inbound_channel_for_reply(self.reservation), "")

        integration = IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.WHATSAPP,
            routing_key="1068791909660300",
            is_active=True,
        )
        WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=integration,
            reservation=self.reservation,
            wamid="wamid.in.unit",
            wa_id="491701234567",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="text",
            body="Hello",
            raw_payload={},
        )
        self.assertEqual(last_inbound_channel_for_reply(self.reservation), "whatsapp")

    def test_guest_whatsapp_session_open(self):
        from apps.communications.guest_message_send import guest_whatsapp_session_open
        from apps.integrations.models import IntegrationConfig, WhatsAppMessage

        integration = IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.WHATSAPP,
            routing_key="1068791909660300",
            is_active=True,
        )
        self.assertFalse(guest_whatsapp_session_open(self.reservation))

        WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=integration,
            reservation=self.reservation,
            wamid="wamid.in.old",
            wa_id="491701234567",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="text",
            body="Old hello",
            raw_payload={},
        )
        old = WhatsAppMessage.objects.get(wamid="wamid.in.old")
        WhatsAppMessage.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(hours=25),
        )
        self.assertFalse(guest_whatsapp_session_open(self.reservation))

        WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=integration,
            reservation=self.reservation,
            wamid="wamid.in.fresh",
            wa_id="491701234567",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="text",
            body="Fresh hello",
            raw_payload={},
        )
        self.assertTrue(guest_whatsapp_session_open(self.reservation))

    def _create_whatsapp_integration(self):
        from apps.integrations.models import IntegrationConfig

        integration = IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.WHATSAPP,
            routing_key="1068791909660300",
            is_active=True,
        )
        integration.set_config_dict(
            {
                "phone_number_id": "1068791909660300",
            }
        )
        integration.save()
        return integration

    @override_settings(STAY_INTEGRATION_FERNET_KEY=TEST_FERNET_KEY)
    @patch.dict(os.environ, {"WHATSAPP_ACCESS_TOKEN": "test-whatsapp-access-token"}, clear=False)
    @patch("apps.communications.guest_message_whatsapp_v2.send_text_message")
    def test_send_whatsapp_api_no_session_handoff(self, mock_send):
        from apps.integrations.models import WhatsAppMessage

        os.environ.pop("GUEST_COMPOSE_LLM_API_KEY", None)
        integration = self._create_whatsapp_integration()
        stale = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=integration,
            reservation=self.reservation,
            wamid="wamid.in.stale",
            wa_id="491701234567",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="text",
            body="Stale",
            raw_payload={},
        )
        WhatsAppMessage.objects.filter(pk=stale.pk).update(
            created_at=timezone.now() - timedelta(hours=30),
        )

        compose = self.client.post(
            f"{self.base}/compose/",
            {"intent": "checkin"},
            format="json",
            **self.auth,
        )
        draft_id = compose.json()["draft_id"]
        body = "Follow-up after stale session"

        response = self.client.post(
            f"{self.base}/send/",
            {
                "draft_id": draft_id,
                "channel": "whatsapp",
                "body_text": body,
            },
            format="json",
            **self.auth,
        )
        # v2: free-form outside the CS window requires an approved template.
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("whatsapp_template_required", response.content.decode())
        mock_send.assert_not_called()

    @override_settings(STAY_INTEGRATION_FERNET_KEY=TEST_FERNET_KEY)
    @patch.dict(os.environ, {"WHATSAPP_ACCESS_TOKEN": "test-whatsapp-access-token"}, clear=False)
    @patch("apps.communications.guest_message_whatsapp_v2.send_text_message")
    def test_send_whatsapp_api_with_session_sent(self, mock_send):
        from apps.integrations.models import WhatsAppMessage

        os.environ.pop("GUEST_COMPOSE_LLM_API_KEY", None)
        mock_send.return_value = {"messages": [{"id": "wamid.out.sent"}]}
        integration = self._create_whatsapp_integration()
        WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=integration,
            reservation=self.reservation,
            wamid="wamid.in.fresh",
            wa_id="491701234567",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="text",
            body="Fresh inbound",
            raw_payload={},
        )

        compose = self.client.post(
            f"{self.base}/compose/",
            {"intent": "reply"},
            format="json",
            **self.auth,
        )
        draft_id = compose.json()["draft_id"]
        body = "Reply within session"

        response = self.client.post(
            f"{self.base}/send/",
            {
                "draft_id": draft_id,
                "channel": "whatsapp",
                "body_text": body,
            },
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        self.assertEqual(data["status"], "sent")
        mock_send.assert_called_once()

    @override_settings(STAY_INTEGRATION_FERNET_KEY=TEST_FERNET_KEY)
    @patch.dict(os.environ, {"WHATSAPP_ACCESS_TOKEN": "test-whatsapp-access-token"}, clear=False)
    @patch("apps.communications.guest_message_whatsapp_v2.send_template_message")
    @patch("apps.communications.guest_message_whatsapp_v2.find_message_template")
    @patch("apps.communications.guest_message_whatsapp_v2.send_text_message")
    def test_send_whatsapp_api_session_error_handoff(self, mock_send, mock_find, mock_template):
        """Session API error falls back to template when welcome template is available (v2)."""
        from apps.integrations.models import WhatsAppMessage

        os.environ.pop("GUEST_COMPOSE_LLM_API_KEY", None)
        mock_send.side_effect = WhatsAppApiError(
            "WhatsApp API error 400: 131047 Re-engagement message"
        )
        mock_find.return_value = {"status": "APPROVED", "name": "welcome_hr"}
        mock_template.return_value = {"messages": [{"id": "wamid.out.template"}]}
        integration = self._create_whatsapp_integration()
        cfg = integration.get_config_dict()
        cfg["waba_id"] = "123456789"
        integration.set_config_dict(cfg)
        integration.save()
        WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=integration,
            reservation=self.reservation,
            wamid="wamid.in.open",
            wa_id="491701234567",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="text",
            body="Recent inbound",
            raw_payload={},
        )

        compose = self.client.post(
            f"{self.base}/compose/",
            {"intent": "checkin"},
            format="json",
            **self.auth,
        )
        draft_id = compose.json()["draft_id"]

        response = self.client.post(
            f"{self.base}/send/",
            {
                "draft_id": draft_id,
                "channel": "whatsapp",
                "body_text": "Session closed by API",
            },
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        self.assertEqual(data["status"], "sent")
        mock_template.assert_called_once()

    @patch("apps.integrations.channex.message_service.ChannexClient")
    def test_send_image_booking(self, mock_client_cls):
        from apps.integrations.channex.booking_service import channex_external_id
        from apps.integrations.models import ChannexMessage, IntegrationConfig
        from apps.tenants.models import ChannelManager, TenantReceptionSettings

        TenantReceptionSettings.objects.create(
            tenant=self.tenant,
            channel_manager=ChannelManager.CHANNEX,
        )
        IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.CHANNEX,
            is_active=True,
        )
        self.reservation.import_source = "channex"
        self.reservation.external_id = channex_external_id("test-booking-uuid")
        self.reservation.save(update_fields=["import_source", "external_id"])

        mock_client = mock_client_cls.return_value
        mock_client.upload_attachment.return_value = "attach-uuid-1"
        mock_client.send_booking_message.return_value = {
            "data": {
                "id": "outbound-image-1",
                "attributes": {
                    "message": "Room photo",
                    "sender": "property",
                    "booking_id": "test-booking-uuid",
                    "have_attachment": True,
                },
            }
        }

        response = self.client.post(
            f"{self.base}/send-image/",
            {
                "file": _tiny_jpeg(),
                "channel": "booking",
                "caption": "Room photo",
            },
            format="multipart",
            **self.auth,
        )
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        self.assertEqual(data["channel"], "booking")
        self.assertEqual(data["status"], "sent")
        self.assertEqual(data["message_type"], "image")
        self.assertIn("/channex-messages/", data["media_url"])
        mock_client.upload_attachment.assert_called_once()
        mock_client.send_booking_message.assert_called_once_with(
            "test-booking-uuid",
            "Room photo",
            attachment_id="attach-uuid-1",
        )
        self.assertTrue(
            ChannexMessage.objects.filter(channex_message_id="outbound-image-1").exists()
        )

        timeline = self.client.get(f"{self.base}/", **self.auth)
        self.assertEqual(len(timeline.json()), 1)
        self.assertEqual(timeline.json()[0]["message_type"], "image")

