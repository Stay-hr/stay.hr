from datetime import date
from unittest.mock import patch

from django.test import TestCase

from apps.properties.models import Property, Unit
from apps.reservations.channel_availability_sync import queue_sync_if_units_changed
from apps.reservations.models import Reservation, ReservationUnit
from apps.reservations.reservation_units import sync_reservation_units
from apps.tenants.models import Tenant


class ChannelAvailabilitySyncTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="demo", name="Demo")
        self.property = Property.objects.create(
            tenant=self.tenant,
            slug="p1",
            name="P1",
            timezone="Europe/Zagreb",
        )
        self.unit_a = Unit.objects.create(
            tenant=self.tenant,
            property=self.property,
            code="R1",
            name="R1",
        )
        self.unit_b = Unit.objects.create(
            tenant=self.tenant,
            property=self.property,
            code="R2",
            name="R2",
        )

    @patch(
        "apps.integrations.channel_manager.tasks.push_reservation_availability_task"
    )
    def test_queue_sync_when_units_change_via_sync_reservation_units(self, mock_task):
        reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            check_in=date(2026, 6, 4),
            check_out=date(2026, 6, 5),
            status=Reservation.Status.EXPECTED,
            booker_name="Wolfgang",
            import_source="booking_pdf",
        )
        ReservationUnit.objects.create(
            tenant=self.tenant,
            reservation=reservation,
            unit=self.unit_b,
            room_name="R2",
        )

        from apps.reservations.channel_availability_sync import reservation_unit_codes

        before = reservation_unit_codes(reservation)
        sync_reservation_units(
            tenant=self.tenant,
            property=self.property,
            reservation=reservation,
            room_name="Luxury Room Uzorita - R1, Luxury Room Uzorita - R2",
        )
        queued = queue_sync_if_units_changed(reservation, before_codes=before)
        self.assertTrue(queued)
