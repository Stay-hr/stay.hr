from datetime import date, timedelta
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.integrations.models import IntegrationConfig, WhatsAppMessage
from apps.integrations.tests.test_whatsapp_webhook import TEST_FERNET_KEY
from apps.integrations.whatsapp.runtime_config import WhatsAppRuntimeConfig
from apps.integrations.whatsapp.tasks import process_inbound_message
from apps.integrations.whatsapp.whatsapp_operator import (
    is_operator_wa_id,
    operator_name_for_wa_id,
)
from apps.integrations.whatsapp.whatsapp_operator_batch import send_operator_collect_prompt_for_session
from apps.integrations.whatsapp.whatsapp_operator_service import (
    OPERATOR_CHECKIN_BUTTON_ID,
    OPERATOR_CHECKIN_BUTTON_TITLE,
    OPERATOR_DOCS_ALL_NO_ID,
    OPERATOR_DOCS_ALL_YES_ID,
    _format_match_candidates,
    handle_operator_inbound,
    is_operator_checkin_command,
    is_operator_checkin_trigger,
    is_operator_docs_all_no_reply,
    is_operator_docs_all_yes_reply,
)
from apps.properties.models import Property
from apps.integrations.whatsapp.operator_reservation_pick import format_reservation_pick_line
from apps.reservations.guest_slots import PLACEHOLDER_FIRST, PLACEHOLDER_LAST
from apps.reservations.models import (
    DocumentIntakeImage,
    DocumentIntakeJob,
    DocumentIntakeJobSource,
    DocumentIntakeJobStatus,
    Guest,
    WhatsAppArrivalConfirmSession,
    WhatsAppArrivalConfirmSessionStatus,
    WhatsAppArrivalConfirmTrigger,
    WhatsAppOperatorSession,
    WhatsAppOperatorSessionStatus,
    Reservation,
)
from apps.tenants.models import Tenant, TenantReceptionSettings


