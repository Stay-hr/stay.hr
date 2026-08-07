from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.integrations.models import IntegrationConfig, WhatsAppMessage
from apps.integrations.tests.test_whatsapp_webhook import TEST_FERNET_KEY
from apps.integrations.whatsapp.apply_reply import waive_whatsapp_autocheckin
from apps.integrations.whatsapp.autocheckin_waive import send_autocheckin_waived_whatsapp
from apps.integrations.whatsapp.tasks import process_inbound_message
from apps.integrations.whatsapp.whatsapp_document_batch import on_whatsapp_document_received
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant

TEST_WHATSAPP_ACCESS_TOKEN = "test-whatsapp-access-token"


@override_settings(
    STAY_INTEGRATION_FERNET_KEY=TEST_FERNET_KEY,
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class WhatsAppAutocheckinWaivedTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="uzorita", name="Uzorita", default_language="hr")
        self.property = Property.objects.create(tenant=self.tenant, name="Uzorita B&B", slug="uzorita")
        self.integration = IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.WHATSAPP,
            routing_key="1068791909660300",
            is_active=True,
        )
        self.integration.set_config_dict(
            {
                "phone_number_id": "1068791909660300",
                "auto_reply": True,
            }
        )
        self.integration.save()
        today = timezone.localdate()
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booker_name="François Hartweg",
            booker_phone="+33674174251",
            adults_count=2,
            check_in=today,
            check_out=today + timedelta(days=1),
            status=Reservation.Status.EXPECTED,
        )
        waive_whatsapp_autocheckin(self.reservation)
        self.reservation.refresh_from_db()

    @patch.dict("os.environ", {"WHATSAPP_ACCESS_TOKEN": TEST_WHATSAPP_ACCESS_TOKEN})
    @patch("apps.integrations.whatsapp.tasks.send_text_message")
    def test_waived_auto_checkin_button_skips_documents(self, mock_send):
        inbound = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            reservation=self.reservation,
            wamid="wamid.in.waived.button",
            wa_id="33674174251",
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

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "autocheckin_waived")
        mock_send.assert_not_called()

    @patch.dict("os.environ", {"WHATSAPP_ACCESS_TOKEN": TEST_WHATSAPP_ACCESS_TOKEN})
    @patch("apps.communications.guest_arrival_inbound.send_guest_message")
    def test_waived_arrival_text_sends_thanks_only(self, mock_send):
        mock_send.return_value = MagicMock(pk=1)
        inbound = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            reservation=self.reservation,
            wamid="wamid.in.waived.arrival",
            wa_id="33674174251",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="text",
            body="We will arrive around 7 pm",
            raw_payload={"type": "text", "text": {"body": "We will arrive around 7 pm"}},
        )

        result = process_inbound_message(inbound.pk)

        self.assertEqual(result["status"], "guest_arrival_handled")
        mock_send.assert_called_once()
        body = mock_send.call_args.kwargs["body_text"]
        lowered = body.lower()
        self.assertTrue("arriv" in lowered or "dolask" in lowered)
        self.assertNotIn("parking", lowered)
        self.assertNotIn("parkir", lowered)

    @patch("apps.integrations.whatsapp.whatsapp_document_batch.fetch_whatsapp_media")
    def test_waived_media_skips_document_batch(self, mock_fetch):
        inbound = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            reservation=self.reservation,
            wamid="wamid.in.waived.image",
            wa_id="33674174251",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="image",
            body="",
            raw_payload={"type": "image", "image": {"id": "media-waived"}},
        )

        result = on_whatsapp_document_received(inbound.pk)

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "waived")
        mock_fetch.assert_not_called()

    @patch.dict("os.environ", {"WHATSAPP_ACCESS_TOKEN": TEST_WHATSAPP_ACCESS_TOKEN})
    @patch("apps.integrations.whatsapp.tasks.send_text_message")
    def test_waived_random_text_skips(self, mock_send):
        inbound = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            reservation=self.reservation,
            wamid="wamid.in.waived.hello",
            wa_id="33674174251",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="text",
            body="Hello",
            raw_payload={"type": "text", "text": {"body": "Hello"}},
        )

        result = process_inbound_message(inbound.pk)

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "autocheckin_waived")
        mock_send.assert_not_called()

    @patch.dict("os.environ", {"WHATSAPP_ACCESS_TOKEN": TEST_WHATSAPP_ACCESS_TOKEN})
    @patch("apps.integrations.whatsapp.autocheckin_waive.send_guest_message")
    def test_send_autocheckin_waived_sets_waived_at(self, mock_send):
        fresh = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booker_name="Ana Anić",
            booker_phone="+385911111111",
            adults_count=2,
            check_in=timezone.localdate(),
            check_out=timezone.localdate() + timedelta(days=2),
            status=Reservation.Status.EXPECTED,
        )
        mock_send.return_value = MagicMock(pk=1)

        result = send_autocheckin_waived_whatsapp(fresh)

        self.assertEqual(result["status"], "sent")
        fresh.refresh_from_db()
        self.assertIsNotNone(fresh.whatsapp_autocheckin_waived_at)
        mock_send.assert_called_once()
        body = mock_send.call_args.kwargs["body_text"]
        self.assertIn("Auto check-in", body)
