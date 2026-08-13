from datetime import date

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from apps.communications.guest_compose import render_missing_id_sides_message
from apps.properties.models import Property
from apps.reservations.document_intake_sides import MissingIdSide, find_missing_id_sides
from apps.reservations.models import Guest, IdDocument, Reservation
from apps.tenants.models import Tenant


class DocumentIntakeSidesTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="uzorita", name="Uzorita", default_language="hr")
        self.property = Property.objects.create(tenant=self.tenant, name="Uzorita", slug="uzorita")
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booker_name="Audrius Kavaliauskas",
            adults_count=2,
            check_in=date(2026, 6, 7),
            check_out=date(2026, 6, 9),
            status=Reservation.Status.EXPECTED,
        )

    def _adult(self, *, first: str, last: str, dob: date) -> Guest:
        return Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name=first,
            last_name=last,
            name=f"{first} {last}",
            date_of_birth=dob,
            document_type="Osobna iskaznica",
        )

    def _child(self) -> Guest:
        return Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="Adomas",
            last_name="Sirokinas",
            name="Adomas Sirokinas",
            date_of_birth=date(2013, 12, 3),
            document_type="Osobna iskaznica",
        )

    def _attach_front(self, guest: Guest) -> IdDocument:
        doc = IdDocument.objects.create(guest=guest)
        doc.front_photo.save(
            f"guest_{guest.pk}_front.jpg",
            SimpleUploadedFile("front.jpg", b"front-bytes"),
            save=True,
        )
        return doc

    def test_national_id_front_only_missing_back(self):
        adult = self._adult(first="Laura", last="Matonyte", dob=date(1992, 10, 10))
        self._attach_front(adult)

        gaps = find_missing_id_sides(self.reservation)

        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0].guest_name, "Laura Matonyte")
        self.assertEqual(gaps[0].side, "back")
        self.assertFalse(gaps[0].is_passport)

    def test_national_id_front_and_back_complete(self):
        adult = self._adult(first="Laura", last="Matonyte", dob=date(1992, 10, 10))
        doc = self._attach_front(adult)
        doc.back_photo.save(
            f"guest_{adult.pk}_back.jpg",
            SimpleUploadedFile("back.jpg", b"back-bytes"),
            save=True,
        )

        self.assertEqual(find_missing_id_sides(self.reservation), [])

    def test_national_id_front_and_back_on_separate_id_documents(self):
        adult = self._adult(first="Ante", last="Vrcan", dob=date(1980, 1, 1))
        front_doc = self._attach_front(adult)
        back_doc = IdDocument.objects.create(guest=adult)
        back_doc.back_photo.save(
            f"guest_{adult.pk}_back.jpg",
            SimpleUploadedFile("back.jpg", b"back-bytes"),
            save=True,
        )

        self.assertEqual(find_missing_id_sides(self.reservation), [])

    def test_passport_front_only_is_complete(self):
        adult = self._adult(first="Markus", last="Zoehrer", dob=date(1980, 1, 1))
        adult.document_type = "Putovnica"
        adult.save(update_fields=["document_type"])
        doc = self._attach_front(adult)
        doc._passport_photo = True

        self.assertEqual(find_missing_id_sides(self.reservation), [])

    def test_passport_missing_front(self):
        adult = self._adult(first="Markus", last="Zoehrer", dob=date(1980, 1, 1))
        adult.document_type = "Putovnica"
        adult.save(update_fields=["document_type"])
        IdDocument.objects.create(guest=adult, extracted_payload={"person": {"document_type": "passport"}})

        gaps = find_missing_id_sides(self.reservation)

        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0].side, "front")
        self.assertTrue(gaps[0].is_passport)

    def test_child_with_partial_id_missing_back(self):
        child = self._child()
        self._attach_front(child)

        gaps = find_missing_id_sides(self.reservation)

        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0].guest_name, "Adomas Sirokinas")
        self.assertEqual(gaps[0].side, "back")
        self.assertFalse(gaps[0].is_passport)

    def test_render_message_lists_guest_and_side(self):
        adult = self._adult(first="Audrius", last="Kavaliauskas", dob=date(1989, 2, 13))
        self._attach_front(adult)
        gaps = find_missing_id_sides(self.reservation)
        text = render_missing_id_sides_message(self.reservation, gaps)

        self.assertIn("Audrius Kavaliauskas", text)
        self.assertIn("stražnja strana osobne iskaznice", text)
