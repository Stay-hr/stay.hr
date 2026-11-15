import hashlib
import hmac
import json
import os
from unittest.mock import patch

from django.test import RequestFactory, override_settings

from apps.integrations.models import IntegrationConfig, WhatsAppMediaArchive, WhatsAppMessage
from apps.integrations.tests.media_archive_support import PHONE_NUMBER_ID, REAL_ENQUEUE, ArchiveTestBase
from apps.integrations.whatsapp.webhook_views import WhatsAppWebhookView

SECRET = "media-archive-app-secret"
FERNET = "M8U_DJpQILQrKpxTOVtRrQp3nR0LJHAl2X0x-7JOH5k="


def _payload(*, message: dict | None = None, echo: dict | None = None) -> dict:
    value = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "+385911111111", "phone_number_id": PHONE_NUMBER_ID},
    }
    change = {"field": "messages", "value": value}
    if message is not None:
        value["contacts"] = [{"profile": {"name": "Ana"}, "wa_id": message["from"]}]
        value["messages"] = [message]
    if echo is not None:
        change["field"] = "smb_message_echoes"
        value["message_echoes"] = [echo]
    return {"object": "whatsapp_business_account", "entry": [{"id": "W", "changes": [change]}]}


def _image(wamid="wamid.img1", media_id="media-1") -> dict:
    return {
        "from": "385911111111", "id": wamid, "timestamp": "1710000000", "type": "image",
        "image": {"id": media_id, "mime_type": "image/jpeg"},
    }


@override_settings(ROOT_URLCONF="config.urls", STAY_INTEGRATION_FERNET_KEY=FERNET)
class MediaArchiveWebhookTests(ArchiveTestBase):
    def setUp(self):
        super().setUp()
        os.environ["WHATSAPP_APP_SECRET"] = SECRET
        os.environ["WHATSAPP_WEBHOOK_VERIFY_TOKEN"] = "t"
        self.addCleanup(os.environ.pop, "WHATSAPP_APP_SECRET", None)
        self.addCleanup(os.environ.pop, "WHATSAPP_WEBHOOK_VERIFY_TOKEN", None)
        patcher = patch("apps.integrations.whatsapp.webhook_service.process_inbound_message")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.factory = RequestFactory()

    def post(self, payload: dict):
        raw = json.dumps(payload).encode()
        sig = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
        request = self.factory.post(
            "/api/v1/integrations/whatsapp/webhook/", data=raw,
            content_type="application/json", HTTP_X_HUB_SIGNATURE_256=sig,
        )
        return WhatsAppWebhookView.as_view()(request)

    def test_media_message_creates_pending_row_and_enqueues_after_commit(self):
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            self.assertEqual(self.post(_payload(message=_image())).status_code, 200)
        archive = WhatsAppMediaArchive.objects.get()
        self.assertEqual(
            (archive.status, archive.kind, archive.media_id, archive.wamid),
            ("pending", "image", "media-1", "wamid.img1"),
        )
        self.assertEqual(len(callbacks), 1)
        self.enqueue.assert_called_once_with(archive.pk)

    def test_text_message_creates_no_row(self):
        text = {"from": "385911111111", "id": "wamid.t", "timestamp": "1", "type": "text", "text": {"body": "hi"}}
        with self.captureOnCommitCallbacks(execute=True):
            self.post(_payload(message=text))
        self.assertEqual(WhatsAppMediaArchive.objects.count(), 0)
        self.enqueue.assert_not_called()

    def test_redelivered_webhook_keeps_single_row_and_single_enqueue(self):
        for _ in range(3):
            with self.captureOnCommitCallbacks(execute=True):
                self.post(_payload(message=_image()))
        self.assertEqual(WhatsAppMessage.objects.count(), 1)
        self.assertEqual(WhatsAppMediaArchive.objects.count(), 1)
        self.assertEqual(self.enqueue.call_count, 1)

    def test_redelivery_heals_missing_row(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.post(_payload(message=_image()))
        WhatsAppMediaArchive.objects.all().delete()
        self.enqueue.reset_mock()
        with self.captureOnCommitCallbacks(execute=True):
            self.post(_payload(message=_image()))
        self.assertEqual(WhatsAppMediaArchive.objects.count(), 1)
        self.assertEqual(self.enqueue.call_count, 1)

    def test_row_creation_failure_does_not_break_webhook(self):
        with patch(
            "apps.integrations.whatsapp.media_archive.ensure_media_archive",
            side_effect=RuntimeError("db hiccup"),
        ):
            response = self.post(_payload(message=_image()))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(WhatsAppMessage.objects.count(), 1)
        self.assertEqual(WhatsAppMediaArchive.objects.count(), 0)

    def test_enqueue_failure_is_swallowed_and_row_stays_pending(self):
        self.enqueue.side_effect = REAL_ENQUEUE
        with patch(
            "apps.integrations.whatsapp.media_archive_tasks.archive_whatsapp_media.delay",
            side_effect=RuntimeError("redis down"),
        ):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.post(_payload(message=_image()))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(WhatsAppMediaArchive.objects.get().status, "pending")

    def test_invalid_media_id_stores_message_but_creates_no_archive_row(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.post(_payload(message=_image(wamid="wamid.bad", media_id="../../v1/me")))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(WhatsAppMessage.objects.filter(wamid="wamid.bad").count(), 1)
        self.assertEqual(WhatsAppMediaArchive.objects.count(), 0)
        self.enqueue.assert_not_called()

    def test_flag_off_creates_nothing(self):
        with override_settings(WHATSAPP_MEDIA_ARCHIVE_ENABLED=False):
            with self.captureOnCommitCallbacks(execute=True):
                self.post(_payload(message=_image()))
        self.assertEqual(WhatsAppMessage.objects.count(), 1)
        self.assertEqual(WhatsAppMediaArchive.objects.count(), 0)

    def test_business_app_echo_media_is_archived(self):
        echo = {
            "from": "385900000000", "to": "385911111111", "id": "wamid.echo1", "timestamp": "1710000000",
            "type": "video", "video": {"id": "vid-1", "mime_type": "video/mp4"},
        }
        with self.captureOnCommitCallbacks(execute=True):
            self.post(_payload(echo=echo))
        archive = WhatsAppMediaArchive.objects.get()
        self.assertEqual((archive.kind, archive.sensitivity), ("video", "standard"))
        self.assertEqual(archive.message.direction, WhatsAppMessage.Direction.OUTBOUND)
        self.enqueue.assert_called_once_with(archive.pk)

    def test_operator_flow_message_is_still_archived(self):
        # Archive row is written at ingestion, before operator/guest routing runs.
        with self.captureOnCommitCallbacks(execute=True):
            self.post(_payload(message=_image(wamid="wamid.op", media_id="op-media")))
        self.assertTrue(WhatsAppMediaArchive.objects.filter(media_id="op-media").exists())
