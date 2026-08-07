from datetime import date
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.integrations.models import IntegrationConfig, WhatsAppMessage
from apps.integrations.tests.test_whatsapp_webhook import TEST_FERNET_KEY
from apps.integrations.whatsapp.whatsapp_document_batch import (
    AFTER_NO_SECONDS,
    CONFIRM_TIMEOUT_SECONDS,
    QUIET_SECONDS,
    assess_batch_after_quiet,
    document_batch_after_no_quiet,
    document_batch_confirm_timeout,
    document_batch_quiet_elapsed,
    finalize_whatsapp_document_batch,
    handle_whatsapp_document_batch_reply,
    is_documents_all_no_reply,
    is_documents_all_yes_reply,
    on_whatsapp_document_received,
)
from apps.properties.models import Property
from apps.reservations.models import (
    DocumentIntakeImage,
    DocumentIntakeJob,
    DocumentIntakeJobStatus,
    WhatsAppDocumentBatchSession,
    WhatsAppDocumentBatchStatus,
    Reservation,
)
from apps.reservations.document_intake_completeness import DocumentIntakeCompleteness
from apps.reservations.tests.fixtures.document_intake.load_fixture import build_reservation_from_fixture
from apps.tenants.models import Tenant


@override_settings(
    STAY_INTEGRATION_FERNET_KEY=TEST_FERNET_KEY,
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
    GUEST_CHECKIN_WEB_ONLY=False,
)
class WhatsAppDocumentBatchTests(TestCase):
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
            adults_count=2,
            check_in=date(2026, 7, 1),
            check_out=date(2026, 7, 3),
            status=Reservation.Status.EXPECTED,
        )

    def _image_message(self, *, pk_suffix: str, wamid: str) -> WhatsAppMessage:
        return WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            reservation=self.reservation,
            wamid=wamid,
            wa_id="385911111111",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="image",
            body="",
            raw_payload={
                "type": "image",
                "image": {"id": f"media-{pk_suffix}", "mime_type": "image/jpeg"},
            },
        )

    def test_reply_detection(self):
        self.assertTrue(is_documents_all_yes_reply(button_id="docs_all_yes"))
        self.assertTrue(is_documents_all_yes_reply(text="Da"))
        self.assertTrue(is_documents_all_yes_reply(text="Yes"))
        self.assertTrue(is_documents_all_no_reply(button_id="docs_all_no"))
        self.assertTrue(is_documents_all_no_reply(text="Ne"))
        self.assertFalse(is_documents_all_yes_reply(text="Auto check-in"))

    @patch("apps.integrations.whatsapp.whatsapp_document_batch._schedule_task")
    @patch("apps.integrations.whatsapp.whatsapp_document_batch.fetch_whatsapp_media")
    @patch("apps.core.tasks.notify_guest_message_inbound.delay")
    def test_three_images_one_job_before_finalize(self, mock_notify, mock_fetch, mock_schedule):
        mock_fetch.return_value = (b"fake-image-bytes", "image/jpeg")
        messages = [self._image_message(pk_suffix=str(i), wamid=f"wamid.in.{i}") for i in range(3)]

        for message in messages:
            result = on_whatsapp_document_received(message.pk)
            self.assertEqual(result["status"], "collected")

        session = WhatsAppDocumentBatchSession.objects.get(reservation=self.reservation)
        self.assertEqual(session.status, WhatsAppDocumentBatchStatus.COLLECTING)
        self.assertEqual(session.job.images.count(), 3)
        self.assertEqual(DocumentIntakeJob.objects.filter(reservation=self.reservation).count(), 1)
        mock_notify.assert_not_called()

    def _mock_ocr_preview_complete(self, mock_process, mock_rematch, mock_eval):
        def _process(ctx):
            job = ctx.job
            job.status = DocumentIntakeJobStatus.DONE
            job.ocr_result = {"persons": []}
            job.save(update_fields=["status", "ocr_result", "updated_at"])

        mock_process.side_effect = _process
        mock_rematch.return_value = []
        mock_eval.return_value = DocumentIntakeCompleteness(is_complete=True)

    def _mock_ocr_preview_incomplete(self, mock_process, mock_rematch, mock_eval):
        def _process(ctx):
            job = ctx.job
            job.status = DocumentIntakeJobStatus.DONE
            job.ocr_result = {"persons": []}
            job.save(update_fields=["status", "ocr_result", "updated_at"])

        mock_process.side_effect = _process
        mock_rematch.return_value = []
        mock_eval.return_value = DocumentIntakeCompleteness(
            is_complete=False,
            missing_guests=[],
            missing_sides=[],
            unmatched_persons=[],
        )

    @patch("apps.reservations.document_intake_completeness.evaluate_completeness")
    @patch("apps.reservations.document_intake_audit.rematch_and_audit_job")
    @patch("apps.reservations.document_intake_service.process_document_intake_job")
    @patch("apps.integrations.whatsapp.whatsapp_document_batch._send_batch_text_ack")
    @patch("apps.integrations.whatsapp.whatsapp_document_batch.send_interactive_button_message")
    @patch("apps.integrations.whatsapp.whatsapp_document_batch._schedule_task")
    @patch("apps.integrations.whatsapp.whatsapp_document_batch.fetch_whatsapp_media")
    def test_quiet_elapsed_sends_interactive_prompt(
        self,
        mock_fetch,
        mock_schedule,
        mock_send,
        mock_text_ack,
        mock_process,
        mock_rematch,
        mock_eval,
    ):
        mock_fetch.return_value = (b"fake-image-bytes", "image/jpeg")
        mock_send.return_value = {"messages": [{"id": "wamid.out.prompt"}]}
        mock_text_ack.return_value = {"status": "sent"}
        self._mock_ocr_preview_complete(mock_process, mock_rematch, mock_eval)
        message = self._image_message(pk_suffix="1", wamid="wamid.in.1")
        on_whatsapp_document_received(message.pk)

        session = WhatsAppDocumentBatchSession.objects.get(reservation=self.reservation)
        session.last_media_at = timezone.now() - timezone.timedelta(seconds=QUIET_SECONDS + 1)
        session.save(update_fields=["last_media_at", "updated_at"])

        result = document_batch_quiet_elapsed(session.pk)

        self.assertEqual(result["status"], "assessed")
        self.assertEqual(result["preview"], "complete")
        mock_send.assert_called_once()
        mock_text_ack.assert_called_once()
        session.refresh_from_db()
        self.assertEqual(session.status, WhatsAppDocumentBatchStatus.AWAITING_CONFIRM)
        self.assertEqual(session.prompt_count, 1)

    @patch("apps.reservations.document_intake_completeness.evaluate_completeness")
    @patch("apps.reservations.document_intake_audit.rematch_and_audit_job")
    @patch("apps.reservations.document_intake_service.process_document_intake_job")
    @patch("apps.integrations.whatsapp.whatsapp_document_batch._send_batch_text_ack")
    @patch("apps.integrations.whatsapp.whatsapp_document_batch.send_interactive_button_message")
    @patch("apps.integrations.whatsapp.whatsapp_document_batch._schedule_task")
    @patch("apps.integrations.whatsapp.whatsapp_document_batch.fetch_whatsapp_media")
    def test_quiet_elapsed_runs_assess_once(
        self,
        mock_fetch,
        mock_schedule,
        mock_send,
        mock_text_ack,
        mock_process,
        mock_rematch,
        mock_eval,
    ):
        mock_fetch.return_value = (b"fake-image-bytes", "image/jpeg")
        mock_send.return_value = {"messages": [{"id": "wamid.out.prompt"}]}
        mock_text_ack.return_value = {"status": "sent"}
        self._mock_ocr_preview_complete(mock_process, mock_rematch, mock_eval)
        message = self._image_message(pk_suffix="dup", wamid="wamid.in.dup")
        on_whatsapp_document_received(message.pk)

        session = WhatsAppDocumentBatchSession.objects.get(reservation=self.reservation)
        session.last_media_at = timezone.now() - timezone.timedelta(seconds=QUIET_SECONDS + 1)
        session.save(update_fields=["last_media_at", "updated_at"])

        first = document_batch_quiet_elapsed(session.pk)
        second = document_batch_quiet_elapsed(session.pk)

        self.assertEqual(first["status"], "assessed")
        self.assertEqual(second["status"], "skipped")
        self.assertEqual(second["reason"], "not_collecting")
        mock_send.assert_called_once()
        mock_text_ack.assert_called_once()

    @patch("apps.integrations.whatsapp.document_intake_finalize.finalize_document_intake_job")
    @patch("apps.integrations.whatsapp.whatsapp_document_batch.send_interactive_button_message")
    @patch("apps.integrations.whatsapp.whatsapp_document_batch._schedule_task")
    @patch("apps.integrations.whatsapp.whatsapp_document_batch.fetch_whatsapp_media")
    @patch("apps.core.tasks.notify_guest_message_inbound.delay")
    def test_yes_finalizes_once(
        self,
        mock_notify,
        mock_fetch,
        mock_schedule,
        mock_send,
        mock_finalize,
    ):
        mock_fetch.return_value = (b"fake-image-bytes", "image/jpeg")
        mock_send.return_value = {"messages": [{"id": "wamid.out.prompt"}]}
        mock_finalize.return_value = {"status": "docs_saved", "job_id": 1}

        message = self._image_message(pk_suffix="1", wamid="wamid.in.yes")
        on_whatsapp_document_received(message.pk)
        session = WhatsAppDocumentBatchSession.objects.get(reservation=self.reservation)
        session.status = WhatsAppDocumentBatchStatus.AWAITING_CONFIRM
        session.save(update_fields=["status", "updated_at"])

        yes_message = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            reservation=self.reservation,
            wamid="wamid.in.yes.reply",
            wa_id="385911111111",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="interactive",
            body="",
            raw_payload={
                "type": "interactive",
                "interactive": {
                    "type": "button_reply",
                    "button_reply": {"id": "docs_all_yes", "title": "Da"},
                },
            },
        )

        result = handle_whatsapp_document_batch_reply(yes_message.pk)

        self.assertEqual(result["status"], "docs_saved")
        mock_finalize.assert_called_once()
        mock_notify.assert_called_once()
        session.refresh_from_db()
        self.assertEqual(session.status, WhatsAppDocumentBatchStatus.DONE)

    @patch("apps.integrations.whatsapp.whatsapp_document_batch.send_interactive_button_message")
    @patch("apps.integrations.whatsapp.whatsapp_document_batch._schedule_task")
    @patch("apps.integrations.whatsapp.whatsapp_document_batch.fetch_whatsapp_media")
    def test_no_then_quiet_reprompts(self, mock_fetch, mock_schedule, mock_send):
        mock_fetch.return_value = (b"fake-image-bytes", "image/jpeg")
        mock_send.return_value = {"messages": [{"id": "wamid.out.prompt"}]}

        message = self._image_message(pk_suffix="1", wamid="wamid.in.no")
        on_whatsapp_document_received(message.pk)
        session = WhatsAppDocumentBatchSession.objects.get(reservation=self.reservation)
        session.status = WhatsAppDocumentBatchStatus.AWAITING_CONFIRM
        session.save(update_fields=["status", "updated_at"])

        no_message = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            reservation=self.reservation,
            wamid="wamid.in.no.reply",
            wa_id="385911111111",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="interactive",
            body="",
            raw_payload={
                "type": "interactive",
                "interactive": {
                    "type": "button_reply",
                    "button_reply": {"id": "docs_all_no", "title": "Ne"},
                },
            },
        )

        result = handle_whatsapp_document_batch_reply(no_message.pk)
        self.assertEqual(result["status"], "after_no")
        session.refresh_from_db()
        self.assertEqual(session.status, WhatsAppDocumentBatchStatus.AFTER_NO)

        result = document_batch_after_no_quiet(session.pk)
        self.assertEqual(result["status"], "prompted")
        self.assertEqual(mock_send.call_count, 1)
        session.refresh_from_db()
        self.assertEqual(session.status, WhatsAppDocumentBatchStatus.AWAITING_CONFIRM)
        self.assertEqual(session.prompt_count, 1)

    @patch("apps.integrations.whatsapp.whatsapp_document_batch._schedule_task")
    @patch("apps.integrations.whatsapp.whatsapp_document_batch.fetch_whatsapp_media")
    def test_new_image_during_after_no_resets_collecting(self, mock_fetch, mock_schedule):
        mock_fetch.return_value = (b"fake-image-bytes", "image/jpeg")
        message = self._image_message(pk_suffix="1", wamid="wamid.in.reset")
        on_whatsapp_document_received(message.pk)
        session = WhatsAppDocumentBatchSession.objects.get(reservation=self.reservation)
        session.status = WhatsAppDocumentBatchStatus.AFTER_NO
        session.after_no_at = timezone.now() - timezone.timedelta(seconds=5)
        session.save(update_fields=["status", "after_no_at", "updated_at"])

        message2 = self._image_message(pk_suffix="2", wamid="wamid.in.reset2")
        result = on_whatsapp_document_received(message2.pk)

        self.assertEqual(result["status"], "collected")
        session.refresh_from_db()
        self.assertEqual(session.status, WhatsAppDocumentBatchStatus.COLLECTING)
        self.assertEqual(session.job.images.count(), 2)

    @patch("apps.integrations.whatsapp.document_intake_finalize.finalize_document_intake_job")
    @patch("apps.integrations.whatsapp.whatsapp_document_batch._schedule_task")
    @patch("apps.integrations.whatsapp.whatsapp_document_batch.fetch_whatsapp_media")
    @patch("apps.core.tasks.notify_guest_message_inbound.delay")
    def test_confirm_timeout_finalizes(
        self,
        mock_notify,
        mock_fetch,
        mock_schedule,
        mock_finalize,
    ):
        mock_fetch.return_value = (b"fake-image-bytes", "image/jpeg")
        mock_finalize.return_value = {"status": "docs_saved"}
        message = self._image_message(pk_suffix="1", wamid="wamid.in.timeout")
        on_whatsapp_document_received(message.pk)
        session = WhatsAppDocumentBatchSession.objects.get(reservation=self.reservation)
        session.status = WhatsAppDocumentBatchStatus.AWAITING_CONFIRM
        session.prompt_sent_at = timezone.now() - timezone.timedelta(seconds=CONFIRM_TIMEOUT_SECONDS + 1)
        session.save(update_fields=["status", "prompt_sent_at", "updated_at"])

        result = document_batch_confirm_timeout(session.pk)

        self.assertEqual(result["status"], "docs_saved")
        mock_finalize.assert_called_once()
        mock_notify.assert_called_once()

    @patch("apps.integrations.whatsapp.document_intake_finalize.finalize_document_intake_job")
    @patch("apps.integrations.whatsapp.whatsapp_document_batch._schedule_task")
    @patch("apps.integrations.whatsapp.whatsapp_document_batch.fetch_whatsapp_media")
    def test_finalize_incomplete_skips_notify(self, mock_fetch, mock_schedule, mock_finalize):
        mock_fetch.return_value = (b"fake-image-bytes", "image/jpeg")
        mock_finalize.return_value = {"status": "incomplete"}
        message = self._image_message(pk_suffix="incomplete", wamid="wamid.in.incomplete")
        on_whatsapp_document_received(message.pk)
        session = WhatsAppDocumentBatchSession.objects.get(reservation=self.reservation)
        session.status = WhatsAppDocumentBatchStatus.AWAITING_CONFIRM
        session.save(update_fields=["status", "updated_at"])

        with patch("apps.core.tasks.notify_guest_message_inbound.delay") as mock_notify:
            result = finalize_whatsapp_document_batch(session.pk)
            self.assertEqual(result["status"], "incomplete")
            mock_finalize.assert_called_once()
            mock_notify.assert_not_called()

    @patch("apps.integrations.whatsapp.document_intake_finalize.finalize_document_intake_job")
    @patch("apps.integrations.whatsapp.whatsapp_document_batch._schedule_task")
    @patch("apps.integrations.whatsapp.whatsapp_document_batch.fetch_whatsapp_media")
    def test_confirm_timeout_return_is_json_serializable(self, mock_fetch, mock_schedule, mock_finalize):
        import json

        mock_fetch.return_value = (b"fake-image-bytes", "image/jpeg")
        mock_finalize.return_value = {
            "status": "incomplete",
            "job_id": 1,
            "completeness": {"is_complete": False, "missing_guests": [], "missing_sides": [], "unmatched_persons": []},
        }
        message = self._image_message(pk_suffix="json", wamid="wamid.in.json")
        on_whatsapp_document_received(message.pk)
        session = WhatsAppDocumentBatchSession.objects.get(reservation=self.reservation)
        session.status = WhatsAppDocumentBatchStatus.AWAITING_CONFIRM
        session.prompt_sent_at = timezone.now() - timezone.timedelta(seconds=CONFIRM_TIMEOUT_SECONDS + 1)
        session.save(update_fields=["status", "prompt_sent_at", "updated_at"])

        result = document_batch_confirm_timeout(session.pk)
        json.dumps(result)
        self.assertEqual(result["status"], "incomplete")

    @patch("apps.integrations.whatsapp.whatsapp_document_batch._schedule_task")
    @patch("apps.integrations.whatsapp.whatsapp_document_batch.fetch_whatsapp_media")
    def test_duplicate_message_skipped(self, mock_fetch, mock_schedule):
        mock_fetch.return_value = (b"fake-image-bytes", "image/jpeg")
        message = self._image_message(pk_suffix="dup", wamid="wamid.in.dup")
        on_whatsapp_document_received(message.pk)
        result = on_whatsapp_document_received(message.pk)
        self.assertEqual(result["status"], "duplicate")
        session = WhatsAppDocumentBatchSession.objects.get(reservation=self.reservation)
        self.assertEqual(session.job.images.count(), 1)

    @patch(
        "apps.integrations.whatsapp.guest_document_lifecycle.is_document_checkin_complete",
        return_value=True,
    )
    @patch("apps.integrations.whatsapp.whatsapp_document_batch.fetch_whatsapp_media")
    def test_image_skipped_when_docs_already_complete(self, mock_fetch, _mock_docs_complete):
        message = self._image_message(pk_suffix="complete", wamid="wamid.in.complete")
        result = on_whatsapp_document_received(message.pk)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "documents_complete")
        mock_fetch.assert_not_called()
        self.assertFalse(
            WhatsAppDocumentBatchSession.objects.filter(reservation=self.reservation).exists()
        )
        self.assertFalse(DocumentIntakeJob.objects.filter(reservation=self.reservation).exists())

    @patch("apps.integrations.whatsapp.whatsapp_document_batch._send_batch_additional_photo_ack")
    @patch("apps.integrations.whatsapp.whatsapp_document_batch._schedule_task")
    @patch("apps.integrations.whatsapp.whatsapp_document_batch.fetch_whatsapp_media")
    def test_image_during_awaiting_confirm_sends_ack(self, mock_fetch, mock_schedule, mock_ack):
        mock_fetch.return_value = (b"fake-image-bytes", "image/jpeg")
        mock_ack.return_value = {"status": "sent"}
        message = self._image_message(pk_suffix="ack1", wamid="wamid.in.ack1")
        on_whatsapp_document_received(message.pk)
        session = WhatsAppDocumentBatchSession.objects.get(reservation=self.reservation)
        session.status = WhatsAppDocumentBatchStatus.AWAITING_CONFIRM
        session.prompt_count = 1
        session.prompt_sent_at = timezone.now()
        session.save(update_fields=["status", "prompt_count", "prompt_sent_at", "updated_at"])

        message2 = self._image_message(pk_suffix="ack2", wamid="wamid.in.ack2")
        result = on_whatsapp_document_received(message2.pk)

        self.assertEqual(result["status"], "collected")
        self.assertTrue(result.get("confirm_interrupted"))
        mock_ack.assert_called_once()
        session.refresh_from_db()
        self.assertEqual(session.status, WhatsAppDocumentBatchStatus.COLLECTING)
        self.assertIsNotNone(session.confirm_interrupted_at)
        self.assertEqual(session.job.images.count(), 2)

    @patch("apps.integrations.whatsapp.document_intake_finalize.finalize_document_intake_job")
    @patch("apps.integrations.whatsapp.whatsapp_document_batch._send_batch_additional_photo_ack")
    @patch("apps.integrations.whatsapp.whatsapp_document_batch._schedule_task")
    @patch("apps.integrations.whatsapp.whatsapp_document_batch.fetch_whatsapp_media")
    @patch("apps.core.tasks.notify_guest_message_inbound.delay")
    def test_yes_after_late_images_finalizes(
        self,
        mock_notify,
        mock_fetch,
        mock_schedule,
        mock_ack,
        mock_finalize,
    ):
        mock_fetch.return_value = (b"fake-image-bytes", "image/jpeg")
        mock_ack.return_value = {"status": "sent"}
        mock_finalize.return_value = {"status": "docs_saved", "job_id": 1}

        message = self._image_message(pk_suffix="m1", wamid="wamid.in.m1")
        on_whatsapp_document_received(message.pk)
        session = WhatsAppDocumentBatchSession.objects.get(reservation=self.reservation)
        session.status = WhatsAppDocumentBatchStatus.AWAITING_CONFIRM
        session.prompt_count = 1
        session.prompt_sent_at = timezone.now()
        session.save(update_fields=["status", "prompt_count", "prompt_sent_at", "updated_at"])

        for idx, suffix in enumerate(("m2", "m3"), start=2):
            on_whatsapp_document_received(
                self._image_message(pk_suffix=suffix, wamid=f"wamid.in.{suffix}").pk
            )

        yes_message = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            reservation=self.reservation,
            wamid="wamid.in.mauro.yes",
            wa_id="385911111111",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="interactive",
            body="Da",
            raw_payload={
                "type": "interactive",
                "interactive": {
                    "type": "button_reply",
                    "button_reply": {"id": "docs_all_yes", "title": "Da"},
                },
            },
        )

        result = handle_whatsapp_document_batch_reply(yes_message.pk)

        self.assertEqual(result["status"], "docs_saved")
        mock_finalize.assert_called_once()
        session.refresh_from_db()
        self.assertEqual(session.status, WhatsAppDocumentBatchStatus.DONE)

    @patch("apps.reservations.document_intake_completeness.evaluate_completeness")
    @patch("apps.reservations.document_intake_audit.rematch_and_audit_job")
    @patch("apps.reservations.document_intake_service.process_document_intake_job")
    @patch("apps.integrations.whatsapp.whatsapp_document_batch._send_batch_text_ack")
    @patch("apps.integrations.whatsapp.whatsapp_document_batch.send_interactive_button_message")
    @patch("apps.integrations.whatsapp.whatsapp_document_batch._schedule_task")
    @patch("apps.integrations.whatsapp.whatsapp_document_batch.fetch_whatsapp_media")
    def test_quiet_runs_ocr_preview_incomplete(
        self,
        mock_fetch,
        mock_schedule,
        mock_send,
        mock_text_ack,
        mock_process,
        mock_rematch,
        mock_eval,
    ):
        mock_fetch.return_value = (b"fake-image-bytes", "image/jpeg")
        mock_send.return_value = {"messages": [{"id": "wamid.out.prompt"}]}
        mock_text_ack.return_value = {"status": "sent"}
        self._mock_ocr_preview_incomplete(mock_process, mock_rematch, mock_eval)
        message = self._image_message(pk_suffix="inc", wamid="wamid.in.inc")
        on_whatsapp_document_received(message.pk)
        session = WhatsAppDocumentBatchSession.objects.get(reservation=self.reservation)
        session.last_media_at = timezone.now() - timezone.timedelta(seconds=QUIET_SECONDS + 1)
        session.save(update_fields=["last_media_at", "updated_at"])

        with patch(
            "apps.integrations.whatsapp.document_intake_finalize.finalize_document_intake_job"
        ) as mock_finalize:
            result = document_batch_quiet_elapsed(session.pk)

        self.assertEqual(result["status"], "assessed")
        self.assertEqual(result["preview"], "incomplete")
        mock_finalize.assert_not_called()
        mock_text_ack.assert_called_once()
        mock_send.assert_not_called()
        session.refresh_from_db()
        self.assertEqual(session.status, WhatsAppDocumentBatchStatus.COLLECTING)

    @patch("apps.reservations.document_intake_completeness.evaluate_completeness")
    @patch("apps.reservations.document_intake_audit.rematch_and_audit_job")
    @patch("apps.reservations.document_intake_service.process_document_intake_job")
    @patch("apps.integrations.whatsapp.whatsapp_document_batch._send_batch_text_ack")
    @patch("apps.integrations.whatsapp.whatsapp_document_batch.send_interactive_button_message")
    @patch("apps.integrations.whatsapp.whatsapp_document_batch._schedule_task")
    @patch("apps.integrations.whatsapp.whatsapp_document_batch.fetch_whatsapp_media")
    def test_quiet_runs_ocr_preview_complete_reprompts(
        self,
        mock_fetch,
        mock_schedule,
        mock_send,
        mock_text_ack,
        mock_process,
        mock_rematch,
        mock_eval,
    ):
        mock_fetch.return_value = (b"fake-image-bytes", "image/jpeg")
        mock_send.return_value = {"messages": [{"id": "wamid.out.prompt"}]}
        mock_text_ack.return_value = {"status": "sent"}
        self._mock_ocr_preview_complete(mock_process, mock_rematch, mock_eval)
        message = self._image_message(pk_suffix="cmp", wamid="wamid.in.cmp")
        on_whatsapp_document_received(message.pk)
        session = WhatsAppDocumentBatchSession.objects.get(reservation=self.reservation)
        session.last_media_at = timezone.now() - timezone.timedelta(seconds=QUIET_SECONDS + 1)
        session.save(update_fields=["last_media_at", "updated_at"])

        with patch(
            "apps.integrations.whatsapp.document_intake_finalize.finalize_document_intake_job"
        ) as mock_finalize:
            result = document_batch_quiet_elapsed(session.pk)

        self.assertEqual(result["status"], "assessed")
        self.assertEqual(result["preview"], "complete")
        mock_finalize.assert_not_called()
        mock_text_ack.assert_called_once()
        mock_send.assert_called_once()

    def _setup_978_batch_session(self):
        self.reservation.delete()
        self.reservation, _guests, ocr_data, _meta = build_reservation_from_fixture(
            tenant=self.tenant,
            property=self.property,
            scenario="978",
        )
        self.reservation.booker_phone = "+385911111111"
        self.reservation.save(update_fields=["booker_phone"])
        message = self._image_message(pk_suffix="978", wamid="wamid.in.978")
        job = DocumentIntakeJob.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            whatsapp_message=message,
            status=DocumentIntakeJobStatus.QUEUED,
            device_id="whatsapp",
        )
        for idx in range(4):
            DocumentIntakeImage.objects.create(
                tenant=self.tenant,
                job=job,
                image=ContentFile(b"fake-passport", name=f"wa_{message.pk}_{idx}.jpg"),
                sort_order=idx,
                detected_side="passport",
            )
        session = WhatsAppDocumentBatchSession.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            job=job,
            wa_id="385911111111",
            status=WhatsAppDocumentBatchStatus.COLLECTING,
            last_media_at=timezone.now() - timezone.timedelta(seconds=QUIET_SECONDS + 1),
        )
        return session, ocr_data

    @patch("apps.reservations.document_intake_service.crop_face_jpeg", return_value=None)
    @patch("apps.integrations.whatsapp.whatsapp_document_batch._send_batch_text_ack")
    @patch("apps.integrations.whatsapp.whatsapp_document_batch.send_interactive_button_message")
    def test_quiet_assess_partial_applies_before_incomplete_reply(
        self,
        mock_send,
        mock_text_ack,
        _mock_crop,
    ):
        mock_text_ack.return_value = {"status": "sent"}
        session, ocr_data = self._setup_978_batch_session()
        job = session.job

        def _process(ctx):
            ctx.job.status = DocumentIntakeJobStatus.DONE
            ctx.job.ocr_result = {
                "persons": ocr_data["persons"],
                "images": ocr_data.get("images") or [],
            }
            ctx.job.save(update_fields=["status", "ocr_result", "updated_at"])

        with patch(
            "apps.reservations.document_intake_service.process_document_intake_job",
            side_effect=_process,
        ):
            result = assess_batch_after_quiet(session)

        job.refresh_from_db()
        self.assertEqual(result["status"], "assessed")
        self.assertTrue(job.applied_result)
        self.assertEqual(len(job.applied_result), 4)
        self.assertIn(result["preview"], {"incomplete", "complete"})

    @patch("apps.reservations.document_intake_service.crop_face_jpeg", return_value=None)
    @patch("apps.integrations.whatsapp.whatsapp_document_batch._send_batch_text_ack")
    @patch("apps.integrations.whatsapp.whatsapp_document_batch.send_interactive_button_message")
    def test_quiet_assess_skips_apply_on_retry(
        self,
        mock_send,
        mock_text_ack,
        _mock_crop,
    ):
        mock_text_ack.return_value = {"status": "sent"}
        session, ocr_data = self._setup_978_batch_session()
        job = session.job

        def _process(ctx):
            ctx.job.status = DocumentIntakeJobStatus.DONE
            ctx.job.ocr_result = {
                "persons": ocr_data["persons"],
                "images": ocr_data.get("images") or [],
            }
            ctx.job.save(update_fields=["status", "ocr_result", "updated_at"])

        with patch(
            "apps.reservations.document_intake_service.process_document_intake_job",
            side_effect=_process,
        ):
            first = assess_batch_after_quiet(session)
            job.refresh_from_db()
            first_applied = list(job.applied_result or [])
            self.assertEqual(len(first_applied), 4)

            session.status = WhatsAppDocumentBatchStatus.COLLECTING
            session.save(update_fields=["status", "updated_at"])
            session.last_media_at = timezone.now() - timezone.timedelta(seconds=QUIET_SECONDS + 1)
            session.save(update_fields=["last_media_at", "updated_at"])

            with patch(
                "apps.reservations.document_intake_service.apply_document_intake_job"
            ) as mock_apply:
                second = assess_batch_after_quiet(session)
                mock_apply.assert_not_called()

        job.refresh_from_db()
        self.assertEqual(list(job.applied_result or []), first_applied)
        self.assertEqual(first["status"], "assessed")
        self.assertEqual(second["status"], "skipped")
        self.assertEqual(second["reason"], "documents_complete")


