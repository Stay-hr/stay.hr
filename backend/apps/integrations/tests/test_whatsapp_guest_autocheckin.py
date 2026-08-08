from datetime import date, datetime, time, timedelta
from io import BytesIO
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from PIL import Image

from apps.integrations.models import IntegrationConfig, WhatsAppMessage
from apps.integrations.tests.test_whatsapp_webhook import TEST_FERNET_KEY
from apps.integrations.whatsapp.runtime_config import WhatsAppRuntimeConfig
from apps.integrations.whatsapp.tasks import process_inbound_message
from apps.integrations.whatsapp.whatsapp_guest_autocheckin import (
    GUEST_AUTO_CHECKIN_BUTTON_ID,
    extract_booking_code_from_text,
    find_reservation_by_booking_code,
    handle_guest_autocheckin_inbound,
    is_guest_auto_checkin_button,
)
from apps.communications.guest_compose import HINT_CHECKIN_READY, HINT_DOCS_AWAITING_ARRIVAL
from apps.communications.models import GuestMessageDraft, GuestMessageIntent
from apps.properties.models import Property
from apps.reservations.models import Guest, IdDocument, Reservation, WhatsAppGuestAutocheckinSession
from apps.tenants.models import Tenant
from django.utils import timezone

TEST_WHATSAPP_ACCESS_TOKEN = "test-whatsapp-access-token"
ZAGREB = ZoneInfo("Europe/Zagreb")


