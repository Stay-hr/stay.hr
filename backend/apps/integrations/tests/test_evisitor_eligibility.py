from datetime import date
from decimal import Decimal

from django.test import TestCase

from apps.integrations.evisitor.eligibility import guest_requires_evisitor
from apps.integrations.evisitor.summary import evisitor_progress_for_guests, evisitor_summary_for_guests
from apps.properties.models import Property
from apps.reservations.models import EvisitorGuestStatus, Guest, Reservation
from apps.tenants.models import Tenant


class EvisitorEligibilityTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Uzorita", slug="uzorita-ev")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Uzorita",
            slug="uzorita-ev",
        )
        self.check_in = date(2026, 5, 26)
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="BK-EV-MINOR",
            check_in=self.check_in,
            check_out=date(2026, 5, 30),
            status=Reservation.Status.CHECKED_IN,
            booker_name="Atacan",
            amount=Decimal("300.00"),
            adults_count=2,
        )

    def _guest(self, **kwargs) -> Guest:
        defaults = {
            "tenant": self.tenant,
            "reservation": self.reservation,
            "first_name": "Test",
            "last_name": "Guest",
            "is_primary": False,
            "evisitor_status": EvisitorGuestStatus.NOT_SENT,
        }
        defaults.update(kwargs)
        return Guest.objects.create(**defaults)

    def test_child_under_18_does_not_require_evisitor(self):
        child = self._guest(date_of_birth=date(2014, 7, 16))
        self.assertFalse(guest_requires_evisitor(child))

    def test_adult_requires_evisitor(self):
        adult = self._guest(date_of_birth=date(1980, 1, 1))
        self.assertTrue(guest_requires_evisitor(adult))

    def test_missing_dob_requires_evisitor(self):
        guest = self._guest(date_of_birth=None)
        self.assertTrue(guest_requires_evisitor(guest))

    def test_summary_complete_when_adults_sent_and_child_not_sent(self):
        adult1 = self._guest(
            first_name="Ekrem",
            date_of_birth=date(1975, 3, 1),
            evisitor_status=EvisitorGuestStatus.SENT,
        )
        adult2 = self._guest(
            first_name="Keziban",
            date_of_birth=date(1978, 6, 15),
            evisitor_status=EvisitorGuestStatus.SENT,
        )
        child = self._guest(
            first_name="Emir",
            date_of_birth=date(2014, 7, 16),
            evisitor_status=EvisitorGuestStatus.NOT_SENT,
        )

        summary = evisitor_summary_for_guests(
            [adult1, adult2, child],
            reference_date=self.check_in,
        )
        self.assertEqual(summary, "complete")

    def test_summary_complete_when_only_children(self):
        child1 = self._guest(date_of_birth=date(2014, 7, 16))
        child2 = self._guest(date_of_birth=date(2016, 2, 2))

        summary = evisitor_summary_for_guests(
            [child1, child2],
            reference_date=self.check_in,
        )
        self.assertEqual(summary, "complete")

    def test_summary_incomplete_when_adult_not_sent(self):
        adult = self._guest(date_of_birth=date(1980, 1, 1))
        child = self._guest(date_of_birth=date(2014, 7, 16))

        summary = evisitor_summary_for_guests(
            [adult, child],
            reference_date=self.check_in,
        )
        self.assertEqual(summary, "incomplete")

    def test_progress_counts_eligible_guests_only(self):
        adult_sent = self._guest(
            date_of_birth=date(1980, 1, 1),
            evisitor_status=EvisitorGuestStatus.SENT,
        )
        adult_pending = self._guest(
            first_name="Pending",
            date_of_birth=date(1985, 5, 5),
            evisitor_status=EvisitorGuestStatus.NOT_SENT,
        )
        adult_failed = self._guest(
            first_name="Failed",
            date_of_birth=date(1970, 3, 3),
            evisitor_status=EvisitorGuestStatus.FAILED,
        )
        adult_checkout_failed = self._guest(
            first_name="CheckoutFailed",
            date_of_birth=date(1972, 2, 2),
            evisitor_status=EvisitorGuestStatus.CHECKOUT_FAILED,
        )
        child = self._guest(date_of_birth=date(2014, 7, 16))

        progress = evisitor_progress_for_guests(
            [adult_sent, adult_pending, adult_failed, adult_checkout_failed, child],
            reference_date=self.check_in,
        )
        self.assertEqual(progress, {"required": 4, "sent": 2, "failed": 1, "pending": 1})

        summary = evisitor_summary_for_guests(
            [adult_sent, adult_checkout_failed],
            reference_date=self.check_in,
        )
        self.assertEqual(summary, "complete")