@override_settings(
    STAY_INTEGRATION_FERNET_KEY=TEST_FERNET_KEY,
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class WhatsAppOperatorTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="uzorita", name="Uzorita", default_language="hr")
        TenantReceptionSettings.objects.create(
            tenant=self.tenant,
            whatsapp_operator_phones=[
                {"name": "Toni Šupe", "phone": "+385998388513"},
                {"name": "Ante Vrcan", "phone": "+385976713511"},
            ],
        )
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
                "auto_reply": True,
            }
        )
        self.integration.save()
        self.operator_wa_id = "385998388513"
        self.other_reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booker_name="Ante Vrcan",
            booker_phone="+385976713511",
            booker_email="ante.test@example.com",
            adults_count=1,
            check_in=date(2026, 7, 1),
            check_out=date(2026, 7, 3),
            status=Reservation.Status.EXPECTED,
        )
        self.target_reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booker_name="Robert Siebinger",
            booker_phone="+4369912014899",
            booker_email="guest@example.com",
            adults_count=1,
            check_in=date(2026, 7, 1),
            check_out=date(2026, 7, 3),
            status=Reservation.Status.EXPECTED,
            booking_code="UZO-TEST",
        )
        self.runtime = WhatsAppRuntimeConfig.from_integration_dict(self.integration.get_config_dict())
        self._interactive_wamid_counter = 0

    def _next_interactive_wamid(self) -> dict:
        self._interactive_wamid_counter += 1
        return {"messages": [{"id": f"wamid.out.interactive.{self._interactive_wamid_counter}"}]}

    def _send_operator_image(self, wamid: str, media_id: str = "media-op-1") -> WhatsAppMessage:
        message = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            wamid=wamid,
            wa_id=self.operator_wa_id,
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="image",
            body="",
            raw_payload={
                "type": "image",
                "image": {"id": media_id, "mime_type": "image/jpeg"},
            },
        )
        handle_operator_inbound(
            row=message,
            integration_row=self.integration,
            runtime=self.runtime,
            action_text="",
        )
        return message

    def _send_operator_checkin(self, wamid: str) -> dict:
        message = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            wamid=wamid,
            wa_id=self.operator_wa_id,
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="text",
            body="check-in",
            raw_payload={"type": "text", "text": {"body": "check-in"}},
        )
        return handle_operator_inbound(
            row=message,
            integration_row=self.integration,
            runtime=self.runtime,
            action_text="check-in",
        )

    def _send_operator_docs_yes(self, wamid: str) -> dict:
        message = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            wamid=wamid,
            wa_id=self.operator_wa_id,
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="interactive",
            body="Da",
            raw_payload={
                "type": "interactive",
                "interactive": {
                    "type": "button_reply",
                    "button_reply": {"id": OPERATOR_DOCS_ALL_YES_ID, "title": "Da"},
                },
            },
        )
        return handle_operator_inbound(
            row=message,
            integration_row=self.integration,
            runtime=self.runtime,
            action_text="Da",
            button_id=OPERATOR_DOCS_ALL_YES_ID,
        )

    def test_operator_lookup(self):
        self.assertTrue(is_operator_wa_id(tenant_id=self.tenant.pk, wa_id="385998388513"))
        self.assertTrue(is_operator_wa_id(tenant_id=self.tenant.pk, wa_id="+385976713511"))
        self.assertFalse(is_operator_wa_id(tenant_id=self.tenant.pk, wa_id="385911111111"))
        self.assertEqual(
            operator_name_for_wa_id(tenant_id=self.tenant.pk, wa_id="385998388513"),
            "Toni Šupe",
        )

    def test_checkin_command_normalization(self):
        self.assertTrue(is_operator_checkin_command("check-in"))
        self.assertTrue(is_operator_checkin_command("check in"))
        self.assertTrue(is_operator_checkin_command("CHECKIN"))
        self.assertFalse(is_operator_checkin_command("auto check-in"))

    def test_checkin_trigger_button(self):
        self.assertTrue(is_operator_checkin_trigger(button_id=OPERATOR_CHECKIN_BUTTON_ID))
        self.assertTrue(is_operator_checkin_trigger(text="check-in"))
        self.assertFalse(is_operator_checkin_trigger(button_id="docs_all_yes"))

    def test_docs_confirm_reply_detection(self):
        self.assertTrue(is_operator_docs_all_yes_reply(button_id=OPERATOR_DOCS_ALL_YES_ID))
        self.assertTrue(is_operator_docs_all_yes_reply(text="da"))
        self.assertTrue(is_operator_docs_all_no_reply(button_id=OPERATOR_DOCS_ALL_NO_ID))
        self.assertTrue(is_operator_docs_all_no_reply(text="ne"))

    @patch("apps.integrations.whatsapp.whatsapp_operator_batch.schedule_operator_quiet_timer")
    @patch("apps.integrations.whatsapp.whatsapp_operator_service.send_interactive_button_message")
    @patch("apps.integrations.whatsapp.whatsapp_operator_service.fetch_whatsapp_media")
    def test_operator_image_collects_without_reservation_link(
        self, mock_fetch, mock_send, mock_schedule_quiet,
    ):
        mock_fetch.return_value = (b"fake-image-bytes", "image/jpeg")
        mock_send.return_value = {"messages": [{"id": "wamid.out.ack"}]}

        message = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            wamid="wamid.in.op.image",
            wa_id=self.operator_wa_id,
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="image",
            body="",
            raw_payload={
                "type": "image",
                "image": {"id": "media-op-1", "mime_type": "image/jpeg"},
            },
        )

        result = handle_operator_inbound(
            row=message,
            integration_row=self.integration,
            runtime=WhatsAppRuntimeConfig.from_integration_dict(self.integration.get_config_dict()),
            action_text="",
        )

        self.assertEqual(result["status"], "collected")
        message.refresh_from_db()
        self.assertIsNone(message.reservation_id)
        session = WhatsAppOperatorSession.objects.get(operator_wa_id=self.operator_wa_id)
        self.assertEqual(session.status, WhatsAppOperatorSessionStatus.COLLECTING)
        job = session.job
        self.assertEqual(job.source, DocumentIntakeJobSource.WHATSAPP_OPERATOR)
        self.assertIsNone(job.reservation_id)
        self.assertEqual(job.images.count(), 1)
        mock_send.assert_not_called()
        mock_schedule_quiet.assert_called_once()

    @patch("apps.integrations.whatsapp.whatsapp_operator_batch.schedule_operator_quiet_timer")
    @patch("apps.integrations.whatsapp.document_intake_finalize.finalize_document_intake_job")
    @patch("apps.integrations.whatsapp.whatsapp_operator_service.send_text_message")
    @patch("apps.integrations.whatsapp.whatsapp_operator_service.send_interactive_button_message")
    @patch("apps.integrations.whatsapp.whatsapp_operator_service.fetch_whatsapp_media")
    def test_operator_checkin_finalize(
        self,
        mock_fetch,
        mock_interactive,
        mock_send,
        mock_finalize_job,
        mock_schedule_quiet,
    ):
        mock_fetch.return_value = (b"fake-image-bytes", "image/jpeg")
        mock_interactive.side_effect = lambda **kwargs: self._next_interactive_wamid()
        mock_send.return_value = {"messages": [{"id": "wamid.out.success"}]}

        def _finalize_side_effect(ctx, **kwargs):
            job = ctx.job
            session = kwargs.get("session")
            job.reservation_id = self.target_reservation.pk
            job.save(update_fields=["reservation_id", "updated_at"])
            if session is not None:
                session.status = WhatsAppOperatorSessionStatus.DONE
                session.save(update_fields=["status", "updated_at"])
            return {
                "status": "completed",
                "job_id": job.pk,
                "reservation_id": self.target_reservation.pk,
                "guest_notify": {"channel": "whatsapp", "status": "sent"},
            }

        mock_finalize_job.side_effect = _finalize_side_effect

        self._send_operator_image("wamid.in.op.image.1")

        session = WhatsAppOperatorSession.objects.get(operator_wa_id=self.operator_wa_id)
        job = session.job

        confirm_result = self._send_operator_checkin("wamid.in.op.checkin")
        self.assertEqual(confirm_result["status"], "prompted")
        session.refresh_from_db()
        self.assertEqual(session.status, WhatsAppOperatorSessionStatus.AWAITING_CONFIRM)
        mock_finalize_job.assert_not_called()

        confirm_call = mock_interactive.call_args_list[-1]
        self.assertEqual(
            confirm_call.kwargs["buttons"],
            [(OPERATOR_DOCS_ALL_YES_ID, "Da"), (OPERATOR_DOCS_ALL_NO_ID, "Ne")],
        )

        result = self._send_operator_docs_yes("wamid.in.op.confirm.yes")
        self.assertEqual(result["status"], "completed")
        session.refresh_from_db()
        self.assertEqual(session.status, WhatsAppOperatorSessionStatus.DONE)
        mock_finalize_job.assert_called_once()
        job.refresh_from_db()
        self.assertEqual(job.reservation_id, self.target_reservation.pk)

    @patch("apps.integrations.whatsapp.whatsapp_operator_batch.schedule_operator_quiet_timer")
    @patch("apps.integrations.whatsapp.document_intake_finalize.finalize_document_intake_job")
    @patch("apps.integrations.whatsapp.whatsapp_operator_service.send_text_message")
    @patch("apps.integrations.whatsapp.whatsapp_operator_service.send_interactive_button_message")
    @patch("apps.integrations.whatsapp.whatsapp_operator_service.fetch_whatsapp_media")
    def test_operator_checkin_button_triggers_finalize(
        self,
        mock_fetch,
        mock_interactive,
        mock_send,
        mock_finalize_job,
        mock_schedule_quiet,
    ):
        mock_fetch.return_value = (b"fake-image-bytes", "image/jpeg")
        mock_interactive.side_effect = lambda **kwargs: self._next_interactive_wamid()
        mock_send.return_value = {"messages": [{"id": "wamid.out.success"}]}
        mock_finalize_job.return_value = {
            "status": "completed",
            "job_id": 1,
            "reservation_id": self.target_reservation.pk,
        }

        self._send_operator_image("wamid.in.op.image.btn.1", media_id="media-op-btn-1")

        session = WhatsAppOperatorSession.objects.get(operator_wa_id=self.operator_wa_id)
        job = session.job

        checkin_message = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            wamid="wamid.in.op.checkin.btn",
            wa_id=self.operator_wa_id,
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="interactive",
            body="Check-in",
            raw_payload={
                "type": "interactive",
                "interactive": {
                    "type": "button_reply",
                    "button_reply": {
                        "id": OPERATOR_CHECKIN_BUTTON_ID,
                        "title": OPERATOR_CHECKIN_BUTTON_TITLE,
                    },
                },
            },
        )
        handle_operator_inbound(
            row=checkin_message,
            integration_row=self.integration,
            runtime=self.runtime,
            action_text=OPERATOR_CHECKIN_BUTTON_TITLE,
            button_id=OPERATOR_CHECKIN_BUTTON_ID,
        )

        result = self._send_operator_docs_yes("wamid.in.op.confirm.btn.yes")
        self.assertEqual(result["status"], "completed")
        mock_finalize_job.assert_called_once()

    @patch("apps.integrations.whatsapp.whatsapp_operator_batch.schedule_operator_quiet_timer")
    @patch("apps.integrations.whatsapp.whatsapp_operator_service.send_text_message")
    @patch("apps.integrations.whatsapp.whatsapp_operator_service.send_interactive_button_message")
    @patch("apps.integrations.whatsapp.whatsapp_operator_service.fetch_whatsapp_media")
    def test_operator_docs_confirm_no_returns_to_collecting(
        self,
        mock_fetch,
        mock_interactive,
        mock_send,
        mock_schedule_quiet,
    ):
        mock_fetch.return_value = (b"fake-image-bytes", "image/jpeg")
        mock_interactive.side_effect = lambda **kwargs: self._next_interactive_wamid()
        mock_send.return_value = {"messages": [{"id": "wamid.out.text"}]}

        self._send_operator_image("wamid.in.op.image.no.1")
        self._send_operator_checkin("wamid.in.op.checkin.no")

        session = WhatsAppOperatorSession.objects.get(operator_wa_id=self.operator_wa_id)
        self.assertEqual(session.status, WhatsAppOperatorSessionStatus.AWAITING_CONFIRM)

        no_message = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            wamid="wamid.in.op.confirm.no",
            wa_id=self.operator_wa_id,
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="interactive",
            body="Ne",
            raw_payload={
                "type": "interactive",
                "interactive": {
                    "type": "button_reply",
                    "button_reply": {"id": OPERATOR_DOCS_ALL_NO_ID, "title": "Ne"},
                },
            },
        )
        result = handle_operator_inbound(
            row=no_message,
            integration_row=self.integration,
            runtime=self.runtime,
            action_text="Ne",
            button_id=OPERATOR_DOCS_ALL_NO_ID,
        )

        self.assertEqual(result["status"], "collecting")
        session.refresh_from_db()
        self.assertEqual(session.status, WhatsAppOperatorSessionStatus.COLLECTING)
        mock_send.assert_called()
        self.assertIn("Pošaljite još slike", mock_send.call_args.kwargs["body"])

    @patch("apps.integrations.whatsapp.whatsapp_operator_batch.schedule_operator_quiet_timer")
    @patch("apps.integrations.whatsapp.whatsapp_operator_service.send_text_message")
    @patch("apps.integrations.whatsapp.whatsapp_operator_service.send_interactive_button_message")
    @patch("apps.integrations.whatsapp.whatsapp_operator_service.fetch_whatsapp_media")
    def test_operator_image_during_awaiting_confirm_resets_collecting(
        self,
        mock_fetch,
        mock_interactive,
        mock_send,
        mock_schedule_quiet,
    ):
        mock_fetch.return_value = (b"fake-image-bytes", "image/jpeg")
        mock_interactive.side_effect = lambda **kwargs: self._next_interactive_wamid()

        self._send_operator_image("wamid.in.op.image.await.1")
        self._send_operator_checkin("wamid.in.op.checkin.await")

        session = WhatsAppOperatorSession.objects.get(operator_wa_id=self.operator_wa_id)
        self.assertEqual(session.status, WhatsAppOperatorSessionStatus.AWAITING_CONFIRM)

        self._send_operator_image("wamid.in.op.image.await.2", media_id="media-await-2")
        session.refresh_from_db()
        self.assertEqual(session.status, WhatsAppOperatorSessionStatus.COLLECTING)
        self.assertEqual(session.job.images.count(), 2)

    @patch("apps.integrations.whatsapp.whatsapp_operator_service.send_text_message")
    @patch("apps.integrations.whatsapp.whatsapp_operator_service.fetch_whatsapp_media")
    def test_operator_finalize_while_processing_sends_in_progress(
        self,
        mock_fetch,
        mock_send,
    ):
        mock_fetch.return_value = (b"fake-image-bytes", "image/jpeg")
        mock_send.return_value = {"messages": [{"id": "wamid.out.text"}]}

        job = DocumentIntakeJob.objects.create(
            tenant_id=self.tenant.pk,
            source=DocumentIntakeJobSource.WHATSAPP_OPERATOR,
            status=DocumentIntakeJobStatus.QUEUED,
            device_id="whatsapp_operator",
        )
        WhatsAppOperatorSession.objects.create(
            tenant_id=self.tenant.pk,
            operator_wa_id=self.operator_wa_id,
            job=job,
            status=WhatsAppOperatorSessionStatus.PROCESSING,
            last_activity_at=timezone.now(),
        )

        result = self._send_operator_docs_yes("wamid.in.op.duplicate.yes")
        self.assertEqual(result["status"], "blocked")
        self.assertIn("u tijeku", mock_send.call_args.kwargs["body"])

    @patch("apps.integrations.whatsapp.whatsapp_operator_batch.schedule_operator_quiet_timer")
    @patch("apps.integrations.whatsapp.document_intake_finalize.finalize_document_intake_job")
    @patch("apps.integrations.whatsapp.whatsapp_operator_service.send_text_message")
    @patch("apps.integrations.whatsapp.whatsapp_operator_service.send_interactive_button_message")
    @patch("apps.integrations.whatsapp.whatsapp_operator_service.fetch_whatsapp_media")
    def test_operator_incomplete_documents_skips_checked_in(
        self,
        mock_fetch,
        mock_interactive,
        mock_send,
        mock_finalize_job,
        mock_schedule_quiet,
    ):
        mock_fetch.return_value = (b"fake-image-bytes", "image/jpeg")
        mock_interactive.side_effect = lambda **kwargs: self._next_interactive_wamid()
        mock_send.return_value = {"messages": [{"id": "wamid.out.text"}]}

        def _incomplete(ctx, **kwargs):
            session = kwargs.get("session")
            if session is not None:
                session.status = WhatsAppOperatorSessionStatus.COLLECTING
                session.save(update_fields=["status", "updated_at"])
            return {"status": "incomplete", "job_id": ctx.job.pk}

        mock_finalize_job.side_effect = _incomplete

        self._send_operator_image("wamid.in.op.image.incomplete.1")
        session = WhatsAppOperatorSession.objects.get(operator_wa_id=self.operator_wa_id)

        self._send_operator_checkin("wamid.in.op.checkin.incomplete")
        result = self._send_operator_docs_yes("wamid.in.op.confirm.incomplete.yes")

        self.assertEqual(result["status"], "incomplete")
        mock_finalize_job.assert_called_once()
        session.refresh_from_db()
        self.assertEqual(session.status, WhatsAppOperatorSessionStatus.COLLECTING)

    @patch("apps.integrations.whatsapp.whatsapp_operator_service.send_interactive_button_message")
    @patch("apps.integrations.whatsapp.whatsapp_operator_service.fetch_whatsapp_media")
    @patch("apps.integrations.whatsapp.tasks.on_whatsapp_document_received.delay")
    @patch("apps.integrations.whatsapp.tasks.find_reservation_for_wa_id")
    def test_process_inbound_routes_operator_before_guest_flow(
        self,
        mock_find_reservation,
        mock_guest_document,
        mock_fetch,
        mock_send,
    ):
        mock_fetch.return_value = (b"fake-image-bytes", "image/jpeg")
        mock_send.return_value = {"messages": [{"id": "wamid.out.ack"}]}
        mock_find_reservation.return_value = self.other_reservation

        ante_wa_id = "385976713511"
        message = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            wamid="wamid.in.ante.image",
            wa_id=ante_wa_id,
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="image",
            body="",
            raw_payload={
                "type": "image",
                "image": {"id": "media-ante-1", "mime_type": "image/jpeg"},
            },
        )

        result = process_inbound_message(message.pk)

        self.assertEqual(result["status"], "collected")
        self.assertTrue(result.get("operator_flow"))
        mock_find_reservation.assert_not_called()
        mock_guest_document.assert_not_called()
        message.refresh_from_db()
        self.assertIsNone(message.reservation_id)

    @patch("apps.integrations.whatsapp.whatsapp_operator_batch.schedule_operator_quiet_timer")
    @patch("apps.integrations.whatsapp.whatsapp_operator_service.fetch_whatsapp_media")
    def test_consolidate_merges_parallel_sessions(self, mock_fetch, mock_schedule):
        mock_fetch.return_value = (b"fake-image-bytes", "image/jpeg")
        from apps.integrations.whatsapp.whatsapp_operator_service import consolidate_operator_collect_sessions

        job_a = DocumentIntakeJob.objects.create(
            tenant_id=self.tenant.pk,
            source=DocumentIntakeJobSource.WHATSAPP_OPERATOR,
            status=DocumentIntakeJobStatus.QUEUED,
            device_id="whatsapp_operator",
        )
        job_b = DocumentIntakeJob.objects.create(
            tenant_id=self.tenant.pk,
            source=DocumentIntakeJobSource.WHATSAPP_OPERATOR,
            status=DocumentIntakeJobStatus.QUEUED,
            device_id="whatsapp_operator",
        )
        session_a = WhatsAppOperatorSession.objects.create(
            tenant_id=self.tenant.pk,
            operator_wa_id=self.operator_wa_id,
            job=job_a,
            status=WhatsAppOperatorSessionStatus.COLLECTING,
        )
        WhatsAppOperatorSession.objects.create(
            tenant_id=self.tenant.pk,
            operator_wa_id=self.operator_wa_id,
            job=job_b,
            status=WhatsAppOperatorSessionStatus.COLLECTING,
        )
        from django.core.files.base import ContentFile
        from apps.reservations.models import DocumentIntakeImage

        DocumentIntakeImage.objects.create(
            tenant_id=self.tenant.pk,
            job=job_b,
            image=ContentFile(b"img-b", name="op_999.jpg"),
            sort_order=0,
        )

        consolidate_operator_collect_sessions(
            tenant_id=self.tenant.pk,
            operator_wa_id=self.operator_wa_id,
            canonical_session=session_a,
        )
        session_b = WhatsAppOperatorSession.objects.get(job=job_b)
        self.assertEqual(session_b.status, WhatsAppOperatorSessionStatus.FAILED)
        self.assertEqual(session_a.job.images.count(), 1)

        self._send_operator_image("wamid.in.op.merge.2", media_id="media-merge-2")
        session_a.refresh_from_db()
        self.assertEqual(session_a.job.images.count(), 2)

    @patch("apps.integrations.whatsapp.whatsapp_operator_service.send_interactive_button_message")
    @patch("apps.integrations.whatsapp.whatsapp_operator_batch.schedule_operator_quiet_timer")
    @patch("apps.integrations.whatsapp.whatsapp_operator_service.fetch_whatsapp_media")
    def test_operator_quiet_elapsed_sends_single_prompt(
        self, mock_fetch, mock_schedule, mock_send,
    ):
        mock_fetch.return_value = (b"fake-image-bytes", "image/jpeg")
        mock_send.return_value = {"messages": [{"id": "wamid.out.quiet"}]}

        self._send_operator_image("wamid.in.op.quiet.1")
        session = WhatsAppOperatorSession.objects.get(operator_wa_id=self.operator_wa_id)
        from apps.integrations.whatsapp.whatsapp_operator_batch import operator_collect_quiet_elapsed

        result = operator_collect_quiet_elapsed(session.pk)
        self.assertEqual(result["status"], "prompted")
        mock_send.assert_called_once()
        self.assertEqual(
            mock_send.call_args.kwargs["buttons"],
            [(OPERATOR_DOCS_ALL_YES_ID, "Da"), (OPERATOR_DOCS_ALL_NO_ID, "Ne")],
        )
        session.refresh_from_db()
        self.assertEqual(session.status, WhatsAppOperatorSessionStatus.AWAITING_CONFIRM)

    @patch("apps.integrations.whatsapp.whatsapp_operator_service._send_guest_operator_checkin_email")
    @patch("apps.integrations.whatsapp.evisitor_reply._send_reservation_whatsapp_text")
    def test_notify_guest_email_only_skips_whatsapp(self, mock_wa, mock_email):
        mock_email.return_value = {"sent": True, "to": "guest@example.com"}
        from apps.integrations.whatsapp.whatsapp_operator_service import notify_guest_operator_checkin_complete

        result = notify_guest_operator_checkin_complete(
            self.target_reservation,
            guest_notify_mode="email_only",
        )
        self.assertEqual(result["channel"], "email")
        mock_wa.assert_not_called()
        mock_email.assert_called_once()


