"""Identity consistency: hard match, collision classify, MRZ gate."""

from datetime import date, timedelta

from django.db import IntegrityError, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from apps.properties.models import Property
from apps.reservations.document_intake_context import DocumentIntakeContext
from apps.reservations.document_intake_identity import (
    classify_identity_collision,
    validate_person_against_mrz,
)
from apps.reservations.document_intake_match import match_persons_to_guests
from apps.reservations.document_intake_ocr_fixup import normalize_document_number
from apps.reservations.document_intake_service import apply_document_intake_job
from apps.reservations.document_intake_web_guest import run_web_guest_matching_pipeline
from apps.reservations.guest_slots import PLACEHOLDER_FIRST, PLACEHOLDER_LAST
from apps.reservations.models import (
    DocumentIntakeJob,
    DocumentIntakeJobSource,
    DocumentIntakeJobStatus,
    Guest,
    IdDocument,
    Reservation,
)
from apps.tenants.models import Tenant


class NormalizeDocumentNumberTests(TestCase):
    def test_strips_hyphen_space_case(self):
        self.assertEqual(normalize_document_number("AB-123456"), "AB123456")
        self.assertEqual(normalize_document_number("ab 123456"), "AB123456")
        self.assertEqual(normalize_document_number("AB123456"), "AB123456")


class DocumentIntakeIdentityMatchTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Id Match", slug="id-match")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Prop",
            slug="prop-id",
            address="X",
        )
        today = timezone.now().date()
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            external_id="ext-id-1",
            booking_code="code-id-1",
            check_in=today,
            check_out=today + timedelta(days=1),
            status=Reservation.Status.EXPECTED,
            booker_name="Danijela Test",
            adults_count=2,
            persons_count=2,
        )
        self.primary = Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="Danijela",
            last_name="Test",
            name="Danijela Test",
            is_primary=True,
            document_number="119273303",
            sex="F",
            date_of_birth=date(1969, 5, 4),
        )
        self.placeholder = Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name=PLACEHOLDER_FIRST,
            last_name=PLACEHOLDER_LAST,
            name="Novi gost",
            is_primary=False,
        )

    def test_document_number_hard_match_is_terminal(self):
        persons = [
            {
                "given_names": "COMPLETELY",
                "surnames": "DIFFERENT",
                "document_number": "119-273-303",
            }
        ]
        matches = match_persons_to_guests(
            tenant_id=self.tenant.pk,
            persons=persons,
            reservation_id=self.reservation.pk,
        )
        self.assertEqual(matches[0]["guest_id"], self.primary.pk)
        self.assertEqual(matches[0]["match_type"], "document_number")
        self.assertTrue(matches[0]["auto_apply"])

    def test_unfilled_slot_not_used_when_doc_exists(self):
        persons = [{"document_number": "119273303", "given_names": "", "surnames": ""}]
        matches = match_persons_to_guests(
            tenant_id=self.tenant.pk,
            persons=persons,
            reservation_id=self.reservation.pk,
        )
        self.assertEqual(matches[0]["guest_id"], self.primary.pk)
        self.assertNotEqual(matches[0]["match_type"], "unfilled_slot")

    def test_mrz_hard_match(self):
        mrz = "IDHRV119273303<<<<<<<<<<<<<<<\n6905045F3001015HRV<<<<<<<<<<<\nTEST<<DANIJELA<<<<<<<<<<<<<<<"
        self.primary.mrz_raw_text = mrz
        self.primary.document_number = ""
        self.primary.save(update_fields=["mrz_raw_text", "document_number"])
        persons = [
            {
                "given_names": "Other",
                "surnames": "Name",
                "mrz_lines": mrz.split("\n"),
            }
        ]
        matches = match_persons_to_guests(
            tenant_id=self.tenant.pk,
            persons=persons,
            reservation_id=self.reservation.pk,
        )
        self.assertEqual(matches[0]["guest_id"], self.primary.pk)
        self.assertEqual(matches[0]["match_type"], "mrz")

    def test_classify_already_processed_and_duplicate(self):
        person = {"document_number": "119273303"}
        same = classify_identity_collision(
            reservation=self.reservation,
            person=person,
            target_guest_id=self.primary.pk,
        )
        self.assertEqual(same.status, "already_processed")
        other = classify_identity_collision(
            reservation=self.reservation,
            person=person,
            target_guest_id=self.placeholder.pk,
        )
        self.assertEqual(other.status, "duplicate_identity")
        self.assertEqual(other.existing_guest_id, self.primary.pk)

    def test_web_guest_duplicate_on_other_slot(self):
        job = DocumentIntakeJob.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            source=DocumentIntakeJobSource.WEB_GUEST,
            guest_checkin_slot_position=2,
            status=DocumentIntakeJobStatus.DONE,
            ocr_result={
                "persons": [
                    {
                        "given_names": "Danijela",
                        "surnames": "Test",
                        "document_number": "119273303",
                    }
                ]
            },
        )
        ctx = DocumentIntakeContext.from_job(job)
        matches = run_web_guest_matching_pipeline(
            ctx=ctx,
            persons=job.ocr_result["persons"],
        )
        self.assertFalse(matches[0]["auto_apply"])
        self.assertEqual(matches[0]["identity_status"], "duplicate_identity")

    def test_web_guest_already_processed_same_slot(self):
        job = DocumentIntakeJob.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            source=DocumentIntakeJobSource.WEB_GUEST,
            guest_checkin_slot_position=1,
            status=DocumentIntakeJobStatus.DONE,
            ocr_result={
                "persons": [{"document_number": "119273303", "given_names": "D", "surnames": "T"}]
            },
        )
        ctx = DocumentIntakeContext.from_job(job)
        matches = run_web_guest_matching_pipeline(
            ctx=ctx, persons=job.ocr_result["persons"]
        )
        self.assertFalse(matches[0]["auto_apply"])
        self.assertEqual(matches[0]["identity_status"], "already_processed")

    def test_mrz_inconsistent_validation(self):
        person = {
            "document_number": "119273303",
            "sex": "M",
            "date_of_birth": "1969-05-04",
            "mrz_lines": [
                "IDHRV119273303<<<<<<<<<<<<<<<",
                "6905045F3001015HRV<<<<<<<<<<<",
                "TEST<<DANIJELA<<<<<<<<<<<<<<<",
            ],
        }
        mismatches = validate_person_against_mrz(person)
        self.assertIn("sex_mismatch", mismatches)

    def test_apply_duplicate_does_not_write_face(self):
        before_docs = IdDocument.objects.filter(guest=self.placeholder).count()
        job = DocumentIntakeJob.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            source=DocumentIntakeJobSource.WEB_GUEST,
            guest_checkin_slot_position=2,
            status=DocumentIntakeJobStatus.DONE,
            ocr_result={
                "persons": [
                    {
                        "given_names": "Danijela",
                        "surnames": "Test",
                        "document_number": "119273303",
                        "sex": "F",
                    }
                ]
            },
            matches=[],
        )
        ctx = DocumentIntakeContext.from_job(job)
        outcomes = apply_document_intake_job(ctx, whatsapp_reply=False)
        self.assertTrue(outcomes)
        self.assertEqual(outcomes[0].get("identity_status"), "duplicate_identity")
        self.assertFalse(outcomes[0].get("face_photo_saved"))
        self.assertEqual(
            IdDocument.objects.filter(guest=self.placeholder).count(), before_docs
        )
        self.placeholder.refresh_from_db()
        self.assertEqual(self.placeholder.document_number, "")


class GuestDocumentUniqueConstraintTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Unique Doc", slug="uniq-doc")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="P",
            slug="p-uniq",
            address="A",
        )
        today = timezone.now().date()
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            external_id="ext-u-1",
            booking_code="code-u-1",
            check_in=today,
            check_out=today + timedelta(days=1),
            status=Reservation.Status.EXPECTED,
            booker_name="A B",
            adults_count=2,
            persons_count=2,
        )
        self.g1 = Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="A",
            last_name="B",
            name="A B",
            is_primary=True,
            document_number="DOCUNIQUE1",
        )
        self.g2 = Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="Novi",
            last_name="gost",
            name="Novi gost",
            is_primary=False,
        )

    def test_unique_document_per_reservation(self):
        self.g2.document_number = "DOCUNIQUE1"
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.g2.save()


class ConcurrentIdentityApplyTests(TransactionTestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Conc Id", slug="conc-id")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="P",
            slug="p-conc",
            address="A",
        )
        today = timezone.now().date()
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            external_id="ext-c-1",
            booking_code="code-c-1",
            check_in=today,
            check_out=today + timedelta(days=1),
            status=Reservation.Status.EXPECTED,
            booker_name="A B",
            adults_count=2,
            persons_count=2,
        )
        self.g1 = Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="A",
            last_name="B",
            name="A B",
            is_primary=True,
        )
        self.g2 = Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="Novi",
            last_name="gost",
            name="Novi gost",
            is_primary=False,
        )

    def test_two_applies_same_doc_one_guest(self):
        person = {
            "given_names": "A",
            "surnames": "B",
            "document_number": "CONCDOC99",
            "sex": "F",
        }

        def make_job(position: int) -> DocumentIntakeJob:
            return DocumentIntakeJob.objects.create(
                tenant=self.tenant,
                reservation=self.reservation,
                source=DocumentIntakeJobSource.WEB_GUEST,
                guest_checkin_slot_position=position,
                status=DocumentIntakeJobStatus.DONE,
                ocr_result={"persons": [person]},
                matches=[],
            )

        job_a = make_job(1)
        job_b = make_job(2)
        apply_document_intake_job(DocumentIntakeContext.from_job(job_a), whatsapp_reply=False)
        outcomes_b = apply_document_intake_job(
            DocumentIntakeContext.from_job(job_b), whatsapp_reply=False
        )

        self.g1.refresh_from_db()
        self.g2.refresh_from_db()
        holders = list(
            Guest.objects.filter(
                reservation=self.reservation,
                document_number="CONCDOC99",
            )
        )
        self.assertEqual(len(holders), 1)
        self.assertEqual(holders[0].pk, self.g1.pk)
        self.assertTrue(
            any(o.get("identity_status") == "duplicate_identity" for o in outcomes_b)
        )
        self.assertEqual(IdDocument.objects.filter(guest=self.g2).count(), 0)