class WhatsAppDocumentBatchTimerTests(TestCase):
    def test_schedule_task_uses_unique_ids_and_tracks_in_cache(self):
        from django.core.cache import cache

        from apps.integrations.whatsapp import whatsapp_document_batch as batch

        cache.clear()
        seen_ids: list[str] = []

        class FakeTask:
            def apply_async(self, *, args, countdown, task_id):
                seen_ids.append(task_id)
                return task_id

        fake = FakeTask()
        batch._schedule_task(task=fake, session_id=7, countdown=10, suffix="quiet")
        batch._schedule_task(task=fake, session_id=7, countdown=10, suffix="quiet")

        self.assertEqual(len(seen_ids), 2)
        self.assertNotEqual(seen_ids[0], seen_ids[1])
        self.assertTrue(all(task_id.startswith("wa-doc-quiet-7-") for task_id in seen_ids))
        self.assertEqual(cache.get(batch._timer_cache_key(7, "quiet")), seen_ids[1])

    @patch("config.celery.app.control.revoke")
    def test_revoke_scheduled_only_revokes_tracked_task(self, mock_revoke):
        from django.core.cache import cache

        from apps.integrations.whatsapp import whatsapp_document_batch as batch

        cache.clear()
        cache.set(batch._timer_cache_key(3, "quiet"), "wa-doc-quiet-3-abc123", timeout=60)

        batch._revoke_scheduled(3, "quiet")

        mock_revoke.assert_called_once_with("wa-doc-quiet-3-abc123", terminate=False)
        self.assertIsNone(cache.get(batch._timer_cache_key(3, "quiet")))