class OperatorMatchCandidateFormatTests(TestCase):
    def test_format_nested_candidates_with_person_names(self):
        formatted = _format_match_candidates(
            [
                {
                    "person_name": "Francois HARTWEG",
                    "reservation_id": None,
                    "candidates": [
                        {
                            "reservation_id": 8,
                            "guest_id": 1014,
                            "guest_name": "François Hartweg",
                            "reservation_label": "#8 · François Hartweg · 2026-06-09",
                            "match_type": "name",
                        },
                        {
                            "reservation_id": 138,
                            "guest_id": 1951,
                            "guest_name": "Novi gost",
                            "reservation_label": "#138 · Jasmin Engeland · 2026-06-10",
                            "match_type": "unfilled_slot",
                        },
                    ],
                }
            ]
        )
        self.assertIn("#8", formatted)
        self.assertIn("Francois HARTWEG", formatted)
        self.assertIn("[name]", formatted)
        self.assertIn("#138", formatted)


class OperatorReservationPickFormatTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="pick-fmt", name="Pick")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Prop",
            slug="prop",
            address="Addr",
        )

    def test_format_reservation_pick_line(self):
        reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booker_name="Jasmin Engeland",
            booking_code="5358710801",
            check_in=date(2026, 6, 10),
            check_out=date(2026, 6, 11),
            nights_count=1,
            adults_count=2,
            status=Reservation.Status.EXPECTED,
        )
        Guest.objects.create(
            tenant=self.tenant,
            reservation=reservation,
            first_name="Jasmin",
            last_name="Engeland",
            name="Jasmin Engeland",
            is_primary=True,
        )
        Guest.objects.create(
            tenant=self.tenant,
            reservation=reservation,
            first_name=PLACEHOLDER_FIRST,
            last_name=PLACEHOLDER_LAST,
            name="Novi gost",
            is_primary=False,
        )
        line = format_reservation_pick_line(reservation)
        self.assertIn(f"#{reservation.pk}", line)
        self.assertIn("5358710801", line)
        self.assertIn("1 noć", line)
        self.assertIn("Jasmin Engeland", line)
        self.assertIn("prazni slot", line)


class OperatorReservationPickFlowTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="pick-flow", name="Pick Flow")
        TenantReceptionSettings.objects.create(
            tenant=self.tenant,
            whatsapp_operator_phones=[{"name": "Toni", "phone": "+385998388513"}],
        )
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Uzorita",
            slug="uzorita",
            address="Addr",
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
                "auto_reply": True,
            }
        )
        self.integration.save()
        self.operator_wa_id = "385998388513"
        self.runtime = WhatsAppRuntimeConfig.from_integration_dict(self.integration.get_config_dict())
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booker_name="Jasmin Engeland",
            booking_code="5358710801",
            check_in=timezone.now().date(),
            check_out=timezone.now().date() + timedelta(days=1),
            adults_count=2,
            persons_count=2,
            status=Reservation.Status.EXPECTED,
        )
        Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="Jasmin",
            last_name="Engeland",
            name="Jasmin Engeland",
            is_primary=True,
        )
        Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name=PLACEHOLDER_FIRST,
            last_name=PLACEHOLDER_LAST,
            name="Novi gost",
            is_primary=False,
        )

    def _pick_session_with_job(self) -> WhatsAppOperatorSession:
        job = DocumentIntakeJob.objects.create(
            tenant_id=self.tenant.pk,
            source=DocumentIntakeJobSource.WHATSAPP_OPERATOR,
            status=DocumentIntakeJobStatus.DONE,
            device_id="whatsapp_operator",
            ocr_result={
                "persons": [
                    {"given_names": "Jasmin", "surnames": "Hengeland"},
                    {"given_names": "Ingo", "surnames": "Hengeland"},
                ],
            },
            matches=[
                {
                    "person_index": 0,
                    "person_name": "Jasmin Hengeland",
                    "auto_apply": False,
                    "confidence": "low",
                    "candidates": [],
                },
                {
                    "person_index": 1,
                    "person_name": "Ingo Hengeland",
                    "auto_apply": False,
                    "confidence": "low",
                    "candidates": [],
                },
            ],
        )
        return WhatsAppOperatorSession.objects.create(
            tenant_id=self.tenant.pk,
            operator_wa_id=self.operator_wa_id,
            job=job,
            status=WhatsAppOperatorSessionStatus.AWAITING_RES_PICK,
            last_activity_at=timezone.now(),
        )

    @patch("apps.integrations.whatsapp.document_intake_finalize.finalize_document_intake_job")
    @patch("apps.integrations.whatsapp.whatsapp_operator_service.send_text_message")
    def test_reservation_pick_reply_applies(
        self,
        mock_send,
        mock_finalize_job,
    ):
        mock_send.return_value = {"messages": [{"id": "wamid.out.pick"}]}
        mock_finalize_job.return_value = {"status": "completed"}

        session = self._pick_session_with_job()
        message = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            wamid="wamid.in.pick.138",
            wa_id=self.operator_wa_id,
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="text",
            body=str(self.reservation.pk),
            raw_payload={"type": "text", "text": {"body": str(self.reservation.pk)}},
        )
        result = handle_operator_inbound(
            row=message,
            integration_row=self.integration,
            runtime=self.runtime,
            action_text=str(self.reservation.pk),
        )

        self.assertEqual(result["status"], "completed")
        mock_finalize_job.assert_called_once()

    @patch("apps.integrations.whatsapp.whatsapp_operator_service.send_text_message")
    def test_reservation_pick_invalid_code(self, mock_send):
        mock_send.return_value = {"messages": [{"id": "wamid.out.invalid"}]}
        self._pick_session_with_job()
        message = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            wamid="wamid.in.pick.invalid",
            wa_id=self.operator_wa_id,
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="text",
            body="bok",
            raw_payload={"type": "text", "text": {"body": "bok"}},
        )
        result = handle_operator_inbound(
            row=message,
            integration_row=self.integration,
            runtime=self.runtime,
            action_text="bok",
        )
        self.assertEqual(result["status"], "reservation_not_found")
        self.assertIn("Nisam pronašao", mock_send.call_args.kwargs["body"])

    def _send_checkin(self, wamid: str) -> dict:
        message = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            wamid=wamid,
            wa_id=self.operator_wa_id,
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="text",
            body="check-in",
            raw_payload={"type": "text", "text": {"body": "check-in"}},
        )
        return handle_operator_inbound(
            row=message,
            integration_row=self.integration,
            runtime=self.runtime,
            action_text="check-in",
        )

    def _send_docs_yes(self, wamid: str) -> dict:
        message = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            wamid=wamid,
            wa_id=self.operator_wa_id,
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="interactive",
            body="Da",
            raw_payload={
                "type": "interactive",
                "interactive": {
                    "type": "button_reply",
                    "button_reply": {"id": OPERATOR_DOCS_ALL_YES_ID, "title": "Da"},
                },
            },
        )
        return handle_operator_inbound(
            row=message,
            integration_row=self.integration,
            runtime=self.runtime,
            action_text="Da",
            button_id=OPERATOR_DOCS_ALL_YES_ID,
        )

    @patch("apps.integrations.whatsapp.document_intake_finalize.process_document_intake_job")
    @patch("apps.integrations.whatsapp.whatsapp_operator_service.send_text_message")
    @patch("apps.integrations.whatsapp.whatsapp_operator_service.send_interactive_button_message")
    @patch("apps.integrations.whatsapp.whatsapp_operator_service.fetch_whatsapp_media")
    def test_finalize_no_match_enters_awaiting_res_pick(
        self,
        mock_fetch,
        mock_interactive,
        mock_send,
        mock_process,
    ):
        mock_fetch.return_value = (b"fake-image-bytes", "image/jpeg")
        mock_interactive.side_effect = lambda **kwargs: {"messages": [{"id": "wamid.out.pick.flow.interactive"}]}
        send_seq = {"n": 0}

        def _unique_text_send(**kwargs):
            send_seq["n"] += 1
            return {"messages": [{"id": f"wamid.out.pick.flow.text.{send_seq['n']}"}]}

        mock_send.side_effect = _unique_text_send

        image_message = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            wamid="wamid.in.pick.flow.1",
            wa_id=self.operator_wa_id,
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="image",
            body="",
            raw_payload={
                "type": "image",
                "image": {"id": "media-pick-1", "mime_type": "image/jpeg"},
            },
        )
        handle_operator_inbound(
            row=image_message,
            integration_row=self.integration,
            runtime=self.runtime,
            action_text="",
        )
        session = WhatsAppOperatorSession.objects.get(operator_wa_id=self.operator_wa_id)
        job = session.job

        def _set_no_match(ctx):
            DocumentIntakeJob.objects.filter(pk=ctx.job.pk).update(
                status=DocumentIntakeJobStatus.DONE,
                ocr_result={
                    "persons": [
                        {"given_names": "Unknown", "surnames": "PERSON"},
                    ],
                },
                matches=[
                    {
                        "person_index": 0,
                        "person_name": "Unknown PERSON",
                        "auto_apply": False,
                        "confidence": "low",
                        "candidates": [
                            {
                                "reservation_id": self.reservation.pk,
                                "guest_id": self.reservation.guests.first().pk,
                                "guest_name": "Jasmin Engeland",
                                "reservation_label": f"#{self.reservation.pk}",
                                "match_type": "unfilled_slot",
                            }
                        ],
                    }
                ],
            )

        mock_process.side_effect = _set_no_match

        self._send_checkin("wamid.in.pick.flow.checkin")
        result = self._send_docs_yes("wamid.in.pick.flow.yes")

        self.assertEqual(result["status"], "awaiting_reservation_pick")
        session.refresh_from_db()
        self.assertEqual(session.status, WhatsAppOperatorSessionStatus.AWAITING_RES_PICK)


