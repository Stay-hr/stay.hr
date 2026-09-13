from datetime import date

from django.test import TestCase
from django.utils import timezone

from apps.properties.models import Property
from apps.reservations.evisitor_identity import (
    invoice_delivery_blocked_guest,
    is_evisitor_identity_invented,
)
from apps.reservations.models import Guest, Reservation
from apps.tenants.models import Tenant


class EvisitorIdentityTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Identity Tenant", slug="evisitor-id")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="P",
            slug="p",
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            check_in=date(2026, 9, 12),
            check_out=date(2026, 9, 13),
            status=Reservation.Status.CHECKED_OUT,
            booker_name="Primary Guest",
        )
        self.primary = Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="Primary",
            last_name="Guest",
            is_primary=True,
        )
        self.secondary = Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="Second",
            last_name="Guest",
            is_primary=False,
        )

    def test_unmarked_primary_does_not_block(self):
        self.assertFalse(is_evisitor_identity_invented(self.primary))
        self.assertIsNone(invoice_delivery_blocked_guest(self.reservation))

    def test_primary_invented_blocks_delivery(self):
        self.primary.evisitor_identity_invented_at = timezone.now()
        self.primary.save(update_fields=["evisitor_identity_invented_at"])
        self.assertTrue(is_evisitor_identity_invented(self.primary))
        self.assertEqual(invoice_delivery_blocked_guest(self.reservation), self.primary)

    def test_secondary_invented_does_not_block(self):
        self.secondary.evisitor_identity_invented_at = timezone.now()
        self.secondary.save(update_fields=["evisitor_identity_invented_at"])
        self.assertTrue(is_evisitor_identity_invented(self.secondary))
        self.assertIsNone(invoice_delivery_blocked_guest(self.reservation))
