from datetime import date

from django.test import TestCase

from apps.properties.models import Property
from apps.reservations.guest_slots import (
    ensure_guest_slots_for_intake,
    target_document_guest_count,
    target_intake_guest_count,
)
from apps.reservations.models import Guest, Reservation
from apps.tenants.models import Tenant


class TargetDocumentGuestCountTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="slot-d", name="Slot D")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Hotel",
            slug="hotel-slot-d",
        )

    def _reservation(self, **kwargs) -> Reservation:
        defaults = {
            "tenant": self.tenant,
            "property": self.property,
            "booker_name": "Booker",
            "check_in": date(2026, 7, 1),
            "check_out": date(2026, 7, 5),
            "status": Reservation.Status.EXPECTED,
        }
        defaults.update(kwargs)
        return Reservation.objects.create(**defaults)

    def test_ignores_persons_count_when_adults_count_set(self):
        """#978 class: 4 adults + 4 children must not inflate to 8 intake slots."""
        reservation = self._reservation(adults_count=4, children_count=4, persons_count=8)
        self.assertEqual(target_document_guest_count(reservation=reservation, min_count=4), 4)
        self.assertEqual(target_intake_guest_count(reservation=reservation, min_count=4), 8)

    def test_ocr_min_count_cannot_inflate_above_adults_policy(self):
        """OCR person count must not create slots above expected_document_count."""
        reservation = self._reservation(adults_count=2, persons_count=3)
        self.assertEqual(target_document_guest_count(reservation=reservation, min_count=3), 2)

    def test_ocr_min_count_capped_by_expected_checkin_adults(self):
        reservation = self._reservation(adults_count=2, persons_count=2)
        reservation.expected_checkin_adults = 1
        reservation.save(update_fields=["expected_checkin_adults"])
        self.assertEqual(target_document_guest_count(reservation=reservation, min_count=2), 1)

    def test_never_below_existing_guest_rows(self):
        reservation = self._reservation(adults_count=2, persons_count=2)
        Guest.objects.create(
            tenant=self.tenant,
            reservation=reservation,
            first_name="A",
            last_name="A",
            name="A A",
            is_primary=True,
        )
        Guest.objects.create(
            tenant=self.tenant,
            reservation=reservation,
            first_name="B",
            last_name="B",
            name="B B",
        )
        Guest.objects.create(
            tenant=self.tenant,
            reservation=reservation,
            first_name="C",
            last_name="C",
            name="C C",
        )
        self.assertEqual(target_document_guest_count(reservation=reservation, min_count=2), 3)

    def test_zero_adults_uses_ocr_min_count(self):
        reservation = self._reservation(adults_count=0, persons_count=2)
        self.assertEqual(target_document_guest_count(reservation=reservation, min_count=2), 2)


class EnsureGuestSlotsForIntakeTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="ensure-d", name="Ensure D")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Hotel",
            slug="hotel-ensure-d",
        )

    def test_does_not_create_slots_for_all_persons_count(self):
        reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booker_name="Laura Lysak",
            check_in=date(2026, 7, 6),
            check_out=date(2026, 7, 10),
            status=Reservation.Status.EXPECTED,
            adults_count=4,
            children_count=4,
            persons_count=8,
        )
        Guest.objects.create(
            tenant=self.tenant,
            reservation=reservation,
            first_name="Laura",
            last_name="Lysak",
            name="Laura Lysak",
            is_primary=True,
        )

        created = ensure_guest_slots_for_intake(
            tenant=self.tenant,
            reservation=reservation,
            min_count=4,
        )
        self.assertEqual(created, 3)
        self.assertEqual(reservation.guests.count(), 4)
