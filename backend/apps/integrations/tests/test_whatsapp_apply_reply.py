from datetime import date
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from apps.communications.guest_compose import (
    render_ask_arrival_time_message,
    render_checkin_ready_message,
    render_docs_awaiting_arrival_message,
    render_operator_checkin_complete_message,
)
from apps.integrations.models import IntegrationConfig, WhatsAppMessage
from apps.integrations.whatsapp.apply_reply import maybe_send_document_apply_whatsapp_reply
from apps.reservations.document_intake_context import DocumentIntakeContext
from apps.integrations.tests.test_whatsapp_webhook import TEST_FERNET_KEY
from apps.properties.models import Property
from apps.reservations.models import (
    DocumentIntakeJob,
    DocumentIntakeJobSource,
    DocumentIntakeJobStatus,
    Guest,
    IdDocument,
    Reservation,
)
from apps.tenants.models import Tenant

TEST_WHATSAPP_ACCESS_TOKEN = "test-whatsapp-access-token"


@override_settings(STAY_INTEGRATION_FERNET_KEY=TEST_FERNET_KEY)
class WhatsAppApplyReplyTests(TestCase):
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
        self.inbound = WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            reservation=self.reservation,
            wamid="wamid.inbound.doc",
            wa_id="385911111111",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="image",
            body="",
            raw_payload={"type": "image", "image": {"id": "media-1"}},
        )
        self.job = DocumentIntakeJob.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            whatsapp_message=self.inbound,
            source=DocumentIntakeJobSource.WHATSAPP,
            status=DocumentIntakeJobStatus.APPLIED,
        )
        Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="Ana",
            last_name="Anić",
            name="Ana Anić",
            is_primary=True,
            document_number="P1234567",
            date_of_birth=date(1990, 5, 5),
        )

    @patch.dict("os.environ", {"WHATSAPP_DOCUMENT_APPLY_REPLY": "true", "WHATSAPP_ACCESS_TOKEN": TEST_WHATSAPP_ACCESS_TOKEN})
    @patch("apps.integrations.whatsapp.apply_reply.send_text_message")
    def test_missing_id_side_message_before_checkin_ready(self, mock_send):
        mock_send.return_value = {"messages": [{"id": "wamid.outbound.sides"}]}
        guest = Guest.objects.get(reservation=self.reservation)
        guest.document_type = "Osobna iskaznica"
        guest.save(update_fields=["document_type"])
        doc = IdDocument.objects.create(guest=guest)
        doc.front_photo.save(
            "front.jpg",
            SimpleUploadedFile("front.jpg", b"front"),
            save=True,
        )

        result = maybe_send_document_apply_whatsapp_reply(
            DocumentIntakeContext.from_job(self.job),
            applied=[{"guest_id": guest.pk, "reservation_id": self.reservation.pk}],
        )

        self.assertEqual(result["status"], "sent")
        body = mock_send.call_args.kwargs["body"]
        self.assertIn("Ana Anić", body)
        self.assertIn("stražnja strana osobne iskaznice", body)
        self.assertNotIn("check-in će proći brzo", body)
        self.job.refresh_from_db()
        self.assertFalse(self.job.whatsapp_reply_sent)

    @patch.dict("os.environ", {"WHATSAPP_DOCUMENT_APPLY_REPLY": "true", "WHATSAPP_ACCESS_TOKEN": TEST_WHATSAPP_ACCESS_TOKEN})
    @patch("apps.integrations.whatsapp.apply_reply.send_text_message")
    def test_checkin_ready_when_both_sides_present(self, mock_send):
        mock_send.return_value = {"messages": [{"id": "wamid.outbound.ready"}]}
        guest = Guest.objects.get(reservation=self.reservation)
        guest.document_type = "Osobna iskaznica"
        guest.save(update_fields=["document_type"])
        doc = IdDocument.objects.create(guest=guest)
        doc.front_photo.save(
            "front.jpg",
            SimpleUploadedFile("front.jpg", b"front"),
            save=True,
        )
        doc.back_photo.save(
            "back.jpg",
            SimpleUploadedFile("back.jpg", b"back"),
            save=True,
        )

        result = maybe_send_document_apply_whatsapp_reply(
            DocumentIntakeContext.from_job(self.job),
            applied=[{"guest_id": guest.pk, "reservation_id": self.reservation.pk}],
        )

        self.assertEqual(result["status"], "sent")
        body = mock_send.call_args.kwargs["body"]
        self.assertIn("Hvala vam na poslanim dokumentima", body)
        self.job.refresh_from_db()
        self.assertTrue(self.job.whatsapp_reply_sent)

    @patch.dict("os.environ", {"WHATSAPP_DOCUMENT_APPLY_REPLY": "true", "WHATSAPP_ACCESS_TOKEN": TEST_WHATSAPP_ACCESS_TOKEN})
    @patch("apps.integrations.whatsapp.apply_reply.send_text_message")
    def test_sends_checkin_ready_after_apply(self, mock_send):
        mock_send.return_value = {"messages": [{"id": "wamid.outbound.ready"}]}
        applied = [{"guest_id": 1, "reservation_id": self.reservation.pk}]

        result = maybe_send_document_apply_whatsapp_reply(
            DocumentIntakeContext.from_job(self.job), applied=applied)

        self.assertEqual(result["status"], "sent")
        mock_send.assert_called_once()
        body = mock_send.call_args.kwargs["body"]
        self.assertIn("Hvala vam na poslanim dokumentima", body)
        self.job.refresh_from_db()
        self.assertTrue(self.job.whatsapp_reply_sent)
        self.assertTrue(
            WhatsAppMessage.objects.filter(
                direction=WhatsAppMessage.Direction.OUTBOUND,
                reservation=self.reservation,
            ).exists()
        )

    @patch.dict("os.environ", {"WHATSAPP_DOCUMENT_APPLY_REPLY": "true"})
    def test_skips_duplicate_reply(self):
        self.job.whatsapp_reply_sent = True
        self.job.save(update_fields=["whatsapp_reply_sent"])
        result = maybe_send_document_apply_whatsapp_reply(
            DocumentIntakeContext.from_job(self.job),
            applied=[{"guest_id": 1, "reservation_id": self.reservation.pk}],
        )
        self.assertEqual(result["status"], "already_sent")

    @patch.dict("os.environ", {"WHATSAPP_DOCUMENT_APPLY_REPLY": "true", "WHATSAPP_ACCESS_TOKEN": TEST_WHATSAPP_ACCESS_TOKEN})
    @patch("apps.integrations.whatsapp.apply_reply.send_text_message")
    def test_skips_when_sibling_job_already_sent_reply(self, mock_send):
        sibling = DocumentIntakeJob.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            source=DocumentIntakeJobSource.WHATSAPP,
            status=DocumentIntakeJobStatus.APPLIED,
            whatsapp_reply_sent=True,
        )
        self.assertIsNotNone(sibling.pk)
        result = maybe_send_document_apply_whatsapp_reply(
            DocumentIntakeContext.from_job(self.job),
            applied=[{"guest_id": 1, "reservation_id": self.reservation.pk}],
        )
        self.assertEqual(result["status"], "already_sent")
        mock_send.assert_not_called()

    def test_render_checkin_ready_hr(self):
        text = render_checkin_ready_message(self.reservation)
        self.assertIn("Hvala vam na poslanim dokumentima", text)

    def test_render_operator_checkin_complete_hr(self):
        text = render_operator_checkin_complete_message(self.reservation)
        self.assertIn("Check-in je obavljen", text)
        self.assertIn("ugodan boravak", text)
        self.assertIn("Check-in:", text)
        self.assertIn("Restaurant Uzorita", text)
        self.assertIn("Parkiranje", text)
        self.assertNotIn("dolazak", text.lower())

    def test_render_operator_checkin_complete_de(self):
        reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booker_name="Robert Siebinger",
            booker_phone="+4369912014899",
            booker_email="guest@example.com",
            booker_country="DE",
            check_in=date(2026, 7, 1),
            check_out=date(2026, 7, 3),
            status=Reservation.Status.EXPECTED,
        )
        text = render_operator_checkin_complete_message(reservation)
        self.assertIn("Check-in ist abgeschlossen", text)
        self.assertIn("angenehmen Aufenthalt", text)
        self.assertIn("Check-in:", text)
        self.assertIn("Restaurant Uzorita", text)
        self.assertIn("nächsten Nachricht", text)
        self.assertIn("Parken", text)
        self.assertNotIn("Ankunft", text)
        ask = render_ask_arrival_time_message(reservation)
        self.assertIn("Ankunft", ask)

    def test_render_docs_awaiting_arrival_hr_without_ask(self):
        text = render_docs_awaiting_arrival_message(self.reservation)
        self.assertIn("dokument", text.lower())
        self.assertNotIn("dolazak", text.lower())
        ask = render_ask_arrival_time_message(self.reservation)
        self.assertIn("dolazak", ask.lower())

    @patch.dict("os.environ", {"WHATSAPP_DOCUMENT_APPLY_REPLY": "true", "WHATSAPP_ACCESS_TOKEN": TEST_WHATSAPP_ACCESS_TOKEN})
    @patch("apps.integrations.whatsapp.apply_reply.send_text_message")
    def test_partial_apply_sends_remaining_guest_message(self, mock_send):
        mock_send.return_value = {"messages": [{"id": "wamid.outbound.partial"}]}
        Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="Novi",
            last_name="gost",
            name="Novi gost",
            is_primary=False,
        )
        self.reservation.adults_count = 2
        self.reservation.save(update_fields=["adults_count"])

        result = maybe_send_document_apply_whatsapp_reply(
            DocumentIntakeContext.from_job(self.job),
            applied=[{"guest_id": 1, "reservation_id": self.reservation.pk}],
        )

        self.assertEqual(result["status"], "sent")
        body = mock_send.call_args.kwargs["body"]
        self.assertIn("preostale odrasle goste", body)
        self.job.refresh_from_db()
        self.assertFalse(self.job.whatsapp_reply_sent)

    @patch.dict("os.environ", {"WHATSAPP_DOCUMENT_APPLY_REPLY": "true", "WHATSAPP_ACCESS_TOKEN": TEST_WHATSAPP_ACCESS_TOKEN})
    @patch("apps.integrations.whatsapp.apply_reply.send_text_message")
    def test_failure_message_includes_arrival_question(self, mock_send):
        from apps.integrations.whatsapp.apply_reply import (
            maybe_send_checkin_automation_failed_whatsapp_reply,
        )

        mock_send.return_value = {"messages": [{"id": "wamid.outbound.fail"}]}
        result = maybe_send_checkin_automation_failed_whatsapp_reply(self.job)
        self.assertEqual(result["status"], "sent")
        body = mock_send.call_args.kwargs["body"]
        self.assertIn("Automatski check-in nije uspio", body)
        self.assertIn("vrijeme dolaska", body)

    def test_is_guest_checkin_acknowledged_requires_checked_in_or_reception_draft(self):
        from apps.integrations.whatsapp.apply_reply import is_guest_checkin_acknowledged

        with patch(
            "apps.integrations.whatsapp.apply_reply.is_document_checkin_complete",
            return_value=True,
        ):
            self.assertFalse(is_guest_checkin_acknowledged(self.reservation))

        self.reservation.status = Reservation.Status.CHECKED_IN
        self.reservation.save(update_fields=["status", "updated_at"])
        self.assertTrue(is_guest_checkin_acknowledged(self.reservation))