@override_settings(
    STAY_INTEGRATION_FERNET_KEY=TEST_FERNET_KEY,
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class OperatorDocsConfirmPriorityTests(TestCase):
    """Docs confirm Da/Ne must win over stale arrival-time sessions."""

    def setUp(self):
        self.tenant = Tenant.objects.create(slug="docs-priority", name="Docs Priority")
        TenantReceptionSettings.objects.create(
            tenant=self.tenant,
            whatsapp_operator_phones=[{"name": "Toni", "phone": "+385998388513"}],
        )
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Uzorita",
            slug="uzorita",
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
        self.operator_wa_id = "385998388513"
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booker_name="Test Guest",
            check_in=date(2026, 6, 14),
            check_out=date(2026, 6, 16),
            status=Reservation.Status.EXPECTED,
        )
        WhatsAppArrivalConfirmSession.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            status=WhatsAppArrivalConfirmSessionStatus.AWAITING_TIME,
            responded_operator_wa_id=self.operator_wa_id,
            trigger=WhatsAppArrivalConfirmTrigger.NIGHTLY_23H,
        )
        job = DocumentIntakeJob.objects.create(
            tenant_id=self.tenant.pk,
            reservation=None,
            source=DocumentIntakeJobSource.WHATSAPP_OPERATOR,
            status=DocumentIntakeJobStatus.QUEUED,
            device_id="whatsapp",
        )
        DocumentIntakeImage.objects.create(
            tenant_id=self.tenant.pk,
            job=job,
            image=ContentFile(b"fake", name="op_test.jpg"),
            sort_order=0,
        )
        self.session = WhatsAppOperatorSession.objects.create(
            tenant_id=self.tenant.pk,
            operator_wa_id=self.operator_wa_id,
            job=job,
            status=WhatsAppOperatorSessionStatus.AWAITING_CONFIRM,
        )

    @patch("apps.integrations.whatsapp.document_intake_finalize.finalize_document_intake_job")
    @patch("apps.integrations.whatsapp.whatsapp_operator_service.send_text_message")
    def test_docs_yes_finalizes_instead_of_time_parse(self, mock_send, mock_finalize):
        mock_finalize.return_value = {"status": "ambiguous_reservation", "job_id": self.session.job_id}
        mock_send.return_value = {"messages": [{"id": "wamid.out.docs.yes.priority"}]}

        message = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            wamid="wamid.in.docs.yes.priority",
            wa_id=self.operator_wa_id,
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="interactive",
            body="Da",
            raw_payload={
                "type": "interactive",
                "interactive": {
                    "type": "button_reply",
                    "button_reply": {"id": OPERATOR_DOCS_ALL_YES_ID, "title": "Da"},
                },
            },
        )
        result = handle_operator_inbound(
            row=message,
            integration_row=self.integration,
            runtime=self.runtime,
            action_text="Da",
            button_id=OPERATOR_DOCS_ALL_YES_ID,
        )

        mock_finalize.assert_called_once()
        for call in mock_send.call_args_list:
            self.assertNotIn("Nisam razumio vrijeme", call.kwargs.get("body", ""))
        self.assertNotEqual(result.get("status"), "time_parse_failed")


