from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from apps.properties.models import Property
from apps.reservations.checkout import CheckoutBlockedError, perform_reservation_checkout
from apps.reservations.guest_slots import (
    PLACEHOLDER_FIRST,
    PLACEHOLDER_LAST,
    is_removable_empty_guest,
    is_unfilled_guest,
    remove_unfilled_secondary_guests,
)
from apps.reservations.models import EvisitorGuestStatus, Guest, Reservation
from apps.tenants.models import Tenant


class CheckoutGuestCleanupTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Uzorita", slug="uzorita")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Uzorita",
            slug="uzorita",
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="BK-CO",
            check_in=date(2026, 5, 10),
            check_out=date(2026, 5, 15),
            status=Reservation.Status.CHECKED_IN,
            booker_name="Ana Anić",
            amount=Decimal("120.00"),
            adults_count=2,
        )

    def _primary(self, **kwargs) -> Guest:
        defaults = {
            "tenant": self.tenant,
            "reservation": self.reservation,
            "first_name": "Ana",
            "last_name": "Anić",
            "is_primary": True,
            "evisitor_status": EvisitorGuestStatus.SENT,
        }
        defaults.update(kwargs)
        return Guest.objects.create(**defaults)

    def _secondary(self, **kwargs) -> Guest:
        defaults = {
            "tenant": self.tenant,
            "reservation": self.reservation,
            "first_name": PLACEHOLDER_FIRST,
            "last_name": PLACEHOLDER_LAST,
            "name": "Novi gost",
            "is_primary": False,
            "evisitor_status": EvisitorGuestStatus.NOT_SENT,
        }
        defaults.update(kwargs)
        return Guest.objects.create(**defaults)

    def test_is_unfilled_guest_placeholder(self):
        guest = self._secondary()
        self.assertTrue(is_unfilled_guest(guest))
        self.assertTrue(is_removable_empty_guest(guest))

    def test_is_unfilled_guest_imported_name_only(self):
        guest = self._secondary(first_name="John", last_name="Smith", name="John Smith")
        self.assertTrue(is_unfilled_guest(guest))
        self.assertTrue(is_removable_empty_guest(guest))

    def test_scanned_but_not_sent_not_removable(self):
        guest = self._secondary(
            first_name="Petra",
            last_name="Petrić",
            name="Petra Petrić",
            document_number="123456789",
        )
        self.assertFalse(is_unfilled_guest(guest))
        self.assertFalse(is_removable_empty_guest(guest))

    def test_primary_unfilled_not_removable(self):
        guest = self._primary(evisitor_status=EvisitorGuestStatus.NOT_SENT)
        self.assertTrue(is_unfilled_guest(guest))
        self.assertFalse(is_removable_empty_guest(guest))

    @patch("apps.reservations.checkout.checkout_reservation_guests_in_evisitor")
    def test_checkout_removes_placeholder_secondary(self, mock_evisitor_checkout):
        mock_evisitor_checkout.return_value = []
        primary = self._primary()
        placeholder = self._secondary()

        perform_reservation_checkout(self.reservation)

        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.status, Reservation.Status.CHECKED_OUT)
        self.assertFalse(Guest.objects.filter(pk=placeholder.pk).exists())
        self.assertTrue(Guest.objects.filter(pk=primary.pk).exists())
        mock_evisitor_checkout.assert_called_once()

    @patch("apps.reservations.checkout.checkout_reservation_guests_in_evisitor")
    def test_checkout_removes_imported_name_without_scan(self, mock_evisitor_checkout):
        mock_evisitor_checkout.return_value = []
        self._primary()
        secondary = self._secondary(
            first_name="Kris",
            last_name="Meeus",
            name="Kris Meeus",
        )

        perform_reservation_checkout(self.reservation)

        self.assertFalse(Guest.objects.filter(pk=secondary.pk).exists())
        self.assertEqual(self.reservation.status, Reservation.Status.CHECKED_OUT)

    @patch("apps.reservations.checkout.checkout_reservation_guests_in_evisitor")
    def test_checkout_blocked_when_minor_not_sent(self, mock_evisitor_checkout):
        self._primary(
            date_of_birth=date(1975, 3, 1),
            evisitor_status=EvisitorGuestStatus.SENT,
        )
        self._secondary(
            first_name="Emir",
            last_name="Atacan",
            name="Emir Atacan",
            date_of_birth=date(2014, 7, 16),
            document_number="U12345678",
            evisitor_status=EvisitorGuestStatus.NOT_SENT,
        )

        with self.assertRaises(CheckoutBlockedError) as ctx:
            perform_reservation_checkout(self.reservation)

        self.assertEqual(ctx.exception.code, "evisitor_incomplete")
        mock_evisitor_checkout.assert_not_called()
        self.assertEqual(self.reservation.status, Reservation.Status.CHECKED_IN)

    @patch("apps.reservations.checkout.checkout_reservation_guests_in_evisitor")
    def test_checkout_allowed_when_adult_and_minor_sent(self, mock_evisitor_checkout):
        mock_evisitor_checkout.return_value = []
        self._primary(
            date_of_birth=date(1975, 3, 1),
            evisitor_status=EvisitorGuestStatus.SENT,
        )
        minor = self._secondary(
            first_name="Emir",
            last_name="Atacan",
            name="Emir Atacan",
            date_of_birth=date(2014, 7, 16),
            document_number="U12345678",
            evisitor_status=EvisitorGuestStatus.SENT,
        )

        perform_reservation_checkout(self.reservation)

        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.status, Reservation.Status.CHECKED_OUT)
        self.assertTrue(Guest.objects.filter(pk=minor.pk).exists())
        mock_evisitor_checkout.assert_called_once()

    @patch("apps.reservations.checkout.checkout_reservation_guests_in_evisitor")
    def test_checkout_blocked_when_scanned_adult_secondary_not_sent(self, mock_evisitor_checkout):
        self._primary()
        self._secondary(
            first_name="Petra",
            last_name="Petrić",
            name="Petra Petrić",
            document_number="AB123456",
            date_of_birth=date(1990, 5, 20),
        )

        with self.assertRaises(CheckoutBlockedError) as ctx:
            perform_reservation_checkout(self.reservation)

        self.assertEqual(ctx.exception.code, "evisitor_incomplete")
        mock_evisitor_checkout.assert_not_called()
        self.assertEqual(self.reservation.status, Reservation.Status.CHECKED_IN)

    @patch("apps.reservations.checkout.checkout_reservation_guests_in_evisitor")
    def test_checkout_blocked_when_primary_unfilled(self, mock_evisitor_checkout):
        self._primary(evisitor_status=EvisitorGuestStatus.NOT_SENT)
        self._secondary()

        with self.assertRaises(CheckoutBlockedError) as ctx:
            perform_reservation_checkout(self.reservation)

        self.assertEqual(ctx.exception.code, "evisitor_incomplete")
        mock_evisitor_checkout.assert_not_called()

    @patch("apps.reservations.checkout.checkout_reservation_guests_in_evisitor")
    def test_checkout_blocked_when_all_secondaries_removed_leaves_none(self, mock_evisitor_checkout):
        self._secondary()
        self._secondary(first_name="Novi", last_name="gost2", name="Novi gost2")

        with self.assertRaises(CheckoutBlockedError) as ctx:
            perform_reservation_checkout(self.reservation)

        self.assertEqual(ctx.exception.code, "evisitor_none")
        mock_evisitor_checkout.assert_not_called()

    def test_remove_unfilled_secondary_guests_count(self):
        self._primary()
        s1 = self._secondary()
        s2 = self._secondary(first_name="Kris", last_name="Meeus", name="Kris Meeus")

        removed = remove_unfilled_secondary_guests(self.reservation)

        self.assertEqual(removed, 2)
        self.assertFalse(Guest.objects.filter(pk__in=[s1.pk, s2.pk]).exists())
        self.assertEqual(self.reservation.guests.count(), 1)
