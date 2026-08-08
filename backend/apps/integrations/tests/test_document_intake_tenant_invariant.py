"""Document intake tenant invariant — cross-tenant WA, legacy heal, idempotency."""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from apps.properties.models import Property
from apps.reservations.document_intake_audit import rematch_and_audit_job
from apps.reservations.document_intake_context import DocumentIntakeContext, ensure_job_tenant_matches_reservation
from apps.reservations.document_intake_service import apply_document_intake_job, process_document_intake_job
from apps.reservations.models import (
    DocumentIntakeImage,
    DocumentIntakeJob,
    DocumentIntakeJobSource,
    DocumentIntakeJobStatus,
    Guest,
    Reservation,
)
from apps.tenants.models import Tenant


def _ocr_persons_for_reservation(reservation: Reservation) -> tuple[list[dict], list[dict]]:
    primary = reservation.guests.get(is_primary=True)
    secondary = reservation.guests.filter(is_primary=False).first()
    persons = [
        {
            "given_names": primary.first_name,
            "surnames": primary.last_name,
            "document_number": "DOC-PRIMARY-1",
            "document_type": "national_id",
            "front_image_index": 0,
            "back_image_index": 1,
            "nationality": "HRV",
            "date_of_birth": "1985-01-01",
            "date_of_expiry": "2030-01-01",
            "sex": "M",
        },
    ]
    matches = [
        {
            "person_index": 0,
            "auto_apply": True,
            "guest_id": primary.pk,
            "reservation_id": reservation.pk,
            "guest_name": primary.name,
        },
    ]
    if secondary is not None:
        persons.append(
            {
                "given_names": secondary.first_name,
                "surnames": secondary.last_name,
                "document_number": "DOC-SECONDARY-1",
                "document_type": "national_id",
                "front_image_index": 2,
                "back_image_index": 3,
                "nationality": "HRV",
                "date_of_birth": "1990-01-01",
                "date_of_expiry": "2030-01-01",
                "sex": "F",
            }
        )
        matches.append(
            {
                "person_index": 1,
                "auto_apply": True,
                "guest_id": secondary.pk,
                "reservation_id": reservation.pk,
                "guest_name": secondary.name,
            }
        )
    return persons, matches


