from datetime import date, timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.integrations.models import IntegrationConfig, WhatsAppMessage
from apps.integrations.tests.test_whatsapp_webhook import TEST_FERNET_KEY
from apps.integrations.whatsapp.tasks import is_auto_checkin_quick_reply, process_inbound_message
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant

TEST_D360_KEY = "test-d360-key"


@override_settings(
    STAY_INTEGRATION_FERNET_KEY=TEST_FERNET_KEY,
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
    # Production guest document channel is web-only; these cases assert that path.
    GUEST_CHECKIN_WEB_ONLY=True,
)
class WhatsAppAutocheckinInboundTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="uzorita", name="Uzorita", default_language="hr")
        self.property = Property.objects.create(tenant=self.tenant, name="Uzorita", slug="uzorita")
        self.integration = IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.WHATSAPP,
            routing_key="1068791909660300",
            is_active=True,
        )
        self.integration.set_config_dict(
            {
                "provider": "360dialog",
                "phone_number_id": "1068791909660300",
                "access_token": TEST_D360_KEY,
                "api_base_url": "https://waba-v2.360dialog.io",
                "auto_reply": True,
            }
        )
        self.integration.save()
        today = timezone.localdate()
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booker_name="Ana Anić",
            booker_phone="+385911111111",
            adults_count=2,
            check_in=today,
            check_out=today + timedelta(days=2),
            status=Reservation.Status.EXPECTED,
        )

    def test_quick_reply_detection(self):
        self.assertTrue(is_auto_checkin_quick_reply("Auto check-in"))
        self.assertTrue(is_auto_checkin_quick_reply("Auto-Check-in"))
        self.assertFalse(is_auto_checkin_quick_reply("Bok"))

    @patch.dict("os.environ", {"D360_API_KEY": TEST_D360_KEY})
    @patch("apps.integrations.whatsapp.tasks.send_text_message")
    def test_template_button_sends_documents_text(self, mock_send):
        mock_send.return_value = {"messages": [{"id": "wamid.out.doc.button"}]}
        inbound = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            reservation=self.reservation,
            wamid="wamid.in.template.button",
            wa_id="385911111111",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="button",
            body="",
            raw_payload={
                "type": "button",
                "button": {"text": "Auto check in", "payload": "Auto check in"},
            },
        )

        result = process_inbound_message(inbound.pk)

        self.assertEqual(result["status"], "web_checkin_sent")
        mock_send.assert_called_once()
        self.reservation.refresh_from_db()
        self.assertIsNotNone(self.reservation.whatsapp_autocheckin_engaged_at)
        body = mock_send.call_args.kwargs["body"]
        self.assertIn("check-in", body.lower())

    @patch.dict("os.environ", {"D360_API_KEY": TEST_D360_KEY})
    @patch("apps.integrations.whatsapp.tasks.send_text_message")
    def test_quick_reply_sends_web_checkin_link(self, mock_send):
        mock_send.return_value = {"messages": [{"id": "wamid.out.doc"}]}
        inbound = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            reservation=self.reservation,
            wamid="wamid.in.quick",
            wa_id="385911111111",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="interactive",
            body="Auto check-in",
            raw_payload={
                "type": "interactive",
                "interactive": {
                    "type": "button_reply",
                    "button_reply": {"id": "1", "title": "Auto check-in"},
                },
            },
        )

        result = process_inbound_message(inbound.pk)

        self.assertEqual(result["status"], "web_checkin_sent")
        mock_send.assert_called_once()
        self.reservation.refresh_from_db()
        self.assertIsNotNone(self.reservation.whatsapp_autocheckin_engaged_at)
        body = mock_send.call_args.kwargs["body"]
        self.assertIn("check-in", body.lower())

    @patch.dict("os.environ", {"D360_API_KEY": TEST_D360_KEY})
    @patch("apps.integrations.whatsapp.tasks.send_text_message")
    def test_template_button_sends_web_checkin_when_auto_reply_disabled(self, mock_send):
        self.integration.set_config_dict(
            {
                "provider": "360dialog",
                "phone_number_id": "1068791909660300",
                "access_token": TEST_D360_KEY,
                "api_base_url": "https://waba-v2.360dialog.io",
                "auto_reply": False,
            }
        )
        self.integration.save()
        mock_send.return_value = {"messages": [{"id": "wamid.out.doc.button.disabled"}]}
        inbound = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            reservation=self.reservation,
            wamid="wamid.in.template.button.disabled",
            wa_id="385911111111",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="button",
            body="",
            raw_payload={
                "type": "button",
                "button": {"text": "Auto check in", "payload": "Auto check in"},
            },
        )

        result = process_inbound_message(inbound.pk)

        self.assertEqual(result["status"], "web_checkin_sent")
        mock_send.assert_called_once()
        self.reservation.refresh_from_db()
        self.assertIsNotNone(self.reservation.whatsapp_autocheckin_engaged_at)

    @patch.dict("os.environ", {"D360_API_KEY": TEST_D360_KEY})
    @patch("apps.integrations.whatsapp.whatsapp_web_checkin_redirect.send_text_message")
    @patch("apps.integrations.whatsapp.tasks.on_whatsapp_document_received.delay")
    def test_inbound_image_sends_web_checkin_link(self, mock_doc_task, mock_send):
        mock_send.return_value = {"messages": [{"id": "wamid.out.web.image"}]}
        inbound = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            reservation=self.reservation,
            wamid="wamid.in.image",
            wa_id="385911111111",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="image",
            body="",
            raw_payload={"type": "image", "image": {"id": "media-1"}},
        )

        result = process_inbound_message(inbound.pk)

        self.assertEqual(result["status"], "web_checkin_sent")
        mock_send.assert_called_once()
        mock_doc_task.assert_not_called()

    @patch("apps.integrations.whatsapp.whatsapp_document_batch.send_interactive_button_message")
    @patch("apps.integrations.whatsapp.tasks.send_text_message")
    def test_autocheckin_during_awaiting_confirm_re_prompts(self, mock_send, mock_interactive):
        from apps.reservations.models import (
            DocumentIntakeJob,
            DocumentIntakeJobStatus,
            WhatsAppDocumentBatchSession,
            WhatsAppDocumentBatchStatus,
        )

        mock_interactive.return_value = {"messages": [{"id": "wamid.out.confirm"}]}
        job = DocumentIntakeJob.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            status=DocumentIntakeJobStatus.DONE,
        )
        WhatsAppDocumentBatchSession.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            job=job,
            wa_id="385911111111",
            status=WhatsAppDocumentBatchStatus.AWAITING_CONFIRM,
            prompt_sent_at=timezone.now(),
        )
        inbound = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            reservation=self.reservation,
            wamid="wamid.in.autocheckin.during.confirm",
            wa_id="385911111111",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="interactive",
            body="Autocheck-in",
            raw_payload={
                "type": "interactive",
                "interactive": {
                    "type": "button_reply",
                    "button_reply": {"id": "guest_auto_checkin", "title": "Autocheck-in"},
                },
            },
        )

        result = process_inbound_message(inbound.pk)

        self.assertEqual(result["status"], "batch_awaiting_confirm")
        mock_send.assert_not_called()
        mock_interactive.assert_called_once()

    @patch.dict("os.environ", {"D360_API_KEY": TEST_D360_KEY})
    @patch("apps.integrations.whatsapp.whatsapp_guest_autocheckin.send_text_message")
    def test_autocheckin_button_when_already_checked_in(self, mock_send):
        mock_send.return_value = {"messages": [{"id": "wamid.out.already"}]}
        self.reservation.status = Reservation.Status.CHECKED_IN
        self.reservation.save(update_fields=["status", "updated_at"])
        inbound = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            reservation=self.reservation,
            wamid="wamid.in.autocheckin.checked.in",
            wa_id="385911111111",
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

        result = process_inbound_message(inbound.pk)

        self.assertEqual(result["status"], "sent")
        mock_send.assert_called_once()
        body = mock_send.call_args.kwargs["body"]
        self.assertIn("već ste prijavljeni", body.lower())
        self.reservation.refresh_from_db()
        self.assertIsNotNone(self.reservation.whatsapp_autocheckin_waived_at)
        self.assertIsNone(self.reservation.whatsapp_autocheckin_engaged_at)

    @patch("apps.integrations.whatsapp.whatsapp_guest_autocheckin.send_interactive_button_message")
    @patch("apps.integrations.whatsapp.whatsapp_guest_autocheckin.send_text_message")
    def test_unsupported_inbound_skips_autocheckin_prompt(self, mock_send, mock_interactive):
        inbound = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            reservation=self.reservation,
            wamid="wamid.in.unsupported",
            wa_id="385911111111",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="unsupported",
            body="",
            raw_payload={"type": "unsupported", "errors": [{"code": 131051}]},
        )

        result = process_inbound_message(inbound.pk)

        self.assertEqual(result["status"], "auto_reply_skipped")
        self.assertEqual(result["reason"], "unsupported")
        mock_send.assert_not_called()
        mock_interactive.assert_not_called()
