from datetime import timedelta
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIClient

from apps.properties.models import Property, Unit
from apps.reservations.guest_slots import PLACEHOLDER_FIRST, PLACEHOLDER_LAST
from apps.reservations.models import DocumentIntakeJob, DocumentIntakeJobSource, DocumentIntakeJobStatus, Guest, Reservation, ReservationUnit
from apps.tenants.models import RECEPTION_DEVICE_SCOPES, ApiApplication, Tenant


def _tiny_jpeg(name: str = "doc.jpg") -> SimpleUploadedFile:
    buf = BytesIO()
    Image.new("RGB", (40, 40), color=(200, 180, 160)).save(buf, format="JPEG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/jpeg")


@override_settings(
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
    MEDIA_ROOT="/tmp/stay_test_media",
)
class DocumentIntakeAPITests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Uzorita", slug="uzorita")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Luxury Room Uzorita",
            slug="uzorita",
            address="Test",
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
        # Match window is today-3 .. today+14 (Zagreb); keep stay relative to "now".
        today = timezone.localdate()
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            external_id="5036489024",
            booking_code="5036489024",
            check_in=today,
            check_out=today + timedelta(days=2),
            status=Reservation.Status.EXPECTED,
            booker_name="Hans Fischer",
            booker_email="hans@example.com",
            booker_phone="+49 170 1234567",
            amount=Decimal("180.15"),
            adults_count=2,
        )
        ReservationUnit.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            unit=self.unit,
        )
        self.primary = Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="Hans",
            last_name="Fischer",
            name="Hans Fischer",
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
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.raw_token}")
        self.base = "/api/v1/reception/document-intake"

    @patch("apps.reservations.document_intake_service.ocr_configured", return_value=True)
    @patch("apps.reservations.document_intake_service.run_document_batch_ocr")
    def test_batch_process_apply_updates_guest(self, mock_ocr, _mock_ocr_configured):
        mock_ocr.return_value = {
            "images": [
                {
                    "index": 0,
                    "side": "front",
                    "mrz_lines": [],
                    "ocr_text": "REPUBLIKA HRVATSKA\nHans Fischer",
                }
            ],
            "persons": [
                {
                    "given_names": "Hans",
                    "surnames": "Fischer",
                    "document_number": "L01X00T47",
                    "nationality": "DEU",
                    "date_of_birth": "1980-05-01",
                    "date_of_expiry": "2030-05-01",
                    "sex": "M",
                    "address": "Markdorf, Teststr. 1",
                    "document_type": "national_id",
                    "front_image_index": 0,
                    "back_image_index": None,
                    # Omit inconsistent MRZ: identity gate rejects document_number_mismatch.
                    "mrz_lines": [],
                    "face_bbox": {"x": 0.05, "y": 0.12, "w": 0.35, "h": 0.45},
                }
            ],
        }

        batch = self.client.post(
            f"{self.base}/batch/",
            {"files": [_tiny_jpeg()]},
            format="multipart",
        )
        self.assertEqual(batch.status_code, 201)
        job_id = batch.json()["job_id"]

        process = self.client.post(f"{self.base}/jobs/{job_id}/process/")
        self.assertEqual(process.status_code, 200)
        body = process.json()
        self.assertEqual(body["status"], DocumentIntakeJobStatus.DONE)
        self.assertEqual(len(body["matches"]), 1)
        self.assertTrue(body["matches"][0]["auto_apply"])
        self.assertIn("ocr_summary", body)
        self.assertIn("Hans", body["ocr_summary"])

        apply = self.client.post(f"{self.base}/jobs/{job_id}/apply/", {}, format="json")
        self.assertEqual(apply.status_code, 200)
        self.assertEqual(apply.json()["status"], DocumentIntakeJobStatus.APPLIED)
        self.assertEqual(len(apply.json()["applied"]), 1)

        self.primary.refresh_from_db()
        self.assertEqual(self.primary.document_number, "L01X00T47")
        self.assertEqual(self.primary.first_name, "Hans")
        self.assertEqual(self.primary.nationality, "DE")
        self.assertEqual(self.primary.document_country_iso2, "DE")

    @patch("apps.reservations.document_intake_service.ocr_configured", return_value=True)
    @patch("apps.reservations.document_intake_service.run_document_batch_ocr")
    def test_apply_polish_nationality_overrides_channel_booker_country(
        self, mock_ocr, _mock_ocr_configured
    ):
        """ISO3 POL must map to PL and override stale Channex booker_country (e.g. GB)."""
        self.reservation.booker_country = "GB"
        self.reservation.save(update_fields=["booker_country"])

        mock_ocr.return_value = {
            "images": [{"index": 0, "side": "front", "mrz_lines": [], "ocr_text": ""}],
            "persons": [
                {
                    "given_names": "Hans",
                    "surnames": "Fischer",
                    "document_number": "DKC942819",
                    "nationality": "POL",
                    "date_of_birth": "1980-05-01",
                    "date_of_expiry": "2035-08-29",
                    "sex": "M",
                    "document_type": "national_id",
                    "front_image_index": 0,
                }
            ],
        }

        batch = self.client.post(
            f"{self.base}/batch/",
            {"files": [_tiny_jpeg()]},
            format="multipart",
        )
        job_id = batch.json()["job_id"]
        self.client.post(f"{self.base}/jobs/{job_id}/process/")
        apply = self.client.post(f"{self.base}/jobs/{job_id}/apply/", {}, format="json")
        self.assertEqual(apply.status_code, 200)

        self.primary.refresh_from_db()
        self.reservation.refresh_from_db()
        self.assertEqual(self.primary.nationality, "PL")
        self.assertEqual(self.primary.document_country_iso2, "PL")
        self.assertEqual(self.primary.document_country_iso3, "POL")
        self.assertEqual(self.reservation.booker_country, "PL")

    @patch("apps.reservations.document_intake_service.ocr_configured", return_value=True)
    @patch("apps.reservations.document_intake_service.run_document_batch_ocr")
    def test_name_match_auto_apply_despite_other_unfilled_slots(
        self, mock_ocr, _mock_ocr_configured
    ):
        """Single name match must auto-apply even when other reservations have empty slots."""
        mock_ocr.return_value = {
            "images": [{"index": 0, "side": "front", "mrz_lines": [], "ocr_text": ""}],
            "persons": [
                {
                    "given_names": "Hans",
                    "surnames": "Fischer",
                    "document_number": "L01X00T47",
                    "nationality": "DEU",
                    "document_type": "national_id",
                    "front_image_index": 0,
                }
            ],
        }

        today = timezone.localdate()
        for day_offset, booker in ((1, "Other A"), (2, "Other B")):
            other = Reservation.objects.create(
                tenant=self.tenant,
                property=self.property,
                external_id=f"other-{day_offset}",
                booking_code=f"other-{day_offset}",
                check_in=today + timedelta(days=day_offset),
                check_out=today + timedelta(days=day_offset + 2),
                status=Reservation.Status.EXPECTED,
                booker_name=booker,
            )
            Guest.objects.create(
                tenant=self.tenant,
                reservation=other,
                first_name=PLACEHOLDER_FIRST,
                last_name=PLACEHOLDER_LAST,
                name="Novi gost",
            )

        batch = self.client.post(
            f"{self.base}/batch/",
            {"files": [_tiny_jpeg()]},
            format="multipart",
        )
        job_id = batch.json()["job_id"]
        process = self.client.post(f"{self.base}/jobs/{job_id}/process/")
        body = process.json()

        self.assertEqual(body["status"], DocumentIntakeJobStatus.DONE)
        match = body["matches"][0]
        self.assertEqual(match["confidence"], "high")
        self.assertTrue(match["auto_apply"])
        self.assertEqual(match["reservation_id"], self.reservation.id)
        self.assertEqual(match["guest_id"], self.primary.id)
        self.assertEqual(len(match["candidates"]), 1)

    def test_batch_requires_files(self):
        res = self.client.post(f"{self.base}/batch/", {}, format="multipart")
        self.assertEqual(res.status_code, 400)

    @patch("apps.reservations.document_intake_service.ocr_configured", return_value=True)
    @patch("apps.reservations.document_intake_service.run_document_batch_ocr")
    def test_multi_person_updates_primary_and_companion(
        self, mock_ocr, _mock_ocr_configured
    ):
        """Three OCR persons fill primary, Novi gost, and a third slot from persons_count."""
        self.reservation.persons_count = 3
        self.reservation.children_count = 1
        self.reservation.save(update_fields=["persons_count", "children_count"])

        companion = Guest.objects.get(reservation=self.reservation, is_primary=False)

        def _person(given, surnames, doc_no, front_idx):
            return {
                "given_names": given,
                "surnames": surnames,
                "document_number": doc_no,
                "nationality": "DEU",
                "date_of_birth": "1980-05-01",
                "date_of_expiry": "2030-05-01",
                "sex": "M",
                "document_type": "national_id",
                "front_image_index": front_idx,
                "back_image_index": None,
            }

        mock_ocr.return_value = {
            "images": [{"index": i, "side": "front", "mrz_lines": [], "ocr_text": ""} for i in range(3)],
            "persons": [
                _person("Hans", "Fischer", "DOC001", 0),
                _person("Elke", "Fischer", "DOC002", 1),
                _person("Lisa", "Fischer", "DOC003", 2),
            ],
        }

        batch = self.client.post(
            f"{self.base}/batch/",
            {"files": [_tiny_jpeg(f"p{i}.jpg") for i in range(3)]},
            format="multipart",
        )
        job_id = batch.json()["job_id"]
        process = self.client.post(f"{self.base}/jobs/{job_id}/process/")
        body = process.json()

        self.assertEqual(body["status"], DocumentIntakeJobStatus.DONE)
        self.assertEqual(len(body["matches"]), 3)
        self.assertTrue(body["matches"][0]["auto_apply"])

        apply = self.client.post(f"{self.base}/jobs/{job_id}/apply/", {}, format="json")
        self.assertEqual(apply.status_code, 200)
        self.assertEqual(len(apply.json()["applied"]), 3)

        self.primary.refresh_from_db()
        companion.refresh_from_db()
        third = Guest.objects.filter(reservation=self.reservation).exclude(
            pk__in=[self.primary.pk, companion.pk]
        ).get()

        self.assertEqual(self.primary.document_number, "DOC001")
        self.assertEqual(companion.first_name, "Elke")
        self.assertEqual(companion.document_number, "DOC002")
        self.assertEqual(third.first_name, "Lisa")
        self.assertEqual(third.document_number, "DOC003")
        self.assertEqual(self.reservation.guests.count(), 3)

    @patch("apps.reservations.document_intake_service.ocr_configured", return_value=True)
    @patch("apps.reservations.document_intake_service.run_document_batch_ocr")
    def test_reapply_skips_already_applied_guests(self, mock_ocr, _mock_ocr_configured):
        """Re-apply on APPLIED job only updates guests not yet filled."""
        companion = Guest.objects.get(reservation=self.reservation, is_primary=False)
        self.primary.document_number = "EXISTING"
        self.primary.save(update_fields=["document_number"])

        def _person(given, surnames, doc_no, front_idx):
            return {
                "given_names": given,
                "surnames": surnames,
                "document_number": doc_no,
                "nationality": "DEU",
                "date_of_birth": "1980-05-01",
                "sex": "M",
                "document_type": "national_id",
                "front_image_index": front_idx,
            }

        mock_ocr.return_value = {
            "images": [{"index": i, "side": "front", "mrz_lines": [], "ocr_text": ""} for i in range(2)],
            "persons": [
                _person("Hans", "Fischer", "DOC001", 0),
                _person("Elke", "Fischer", "DOC002", 1),
            ],
        }

        batch = self.client.post(
            f"{self.base}/batch/",
            {"files": [_tiny_jpeg("a.jpg"), _tiny_jpeg("b.jpg")]},
            format="multipart",
        )
        job_id = batch.json()["job_id"]
        self.client.post(f"{self.base}/jobs/{job_id}/process/")

        from apps.reservations.models import DocumentIntakeJob

        job = DocumentIntakeJob.objects.get(pk=job_id)
        job.applied_result = [{"guest_id": self.primary.pk, "person_index": 0}]
        job.status = DocumentIntakeJobStatus.APPLIED
        job.save(update_fields=["applied_result", "status"])

        apply = self.client.post(f"{self.base}/jobs/{job_id}/apply/", {}, format="json")
        self.assertEqual(apply.status_code, 200)
        self.assertEqual(len(apply.json()["applied"]), 1)

        self.primary.refresh_from_db()
        companion.refresh_from_db()
        self.assertEqual(self.primary.document_number, "EXISTING")
        self.assertEqual(companion.document_number, "DOC002")

    @patch("apps.reservations.document_intake_service.ocr_configured", return_value=True)
    @patch("apps.reservations.document_intake_service.run_document_batch_ocr")
    def test_batch_with_reservation_id_sets_job_and_scopes_matches(self, mock_ocr, _mock_ocr_cfg):
        mock_ocr.return_value = {
            "images": [{"index": 0, "side": "front", "mrz_lines": [], "ocr_text": ""}],
            "persons": [
                {
                    "given_names": "Hans",
                    "surnames": "Fischer",
                    "document_number": "L01X00T47",
                    "nationality": "DEU",
                    "document_type": "national_id",
                    "front_image_index": 0,
                }
            ],
        }

        batch = self.client.post(
            f"{self.base}/batch/",
            {
                "files": [_tiny_jpeg()],
                "reservation_id": str(self.reservation.pk),
            },
            format="multipart",
        )
        self.assertEqual(batch.status_code, 201)
        job_id = batch.json()["job_id"]

        job = DocumentIntakeJob.objects.get(pk=job_id)
        self.assertEqual(job.reservation_id, self.reservation.pk)
        self.assertEqual(job.source, DocumentIntakeJobSource.HOSPIRA_BATCH)

        process = self.client.post(f"{self.base}/jobs/{job_id}/process/")
        self.assertEqual(process.status_code, 200)
        body = process.json()
        self.assertEqual(body["reservation_id"], self.reservation.pk)
        self.assertEqual(len(body["matches"]), 1)
        self.assertEqual(body["matches"][0]["reservation_id"], self.reservation.pk)
        self.assertIn("completeness", body)
        self.assertIn("is_complete", body["completeness"])

    def test_batch_invalid_reservation_id_rejected(self):
        batch = self.client.post(
            f"{self.base}/batch/",
            {
                "files": [_tiny_jpeg()],
                "reservation_id": "999999",
            },
            format="multipart",
        )
        self.assertEqual(batch.status_code, 400)
