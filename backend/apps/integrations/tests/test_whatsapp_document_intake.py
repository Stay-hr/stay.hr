from datetime import date
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.integrations.models import IntegrationConfig, WhatsAppMessage
from apps.integrations.tests.test_whatsapp_webhook import TEST_FERNET_KEY
from apps.integrations.whatsapp.document_intake_task import process_whatsapp_document_message
from apps.properties.models import Property
from apps.reservations.models import (
    DocumentIntakeJob,
    DocumentIntakeJobStatus,
    WhatsAppDocumentBatchSession,
    WhatsAppDocumentBatchStatus,
    Reservation,
)
from apps.tenants.models import Tenant


@override_settings(
    STAY_INTEGRATION_FERNET_KEY=TEST_FERNET_KEY,
    CELERY_TASK_ALWAYS_EAGER=True,
)
class WhatsAppDocumentIntakeTaskTests(TestCase):
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
                "phone_number_id": "1068791909660300",
                "auto_reply": False,
            }
        )
        self.integration.save()
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booker_name="Ana Anić",
            booker_phone="+385911111111",
            check_in=date(2026, 7, 1),
            check_out=date(2026, 7, 3),
            status=Reservation.Status.EXPECTED,
        )
        self.message = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            reservation=self.reservation,
            wamid="wamid.in.image",
            wa_id="385911111111",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="image",
            body="",
            raw_payload={"type": "image", "image": {"id": "media-123", "mime_type": "image/jpeg"}},
        )

    @patch("apps.integrations.whatsapp.whatsapp_document_batch._schedule_task")
    @patch("apps.integrations.whatsapp.whatsapp_document_batch.fetch_whatsapp_media")
    @patch("apps.core.tasks.notify_guest_message_inbound.delay")
    def test_creates_batch_session_without_instant_ocr(self, mock_notify, mock_fetch, mock_schedule):
        mock_fetch.return_value = (b"fake-image-bytes", "image/jpeg")

        result = process_whatsapp_document_message(self.message.pk)

        self.assertEqual(result["status"], "collected")
        session = WhatsAppDocumentBatchSession.objects.get(reservation=self.reservation)
        self.assertEqual(session.status, WhatsAppDocumentBatchStatus.COLLECTING)
        job = DocumentIntakeJob.objects.get(pk=result["job_id"])
        self.assertEqual(job.status, DocumentIntakeJobStatus.QUEUED)
        self.assertEqual(job.images.count(), 1)
        mock_notify.assert_not_called()

    @patch("apps.integrations.whatsapp.whatsapp_document_batch._schedule_task")
    @patch("apps.integrations.whatsapp.whatsapp_document_batch.fetch_whatsapp_media")
    def test_skips_duplicate_message(self, mock_fetch, mock_schedule):
        mock_fetch.return_value = (b"fake-image-bytes", "image/jpeg")
        process_whatsapp_document_message(self.message.pk)
        result = process_whatsapp_document_message(self.message.pk)
        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(DocumentIntakeJob.objects.filter(reservation=self.reservation).count(), 1)