class DocumentIntakeTenantInvariantTests(TestCase):
    def setUp(self):
        self.demo_tenant = Tenant.objects.create(slug="demo", name="Demo", default_language="hr")
        # Migration 0016 seeds slug=platform; reuse it under keepdb / full-suite runs.
        self.platform_tenant, _ = Tenant.objects.get_or_create(
            slug="platform",
            defaults={
                "name": "Platform",
                "default_language": "hr",
                "is_system": True,
            },
        )
        self.property = Property.objects.create(
            tenant=self.demo_tenant,
            name="Demo Property",
            slug="demo-property",
        )
        self.reservation = Reservation.objects.create(
            tenant=self.demo_tenant,
            property=self.property,
            booker_name="Ante Vrcan",
            adults_count=2,
            persons_count=2,
            check_in=date(2026, 7, 1),
            check_out=date(2026, 7, 5),
            status=Reservation.Status.EXPECTED,
        )
        self.primary = Guest.objects.create(
            tenant=self.demo_tenant,
            reservation=self.reservation,
            first_name="Ante",
            last_name="Vrcan",
            name="Ante Vrcan",
            is_primary=True,
        )
        self.secondary = Guest.objects.create(
            tenant=self.demo_tenant,
            reservation=self.reservation,
            first_name="Novi",
            last_name="gost",
            name="Novi gost",
        )

    def _attach_images(self, job: DocumentIntakeJob, count: int = 4) -> None:
        for i in range(count):
            DocumentIntakeImage.objects.create(
                tenant_id=job.tenant_id,
                job=job,
                image=SimpleUploadedFile(
                    f"img{i}.jpg",
                    f"unique-image-bytes-{i}".encode(),
                    content_type="image/jpeg",
                ),
                sort_order=i,
            )

    def _done_job(self, *, tenant_id: int, persons: list[dict], matches: list[dict]) -> DocumentIntakeJob:
        job = DocumentIntakeJob.objects.create(
            tenant_id=tenant_id,
            reservation=self.reservation,
            status=DocumentIntakeJobStatus.DONE,
            source=DocumentIntakeJobSource.HOSPIRA_BATCH,
            ocr_result={"persons": persons},
            matches=matches,
        )
        self._attach_images(job)
        return job

    @patch("apps.reservations.document_intake_service.crop_face_jpeg", return_value=None)
    def test_same_tenant_reception_batch_match_and_apply(self, _mock_crop):
        persons, matches = _ocr_persons_for_reservation(self.reservation)
        job = self._done_job(tenant_id=self.demo_tenant.pk, persons=persons, matches=matches)
        self.assertEqual(job.tenant_id, self.reservation.tenant_id)

        ctx = DocumentIntakeContext.from_job(job)
        refreshed = rematch_and_audit_job(ctx)
        self.assertTrue(refreshed[0]["auto_apply"])

        applied = apply_document_intake_job(ctx, allow_partial=True, whatsapp_reply=False)
        job.refresh_from_db()
        self.assertGreaterEqual(len(applied), 1)
        self.assertEqual(job.tenant_id, self.reservation.tenant_id)

    def test_cross_tenant_wa_create_uses_reservation_tenant(self):
        intake_tenant_id = self.reservation.tenant_id
        job = DocumentIntakeJob.objects.create(
            tenant_id=intake_tenant_id,
            reservation=self.reservation,
            source=DocumentIntakeJobSource.WHATSAPP,
            status=DocumentIntakeJobStatus.QUEUED,
            device_id="whatsapp",
        )
        ensure_job_tenant_matches_reservation(job, self.reservation)
        self.assertEqual(job.tenant_id, self.demo_tenant.pk)
        self.assertNotEqual(job.tenant_id, self.platform_tenant.pk)

        persons, _matches = _ocr_persons_for_reservation(self.reservation)
        job.status = DocumentIntakeJobStatus.DONE
        job.ocr_result = {"persons": persons}
        job.save(update_fields=["status", "ocr_result", "updated_at"])
        self._attach_images(job)

        ctx = DocumentIntakeContext.from_job(job)
        matches = rematch_and_audit_job(ctx)
        self.assertTrue(matches[0]["auto_apply"])
        self.assertEqual(matches[0]["guest_id"], self.primary.pk)

    def test_legacy_mismatched_job_healed_once(self):
        persons, _matches = _ocr_persons_for_reservation(self.reservation)
        job = self._done_job(tenant_id=self.platform_tenant.pk, persons=persons, matches=[])

        with self.assertLogs("apps.reservations.document_intake_context", level="ERROR") as logs:
            ctx = DocumentIntakeContext.from_job(job)
        self.assertTrue(any("tenant mismatch healed" in line for line in logs.output))
        job.refresh_from_db()
        self.assertEqual(job.tenant_id, self.demo_tenant.pk)
        self.assertEqual(ctx.effective_tenant_id, self.demo_tenant.pk)

        matches = rematch_and_audit_job(ctx)
        self.assertTrue(matches[0]["auto_apply"])
        self.assertEqual(matches[0]["guest_id"], self.primary.pk)

    @patch("apps.reservations.document_intake_service.crop_face_jpeg", return_value=None)
    @patch("apps.reservations.document_intake_service.ocr_configured", return_value=True)
    @patch("apps.reservations.document_intake_service.run_document_batch_ocr")
    def test_idempotency_process_and_apply(self, mock_ocr, _mock_configured, _mock_crop):
        persons, matches = _ocr_persons_for_reservation(self.reservation)
        mock_ocr.return_value = {"persons": persons, "images": []}

        job = DocumentIntakeJob.objects.create(
            tenant_id=self.demo_tenant.pk,
            reservation=self.reservation,
            status=DocumentIntakeJobStatus.QUEUED,
            source=DocumentIntakeJobSource.HOSPIRA_BATCH,
        )
        self._attach_images(job)

        ctx = DocumentIntakeContext.from_job(job)
        guest_count_before = self.reservation.guests.count()
        tenant_before = job.tenant_id

        process_document_intake_job(ctx)
        job.refresh_from_db()
        matches_after_first = list(job.matches or [])

        process_document_intake_job(DocumentIntakeContext.from_job(job))
        job.refresh_from_db()
        self.assertEqual(job.matches, matches_after_first)

        ctx = DocumentIntakeContext.from_job(job)
        apply_document_intake_job(ctx, allow_partial=True, whatsapp_reply=False)
        job.refresh_from_db()
        applied_after_first = list(job.applied_result or [])

        apply_document_intake_job(ctx, allow_partial=True, whatsapp_reply=False)
        job.refresh_from_db()

        self.assertEqual(self.reservation.guests.count(), guest_count_before)
        self.assertEqual(job.tenant_id, tenant_before)
        self.assertEqual(len(job.applied_result or []), len(applied_after_first))
