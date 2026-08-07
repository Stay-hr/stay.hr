from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.integrations.tests.test_whatsapp_webhook import TEST_FERNET_KEY
from apps.integrations.whatsapp.operator_job_complete import complete_operator_document_job
from apps.properties.models import Property, Unit
from apps.reservations.guest_slots import PLACEHOLDER_FIRST, PLACEHOLDER_LAST
from apps.reservations.models import (
    DocumentIntakeImage,
    DocumentIntakeJob,
    DocumentIntakeJobSource,
    DocumentIntakeJobStatus,
    Guest,
    Reservation,
    ReservationUnit,
    WhatsAppOperatorSession,
    WhatsAppOperatorSessionStatus,
)
from apps.tenants.models import Tenant

TEST_WHATSAPP_ACCESS_TOKEN = "test-whatsapp-access-token"


@override_settings(
    STAY_INTEGRATION_FERNET_KEY=TEST_FERNET_KEY,
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class OperatorJobCompleteTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="uzorita", name="Uzorita", default_language="hr")
        self.property = Property.objects.create(tenant=self.tenant, name="Uzorita B&B", slug="uzorita")
        self.unit = Unit.objects.create(
            tenant=self.tenant,
            property=self.property,
            code="R1",
            name="Room 1",
        )
        today = timezone.localdate()
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booker_name="François Hartweg",
            booker_phone="+33674174251",
            booker_email="guest@example.com",
            adults_count=2,
            check_in=today,
            check_out=today + timedelta(days=1),
            status=Reservation.Status.EXPECTED,
            booking_code="5193574002",
        )
        self.primary = Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="François",
            last_name="Hartweg",
            name="François Hartweg",
            is_primary=True,
        )
        self.companion = Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name=PLACEHOLDER_FIRST,
            last_name=PLACEHOLDER_LAST,
            name="Novi gost",
            is_primary=False,
        )
        ReservationUnit.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            unit=self.unit,
            room_name="Room 1",
            sort_order=0,
        )
        self.job = DocumentIntakeJob.objects.create(
            tenant=self.tenant,
            source=DocumentIntakeJobSource.WHATSAPP_OPERATOR,
            status=DocumentIntakeJobStatus.DONE,
            device_id="whatsapp_operator",
            ocr_result={
                "persons": [
                    {"given_names": "Francois, Jacques, Alfred", "surnames": "HARTWEG"},
                    {"given_names": "Anne, Elisabeth OECHEL ep. HARTWEG", "surnames": ""},
                ],
            },
            matches=[],
        )
        WhatsAppOperatorSession.objects.create(
            tenant=self.tenant,
            operator_wa_id="385998388513",
            job=self.job,
            status=WhatsAppOperatorSessionStatus.FAILED,
        )
        tiny = SimpleUploadedFile(
            "op.jpg",
            b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb\x00C\x00"
            b"\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f"
            b"\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.\x27 ,#\x1c\x1c(7),01441\x1f\x27=9=82<.7\xff\xd9",
            content_type="image/jpeg",
        )
        for order in range(2):
            DocumentIntakeImage.objects.create(
                tenant=self.tenant,
                job=self.job,
                image=tiny,
                sort_order=order,
            )

    def test_dry_run_builds_selections_for_hartweg(self):
        result = complete_operator_document_job(
            self.job.pk,
            reservation_id=self.reservation.pk,
            dry_run=True,
        )
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["reservation_id"], self.reservation.pk)
        self.assertEqual(len(result["selections"]), 2)
        guest_ids = {s["guest_id"] for s in result["selections"]}
        self.assertEqual(guest_ids, {self.primary.pk, self.companion.pk})

    @patch("apps.integrations.whatsapp.operator_job_complete.run_document_intake_matching_pipeline")
    def test_operator_rematch_uses_matching_pipeline(self, mock_pipeline):
        mock_pipeline.return_value = [
            {
                "person_index": 0,
                "auto_apply": True,
                "guest_id": self.primary.pk,
                "reservation_id": self.reservation.pk,
            },
            {
                "person_index": 1,
                "auto_apply": True,
                "guest_id": self.companion.pk,
                "reservation_id": self.reservation.pk,
            },
        ]
        complete_operator_document_job(
            self.job.pk,
            reservation_id=self.reservation.pk,
            dry_run=True,
        )
        self.assertGreaterEqual(mock_pipeline.call_count, 1)
        reservation_calls = [
            call for call in mock_pipeline.call_args_list if call.kwargs.get("reservation") is not None
        ]
        self.assertGreaterEqual(len(reservation_calls), 1)
        scoped = reservation_calls[-1].kwargs
        self.assertEqual(scoped["tenant_id"], self.tenant.pk)
        self.assertEqual(scoped["reservation"].pk, self.reservation.pk)

    @patch.dict("os.environ", {"WHATSAPP_ACCESS_TOKEN": TEST_WHATSAPP_ACCESS_TOKEN})
    @patch("apps.integrations.whatsapp.operator_job_complete._notify_operator")
    @patch("apps.integrations.whatsapp.operator_job_complete.notify_guest_operator_checkin_complete")
    @patch("apps.integrations.whatsapp.operator_job_complete.submit_evisitor_for_reservation")
    @patch("apps.integrations.whatsapp.operator_job_complete.apply_document_intake_job")
    def test_complete_applies_checkin_and_notifies(
        self,
        mock_apply,
        mock_evisitor,
        mock_guest_notify,
        mock_operator_notify,
    ):
        mock_apply.return_value = [
            {"guest_id": self.primary.pk, "guest_name": "François Hartweg"},
            {"guest_id": self.companion.pk, "guest_name": "Anne Hartweg"},
        ]
        mock_evisitor.return_value = [
            {
                "guest_id": self.primary.pk,
                "guest_name": "François Hartweg",
                "status": "sent",
                "registration_id": "abc-123",
            },
            {
                "guest_id": self.companion.pk,
                "guest_name": "Anne Hartweg",
                "status": "sent",
                "registration_id": "def-456",
            },
        ]
        mock_guest_notify.return_value = {"channel": "whatsapp", "status": "sent"}
        mock_operator_notify.return_value = {"status": "sent"}

        result = complete_operator_document_job(
            self.job.pk,
            reservation_id=self.reservation.pk,
        )

        self.assertEqual(result["status"], "completed")
        mock_apply.assert_called_once()
        mock_evisitor.assert_called_once()
        mock_guest_notify.assert_called_once()
        mock_operator_notify.assert_called_once()

        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.status, Reservation.Status.CHECKED_IN)

        session = WhatsAppOperatorSession.objects.get(job=self.job)
        self.assertEqual(session.status, WhatsAppOperatorSessionStatus.DONE)

    @patch(
        "apps.integrations.whatsapp.guest_welcome_sequence.send_guest_welcome_entrance_and_ask_arrival"
    )
    @patch("apps.integrations.whatsapp.whatsapp_operator_service._send_guest_operator_checkin_email")
    def test_notify_guest_whatsapp_first_email_fallback(self, mock_email, mock_welcome):
        from apps.integrations.whatsapp.whatsapp_operator_service import (
            notify_guest_operator_checkin_complete,
        )

        mock_welcome.return_value = {"status": "skipped", "reason": "no_wa_id"}
        mock_email.return_value = {"sent": True, "to": "guest@example.com"}

        result = notify_guest_operator_checkin_complete(self.reservation)

        self.assertEqual(result["channel"], "email")
        self.assertTrue(result["sent"])
        mock_welcome.assert_called_once()
        mock_email.assert_called_once()

    @patch(
        "apps.integrations.whatsapp.guest_welcome_sequence.send_guest_welcome_entrance_and_ask_arrival"
    )
    @patch("apps.integrations.whatsapp.whatsapp_operator_service._send_guest_operator_checkin_email")
    def test_notify_guest_whatsapp_only_when_available(self, mock_email, mock_welcome):
        from apps.integrations.whatsapp.whatsapp_operator_service import (
            notify_guest_operator_checkin_complete,
        )

        mock_welcome.return_value = {"status": "sent", "wamid": "wamid.test"}
        result = notify_guest_operator_checkin_complete(self.reservation)

        self.assertEqual(result["channel"], "whatsapp")
        mock_welcome.assert_called_once()
        mock_email.assert_not_called()

    def test_management_command_dry_run(self):
        out = StringIO()
        call_command(
            "complete_operator_document_job",
            "--job-id",
            str(self.job.pk),
            "--reservation-id",
            str(self.reservation.pk),
            "--dry-run",
            stdout=out,
        )
        self.assertIn("DRY RUN", out.getvalue())