@override_settings(STAY_INTEGRATION_FERNET_KEY=TEST_FERNET_KEY)
class WhatsAppGuestAutocheckinTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="uzorita", name="Uzorita", default_language="hr")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Uzorita",
            slug="uzorita",
            whatsapp_autocheckin_enabled=True,
            whatsapp_autocheckin_time=time(8, 0),
            timezone="Europe/Zagreb",
        )
        self.integration = IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.WHATSAPP,
            routing_key="1068791909660300",
            is_active=True,
        )
        self.integration.set_config_dict(
            {
                "phone_number_id": "1068791909660300",
            }
        )
        self.integration.save()
        self.runtime = WhatsAppRuntimeConfig.from_integration_dict(self.integration.get_config_dict())
        self.today = date(2026, 6, 7)
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booker_name="Ana Anić",
            booker_phone="+385922222222",
            booking_code="BCOM-777",
            check_in=self.today,
            check_out=self.today + timedelta(days=2),
            status=Reservation.Status.EXPECTED,
        )

    def test_extract_booking_code(self):
        self.assertEqual(extract_booking_code_from_text("BCOM-777"), "BCOM-777")
        self.assertEqual(extract_booking_code_from_text("Kod je 1234567890"), "1234567890")
        self.assertEqual(extract_booking_code_from_text("Booking: 5193574002"), "5193574002")
        self.assertEqual(extract_booking_code_from_text("5193 574 002"), "5193574002")
        self.assertIsNone(extract_booking_code_from_text("bok"))

    def test_find_reservation_by_booking_code(self):
        found = find_reservation_by_booking_code(tenant_id=self.tenant.pk, code="bcom-777")
        self.assertEqual(found.pk, self.reservation.pk)

    def test_guest_auto_checkin_button_id(self):
        self.assertTrue(
            is_guest_auto_checkin_button(button_id=GUEST_AUTO_CHECKIN_BUTTON_ID, text="")
        )
        self.assertTrue(is_guest_auto_checkin_button(text="Auto check-in"))

    @patch.dict("os.environ", {"WHATSAPP_ACCESS_TOKEN": TEST_WHATSAPP_ACCESS_TOKEN})
    @patch("apps.integrations.whatsapp.whatsapp_guest_autocheckin.send_text_message")
    def test_unknown_phone_asks_for_booking_code(self, mock_send):
        mock_send.return_value = {"messages": [{"id": "wamid.out.ask"}]}
        inbound = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            wamid="wamid.in.hello",
            wa_id="385933333333",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="text",
            body="Bok",
            raw_payload={"type": "text", "text": {"body": "Bok"}},
        )

        result = handle_guest_autocheckin_inbound(
            row=inbound,
            integration_row=self.integration,
            runtime=self.runtime,
            action_text="Bok",
            reservation=None,
        )

        self.assertEqual(result["status"], "sent")
        mock_send.assert_called_once()
        body = mock_send.call_args.kwargs["body"]
        self.assertIn("booking kod", body.lower())
        self.assertTrue(
            WhatsAppGuestAutocheckinSession.objects.filter(
                tenant_id=self.tenant.pk,
                wa_id="385933333333",
            ).exists()
        )

    @patch.dict("os.environ", {"WHATSAPP_ACCESS_TOKEN": TEST_WHATSAPP_ACCESS_TOKEN})
    @patch("apps.integrations.whatsapp.whatsapp_guest_autocheckin.send_interactive_button_message")
    @patch("apps.integrations.whatsapp.whatsapp_guest_autocheckin.property_local_now")
    def test_booking_code_resolves_and_sends_prompt(self, mock_now, mock_send):
        mock_now.return_value = datetime(2026, 6, 7, 9, 0, tzinfo=ZAGREB)
        mock_send.return_value = {"messages": [{"id": "wamid.out.prompt"}]}
        inbound = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            wamid="wamid.in.code",
            wa_id="385933333333",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="text",
            body="BCOM-777",
            raw_payload={"type": "text", "text": {"body": "BCOM-777"}},
        )

        result = handle_guest_autocheckin_inbound(
            row=inbound,
            integration_row=self.integration,
            runtime=self.runtime,
            action_text="BCOM-777",
            reservation=None,
        )

        self.assertEqual(result["status"], "autocheckin_prompt_sent")
        inbound.refresh_from_db()
        self.reservation.refresh_from_db()
        self.assertEqual(inbound.reservation_id, self.reservation.pk)
        self.assertIsNotNone(self.reservation.whatsapp_autocheckin_engaged_at)
        mock_send.assert_called_once()
        buttons = mock_send.call_args.kwargs["buttons"]
        self.assertEqual(buttons[0][0], GUEST_AUTO_CHECKIN_BUTTON_ID)

    @patch.dict("os.environ", {"WHATSAPP_ACCESS_TOKEN": TEST_WHATSAPP_ACCESS_TOKEN})
    @patch("apps.integrations.whatsapp.whatsapp_guest_autocheckin.send_text_message")
    def test_wrong_booking_code_sends_not_found(self, mock_send):
        mock_send.return_value = {"messages": [{"id": "wamid.out.notfound"}]}
        WhatsAppGuestAutocheckinSession.objects.create(
            tenant=self.tenant,
            wa_id="385915412198",
            status="awaiting_booking_code",
        )
        inbound = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            wamid="wamid.in.wrongcode",
            wa_id="385915412198",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="text",
            body="WRONG-CODE-999",
            raw_payload={"type": "text", "text": {"body": "WRONG-CODE-999"}},
        )

        result = handle_guest_autocheckin_inbound(
            row=inbound,
            integration_row=self.integration,
            runtime=self.runtime,
            action_text="WRONG-CODE-999",
            reservation=None,
        )

        self.assertEqual(result["status"], "sent")
        body = mock_send.call_args.kwargs["body"]
        self.assertIn("Ne prepoznajem", body)

    @patch.dict("os.environ", {"WHATSAPP_ACCESS_TOKEN": TEST_WHATSAPP_ACCESS_TOKEN})
    @patch("apps.integrations.whatsapp.whatsapp_guest_autocheckin.send_text_message")
    @patch("apps.integrations.whatsapp.whatsapp_guest_autocheckin.property_local_now")
    def test_alt_phone_code_match_before_checkin_auto_reply_off(self, mock_now, mock_send):
        mock_now.return_value = datetime(2026, 6, 6, 9, 0, tzinfo=ZAGREB)
        mock_send.return_value = {"messages": [{"id": "wamid.out.early"}]}
        runtime = WhatsAppRuntimeConfig.from_integration_dict(
            {
                **self.integration.get_config_dict(),
                "auto_reply": False,
            }
        )
        inbound = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            wamid="wamid.in.altphone",
            wa_id="385915412198",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="text",
            body="BCOM-777",
            raw_payload={"type": "text", "text": {"body": "BCOM-777"}},
        )

        result = handle_guest_autocheckin_inbound(
            row=inbound,
            integration_row=self.integration,
            runtime=runtime,
            action_text="BCOM-777",
            reservation=None,
        )

        self.assertEqual(result["status"], "sent")
        inbound.refresh_from_db()
        self.assertEqual(inbound.reservation_id, self.reservation.pk)
        body = mock_send.call_args.kwargs["body"]
        self.assertIn("BCOM-777", body)
        lowered = body.lower()
        self.assertTrue(
            "dan dolaska" in lowered or "arrival day" in lowered,
            msg=f"expected arrival-day hint, got: {body!r}",
        )

    def _tiny_jpeg(self) -> bytes:
        buf = BytesIO()
        Image.new("RGB", (20, 20), color=(200, 180, 160)).save(buf, format="JPEG")
        return buf.getvalue()

    def _guest_with_complete_documents(self) -> Guest:
        guest = Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="Ana",
            last_name="Anić",
            name="Ana Anić",
            date_of_birth=date(1990, 1, 1),
            document_number="ABC123456",
            nationality="HR",
            sex="F",
            is_primary=True,
        )
        doc = IdDocument.objects.create(guest=guest)
        doc.front_photo.save(
            f"guest_{guest.pk}_front.jpg",
            SimpleUploadedFile("front.jpg", self._tiny_jpeg()),
            save=True,
        )
        doc.back_photo.save(
            f"guest_{guest.pk}_back.jpg",
            SimpleUploadedFile("back.jpg", self._tiny_jpeg()),
            save=True,
        )
        return guest

    @patch.dict("os.environ", {"WHATSAPP_ACCESS_TOKEN": TEST_WHATSAPP_ACCESS_TOKEN})
    @patch("apps.communications.guest_arrival_inbound.llm_configured", return_value=False)
    @patch("apps.communications.guest_arrival_inbound.send_guest_message")
    @patch("apps.integrations.whatsapp.whatsapp_guest_autocheckin.send_interactive_button_message")
    @patch("apps.core.timezone.property_local_now")
    def test_complete_documents_parking_skips_autocheckin_prompt(
        self,
        mock_now,
        mock_interactive,
        mock_send_guest,
        _llm_off,
    ):
        mock_now.return_value = datetime(2026, 6, 7, 15, 0, tzinfo=ZAGREB)
        mock_send_guest.return_value = MagicMock(pk=1)
        self._guest_with_complete_documents()
        inbound = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            reservation=self.reservation,
            wamid="wamid.in.parking",
            wa_id="385922222222",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="text",
            body="We need parking and arrive at 8 PM",
            raw_payload={"type": "text", "text": {"body": "We need parking and arrive at 8 PM"}},
        )

        result = process_inbound_message(inbound.pk)

        self.assertEqual(result["status"], "guest_arrival_handled")
        mock_interactive.assert_not_called()
        mock_send_guest.assert_called_once()
        self.reservation.refresh_from_db()
        self.assertIsNotNone(self.reservation.guest_stated_arrival_at)

    @patch.dict("os.environ", {"WHATSAPP_ACCESS_TOKEN": TEST_WHATSAPP_ACCESS_TOKEN})
    @patch("apps.integrations.whatsapp.whatsapp_guest_autocheckin.send_text_message")
    @patch("apps.integrations.whatsapp.whatsapp_guest_autocheckin.send_interactive_button_message")
    @patch("apps.integrations.whatsapp.whatsapp_guest_autocheckin.property_local_now")
    def test_complete_documents_no_keywords_skips_auto_reply(self, mock_now, mock_interactive, mock_send):
        mock_now.return_value = datetime(2026, 6, 7, 9, 0, tzinfo=ZAGREB)
        self._guest_with_complete_documents()
        GuestMessageDraft.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            intent=GuestMessageIntent.REPLY,
            hint=HINT_DOCS_AWAITING_ARRIVAL,
            llm_body_text="Docs saved",
            final_body_text="Docs saved",
            language="hr",
            sent_at=timezone.now(),
        )
        inbound = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            reservation=self.reservation,
            wamid="wamid.in.hello",
            wa_id="385922222222",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="text",
            body="Hello",
            raw_payload={"type": "text", "text": {"body": "Hello"}},
        )

        result = handle_guest_autocheckin_inbound(
            row=inbound,
            integration_row=self.integration,
            runtime=self.runtime,
            action_text=inbound.body,
            reservation=self.reservation,
        )

        self.assertEqual(result["status"], "auto_reply_skipped")
        self.assertEqual(result["reason"], "no_matching_handler")
        mock_interactive.assert_not_called()
        mock_send.assert_not_called()

    @patch.dict("os.environ", {"WHATSAPP_ACCESS_TOKEN": TEST_WHATSAPP_ACCESS_TOKEN})
    @patch(
        "apps.integrations.whatsapp.whatsapp_post_checkin_reply.send_whatsapp_entrance_image_from_asset"
    )
    @patch("apps.integrations.whatsapp.whatsapp_post_checkin_reply.send_guest_message")
    @patch("apps.integrations.whatsapp.whatsapp_guest_autocheckin.send_interactive_button_message")
    @patch("apps.core.timezone.property_local_now")
    def test_complete_documents_second_parking_same_day_not_duplicated(
        self,
        mock_now,
        mock_interactive,
        mock_send_guest,
        mock_entrance,
    ):
        mock_now.return_value = datetime(2026, 6, 7, 18, 0, tzinfo=ZAGREB)

        def _mark_draft_sent(*, draft, **kwargs):
            draft.sent_at = timezone.now()
            draft.save(update_fields=["sent_at"])
            return MagicMock(pk=1)

        mock_send_guest.side_effect = _mark_draft_sent
        mock_entrance.return_value = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            reservation=self.reservation,
            wamid="wamid.out.img2",
            wa_id="385922222222",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.OUTBOUND,
            message_type="image",
            body="Entrance",
            raw_payload={},
        )
        self._guest_with_complete_documents()
        self.reservation.status = Reservation.Status.CHECKED_IN
        self.reservation.save(update_fields=["status", "updated_at"])
        for idx in range(2):
            inbound = WhatsAppMessage.objects.create(
                tenant=self.tenant,
                integration=self.integration,
                reservation=self.reservation,
                wamid=f"wamid.in.parking{idx}",
                wa_id="385922222222",
                phone_number_id="1068791909660300",
                direction=WhatsAppMessage.Direction.INBOUND,
                message_type="text",
                body="Where can I park?",
                raw_payload={"type": "text", "text": {"body": "Where can I park?"}},
            )
            result = handle_guest_autocheckin_inbound(
                row=inbound,
                integration_row=self.integration,
                runtime=self.runtime,
                action_text=inbound.body,
                reservation=self.reservation,
            )
            if idx == 0:
                self.assertEqual(result["status"], "post_checkin_reply_sent")
            else:
                self.assertEqual(result["status"], "auto_reply_skipped")
                self.assertEqual(result["reason"], "no_matching_handler")

        mock_send_guest.assert_called_once()
        mock_entrance.assert_called_once()

    @patch.dict("os.environ", {"WHATSAPP_ACCESS_TOKEN": TEST_WHATSAPP_ACCESS_TOKEN})
    @patch("apps.integrations.whatsapp.whatsapp_guest_autocheckin.send_text_message")
    @patch("apps.integrations.whatsapp.whatsapp_guest_autocheckin.send_interactive_button_message")
    def test_checked_in_autocheckin_button_replies_already_checked_in(
        self,
        mock_interactive,
        mock_send,
    ):
        mock_send.return_value = {"messages": [{"id": "wamid.out.already"}]}
        self.reservation.status = Reservation.Status.CHECKED_IN
        self.reservation.save(update_fields=["status", "updated_at"])
        inbound = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            reservation=self.reservation,
            wamid="wamid.in.autocheckin.checked",
            wa_id="385922222222",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="interactive",
            body="Auto check-in",
            raw_payload={
                "type": "interactive",
                "interactive": {
                    "type": "button_reply",
                    "button_reply": {"id": "guest_auto_checkin", "title": "Auto check-in"},
                },
            },
        )

        result = handle_guest_autocheckin_inbound(
            row=inbound,
            integration_row=self.integration,
            runtime=self.runtime,
            action_text="Auto check-in",
            reservation=self.reservation,
        )

        self.assertEqual(result["status"], "sent")
        mock_send.assert_called_once()
        mock_interactive.assert_not_called()
        body = mock_send.call_args.kwargs["body"]
        self.assertIn("već ste prijavljeni", body.lower())
        self.reservation.refresh_from_db()
        self.assertIsNotNone(self.reservation.whatsapp_autocheckin_waived_at)

    @patch.dict("os.environ", {"WHATSAPP_ACCESS_TOKEN": TEST_WHATSAPP_ACCESS_TOKEN})
    @patch("apps.integrations.whatsapp.whatsapp_guest_autocheckin.send_text_message")
    @patch("apps.integrations.whatsapp.whatsapp_guest_autocheckin.send_interactive_button_message")
    def test_engaged_reservation_skips_repeat_autocheckin_prompt(
        self,
        mock_interactive,
        mock_send,
    ):
        self.reservation.whatsapp_autocheckin_engaged_at = timezone.now()
        self.reservation.save(update_fields=["whatsapp_autocheckin_engaged_at", "updated_at"])
        inbound = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            reservation=self.reservation,
            wamid="wamid.in.engaged.skip",
            wa_id="385922222222",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="text",
            body="",
            raw_payload={"type": "text", "text": {"body": ""}},
        )

        result = handle_guest_autocheckin_inbound(
            row=inbound,
            integration_row=self.integration,
            runtime=self.runtime,
            action_text="",
            reservation=self.reservation,
        )

        self.assertEqual(result["status"], "auto_reply_skipped")
        self.assertEqual(result["reason"], "autocheckin_awaiting_documents")
        mock_send.assert_not_called()
        mock_interactive.assert_not_called()

    @patch.dict("os.environ", {"WHATSAPP_ACCESS_TOKEN": TEST_WHATSAPP_ACCESS_TOKEN})
    @patch("apps.integrations.whatsapp.whatsapp_guest_autocheckin.send_text_message")
    @patch("apps.integrations.whatsapp.whatsapp_guest_autocheckin.send_interactive_button_message")
    def test_checked_in_unrecognized_question_skips_auto_reply(
        self,
        mock_interactive,
        mock_send,
    ):
        self._guest_with_complete_documents()
        self.reservation.status = Reservation.Status.CHECKED_IN
        self.reservation.save(update_fields=["status", "updated_at"])
        inbound = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            reservation=self.reservation,
            wamid="wamid.in.key",
            wa_id="385922222222",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="text",
            body="Wo können wir morgen früh den Zimmerschlüssel abgeben?",
            raw_payload={
                "type": "text",
                "text": {"body": "Wo können wir morgen früh den Zimmerschlüssel abgeben?"},
            },
        )

        result = handle_guest_autocheckin_inbound(
            row=inbound,
            integration_row=self.integration,
            runtime=self.runtime,
            action_text=inbound.body,
            reservation=self.reservation,
        )

        self.assertEqual(result["status"], "auto_reply_skipped")
        self.assertEqual(result["reason"], "no_matching_handler")
        mock_interactive.assert_not_called()
        mock_send.assert_not_called()

    @patch.dict("os.environ", {"WHATSAPP_ACCESS_TOKEN": TEST_WHATSAPP_ACCESS_TOKEN})
    @patch(
        "apps.integrations.whatsapp.whatsapp_post_checkin_reply.send_whatsapp_entrance_image_from_asset"
    )
    @patch("apps.integrations.whatsapp.whatsapp_post_checkin_reply.send_guest_message")
    @patch("apps.integrations.whatsapp.whatsapp_guest_autocheckin.send_interactive_button_message")
    @patch("apps.core.timezone.property_local_now")
    def test_checkin_ready_draft_acknowledges_parking_without_complete_docs(
        self,
        mock_now,
        mock_interactive,
        mock_send_guest,
        mock_entrance,
    ):
        mock_now.return_value = datetime(2026, 6, 7, 15, 0, tzinfo=ZAGREB)
        mock_send_guest.return_value = MagicMock(pk=1)
        mock_entrance.return_value = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            reservation=self.reservation,
            wamid="wamid.out.img3",
            wa_id="385922222222",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.OUTBOUND,
            message_type="image",
            body="Entrance",
            raw_payload={},
        )
        GuestMessageDraft.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            intent=GuestMessageIntent.REPLY,
            hint=HINT_CHECKIN_READY,
            llm_body_text="Check-in ready",
            final_body_text="Check-in ready",
            language="en",
            sent_at=timezone.now(),
        )
        inbound = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            reservation=self.reservation,
            wamid="wamid.in.arrival.parking",
            wa_id="385922222222",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="text",
            body="I think we will arrive at 7 PM. Where can I park?",
            raw_payload={
                "type": "text",
                "text": {"body": "I think we will arrive at 7 PM. Where can I park?"},
            },
        )

        result = handle_guest_autocheckin_inbound(
            row=inbound,
            integration_row=self.integration,
            runtime=self.runtime,
            action_text=inbound.body,
            reservation=self.reservation,
        )

        self.assertEqual(result["status"], "post_checkin_reply_sent")
        mock_interactive.assert_not_called()
        mock_send_guest.assert_called_once()
        mock_entrance.assert_called_once()

    @patch.dict("os.environ", {"WHATSAPP_ACCESS_TOKEN": TEST_WHATSAPP_ACCESS_TOKEN})
    @patch("apps.integrations.whatsapp.whatsapp_guest_autocheckin.send_interactive_button_message")
    @patch("apps.integrations.whatsapp.evisitor_reply.send_text_message")
    @patch("apps.integrations.whatsapp.autocheckin_docs_deadline.property_local_now")
    def test_engaged_before_checkin_no_docs_sends_period_ended(
        self, mock_now, mock_send, mock_interactive
    ):
        mock_now.return_value = datetime(2026, 6, 7, 10, 0, tzinfo=ZAGREB)
        mock_send.return_value = {"messages": [{"id": "wamid.out.period"}]}
        self.reservation.whatsapp_autocheckin_engaged_at = timezone.now()
        self.reservation.save(update_fields=["whatsapp_autocheckin_engaged_at", "updated_at"])
        inbound = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            reservation=self.reservation,
            wamid="wamid.in.period",
            wa_id="385922222222",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="text",
            body="Gdje je parking?",
            raw_payload={"type": "text", "text": {"body": "Gdje je parking?"}},
        )

        result = handle_guest_autocheckin_inbound(
            row=inbound,
            integration_row=self.integration,
            runtime=self.runtime,
            action_text=inbound.body,
            reservation=self.reservation,
        )

        self.assertEqual(result["status"], "sent")
        mock_interactive.assert_not_called()
        mock_send.assert_called_once()
        body = mock_send.call_args.kwargs["body"]
        self.assertIn("automatski online check-in", body.lower())

    @patch.dict("os.environ", {"WHATSAPP_ACCESS_TOKEN": TEST_WHATSAPP_ACCESS_TOKEN})
    @patch("apps.integrations.whatsapp.whatsapp_guest_autocheckin.send_interactive_button_message")
    @patch("apps.integrations.whatsapp.whatsapp_guest_autocheckin.property_local_now")
    def test_incomplete_documents_without_ack_draft_sends_autocheckin(
        self, mock_now, mock_interactive
    ):
        mock_now.return_value = datetime(2026, 6, 7, 9, 0, tzinfo=ZAGREB)
        mock_interactive.return_value = {"messages": [{"id": "wamid.out.prompt"}]}
        inbound = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            reservation=self.reservation,
            wamid="wamid.in.free",
            wa_id="385922222222",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="text",
            body="Hello reception",
            raw_payload={"type": "text", "text": {"body": "Hello reception"}},
        )

        result = handle_guest_autocheckin_inbound(
            row=inbound,
            integration_row=self.integration,
            runtime=self.runtime,
            action_text=inbound.body,
            reservation=self.reservation,
        )

        self.assertEqual(result["status"], "autocheckin_prompt_sent")
        mock_interactive.assert_called_once()

    @patch("apps.communications.guest_arrival_inbound.llm_configured", return_value=False)
    @patch("apps.communications.guest_arrival_inbound.send_guest_message")
    @patch("apps.core.timezone.property_local_now")
    def test_expected_docs_complete_arrival_saves_time_and_replies(
        self,
        mock_now,
        mock_send_guest,
        _llm_off,
    ):
        mock_now.return_value = datetime(2026, 6, 7, 15, 0, tzinfo=ZAGREB)
        mock_send_guest.return_value = MagicMock(pk=1)
        self._guest_with_complete_documents()
        from apps.communications.guest_arrival_inbound import maybe_handle_guest_arrival_inbound

        result = maybe_handle_guest_arrival_inbound(
            self.reservation,
            "Dolazimo oko 18:00...19:00",
            channel="whatsapp",
        )

        self.assertEqual(result["status"], "guest_arrival_handled")
        mock_send_guest.assert_called_once()
        self.reservation.refresh_from_db()
        self.assertEqual(
            self.reservation.guest_stated_arrival_at,
            datetime(2026, 6, 7, 19, 0, tzinfo=ZAGREB),
        )