@override_settings(
    STAY_INTEGRATION_FERNET_KEY=TEST_FERNET_KEY,
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class OperatorDuplicatePromptTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="dup-prompt", name="Dup Prompt")
        TenantReceptionSettings.objects.create(
            tenant=self.tenant,
            whatsapp_operator_phones=[{"name": "Toni", "phone": "+385998388513"}],
        )
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
            }
        )
        self.integration.save()
        self.operator_wa_id = "385998388513"
        job = DocumentIntakeJob.objects.create(
            tenant_id=self.tenant.pk,
            source=DocumentIntakeJobSource.WHATSAPP_OPERATOR,
            status=DocumentIntakeJobStatus.QUEUED,
            device_id="whatsapp",
        )
        DocumentIntakeImage.objects.create(
            tenant_id=self.tenant.pk,
            job=job,
            image=ContentFile(b"fake", name="op_dup.jpg"),
            sort_order=0,
        )
        self.session = WhatsAppOperatorSession.objects.create(
            tenant_id=self.tenant.pk,
            operator_wa_id=self.operator_wa_id,
            job=job,
            status=WhatsAppOperatorSessionStatus.COLLECTING,
        )

    @patch("apps.integrations.whatsapp.whatsapp_operator_batch._send_operator_docs_confirm_prompt")
    def test_duplicate_quiet_prompt_skipped(self, mock_send):
        mock_send.return_value = {"status": "sent", "outbound_wamid": "wamid.out.1"}

        first = send_operator_collect_prompt_for_session(self.session.pk)
        second = send_operator_collect_prompt_for_session(self.session.pk)

        self.assertEqual(first["status"], "prompted")
        self.assertEqual(second["status"], "skipped")
        self.assertEqual(second["reason"], "already_awaiting_confirm")
        mock_send.assert_called_once()
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, WhatsAppOperatorSessionStatus.AWAITING_CONFIRM)
