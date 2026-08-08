from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase

from apps.properties.models import Property, Unit
from apps.reservations.models import MonthlyStatisticsOverride, Reservation, ReservationUnit
from apps.reservations.statistics import aggregate_monthly_statistics
from apps.tenants.models import Tenant


class MonthlyStatisticsTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Uzorita", slug="uzorita")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Uzorita Luxury Rooms",
            slug="uzorita",
        )

    def _create_reservation(self, *, check_in, amount, status, nights=None, units_count=None):
        return Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            check_in=check_in,
            check_out=check_in + timedelta(days=nights or 3),
            status=status,
            booker_name="Test Booker",
            amount=amount,
            commission_amount=Decimal("10.00"),
            nights_count=nights or 3,
            currency="EUR",
            units_count=units_count,
        )

    def _create_unit(self, code: str) -> Unit:
        return Unit.objects.create(
            tenant=self.tenant,
            property=self.property,
            code=code,
            name=f"Room {code}",
            is_active=True,
        )

    def _link_unit(self, reservation: Reservation, unit: Unit) -> ReservationUnit:
        return ReservationUnit.objects.create(
            tenant=self.tenant,
            reservation=reservation,
            unit=unit,
            sort_order=0,
            room_name=unit.name,
        )

    def test_aggregate_current_and_previous_year(self):
        self._create_reservation(
            check_in=date(2025, 3, 10),
            amount=Decimal("100.00"),
            status=Reservation.Status.CHECKED_OUT,
        )
        self._create_reservation(
            check_in=date(2026, 3, 15),
            amount=Decimal("200.00"),
            status=Reservation.Status.CHECKED_IN,
        )
        Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            check_in=date(2026, 4, 1),
            check_out=date(2026, 4, 5),
            status=Reservation.Status.EXPECTED,
            booker_name="Expected only",
            amount=Decimal("999.00"),
        )

        payload = aggregate_monthly_statistics(self.tenant, 2026)

        self.assertEqual(payload["property_label"], "Uzorita")

        self.assertEqual(payload["year"], 2026)
        self.assertEqual(payload["comparison_year"], 2025)
        self.assertEqual(payload["currency"], "EUR")
        self.assertEqual(len(payload["months"]), 12)

        march = next(m for m in payload["months"] if m["month"] == 3)
        self.assertEqual(march["current"]["revenue"], "200.00")
        self.assertEqual(march["current"]["commission"], "10.00")
        self.assertEqual(march["current"]["nights"], 3)
        self.assertEqual(march["previous"]["revenue"], "100.00")

        april = next(m for m in payload["months"] if m["month"] == 4)
        self.assertEqual(april["current"]["revenue"], "0.00")
        self.assertEqual(april["current"]["nights"], 0)
        self.assertEqual(april["current"]["reserved_revenue"], "999.00")
        self.assertEqual(april["current"]["reserved_nights"], 4)
        self.assertEqual(march["current"]["reserved_revenue"], "200.00")
        self.assertEqual(march["current"]["reserved_nights"], 3)

    def test_expected_increases_reserved_not_realized(self):
        self._create_reservation(
            check_in=date(2026, 6, 1),
            amount=Decimal("500.00"),
            status=Reservation.Status.EXPECTED,
            nights=5,
        )
        payload = aggregate_monthly_statistics(self.tenant, 2026)
        june = next(m for m in payload["months"] if m["month"] == 6)
        self.assertEqual(june["current"]["revenue"], "0.00")
        self.assertEqual(june["current"]["nights"], 0)
        self.assertEqual(june["current"]["reserved_revenue"], "500.00")
        self.assertEqual(june["current"]["reserved_nights"], 5)

    def test_canceled_aggregated_for_current_and_comparison_year(self):
        self._create_reservation(
            check_in=date(2026, 5, 12),
            amount=Decimal("400.00"),
            status=Reservation.Status.CANCELED,
            nights=4,
        )
        self._create_reservation(
            check_in=date(2025, 5, 12),
            amount=Decimal("200.00"),
            status=Reservation.Status.CANCELED,
            nights=2,
        )
        payload = aggregate_monthly_statistics(self.tenant, 2026)
        self.assertEqual(payload["prior_year"], 2024)
        may = next(m for m in payload["months"] if m["month"] == 5)
        self.assertEqual(may["current"]["canceled_revenue"], "400.00")
        self.assertEqual(may["current"]["canceled_nights"], 4)
        self.assertEqual(may["current"]["revenue"], "0.00")
        self.assertEqual(may["previous"]["canceled_revenue"], "200.00")
        self.assertEqual(may["previous"]["canceled_nights"], 2)

    def test_no_show_counts_as_realized_revenue(self):
        unit = self._create_unit("R3")
        reservation = self._create_reservation(
            check_in=date(2026, 6, 13),
            amount=Decimal("89.00"),
            status=Reservation.Status.NO_SHOW,
            nights=1,
        )
        self._link_unit(reservation, unit)
        payload = aggregate_monthly_statistics(self.tenant, 2026)
        june = next(m for m in payload["months"] if m["month"] == 6)
        self.assertEqual(june["current"]["revenue"], "89.00")
        self.assertEqual(june["current"]["commission"], "10.00")
        self.assertEqual(june["current"]["nights"], 1)
        self.assertEqual(june["current"]["canceled_revenue"], "0.00")
        self.assertEqual(june["current"]["canceled_nights"], 0)
        self.assertEqual(june["current"]["reserved_revenue"], "89.00")
        self.assertEqual(june["current"]["reserved_commission"], "10.00")
        self.assertEqual(june["current"]["reserved_nights"], 1)
        self.assertEqual(june["current"]["reserved_room_nights"], 1)
        self.assertEqual(june["current"]["occupied_room_nights"], 0)

    def test_prior_year_realized_on_previous_bucket(self):
        self._create_reservation(
            check_in=date(2024, 5, 8),
            amount=Decimal("80.00"),
            status=Reservation.Status.CHECKED_OUT,
            nights=2,
        )
        self._create_reservation(
            check_in=date(2025, 5, 10),
            amount=Decimal("100.00"),
            status=Reservation.Status.CHECKED_OUT,
            nights=3,
        )
        payload = aggregate_monthly_statistics(self.tenant, 2026)
        may = next(m for m in payload["months"] if m["month"] == 5)
        self.assertEqual(may["previous"]["revenue"], "100.00")
        self.assertEqual(may["previous"]["nights"], 3)
        self.assertEqual(may["previous"]["prior_revenue"], "80.00")
        self.assertEqual(may["previous"]["prior_nights"], 2)

    def test_prior_year_override(self):
        MonthlyStatisticsOverride.objects.create(
            tenant=self.tenant,
            year=2024,
            month=6,
            revenue=Decimal("1500.00"),
            nights=20,
        )
        payload = aggregate_monthly_statistics(self.tenant, 2026)
        june = next(m for m in payload["months"] if m["month"] == 6)["previous"]
        self.assertEqual(june["prior_revenue"], "1500.00")
        self.assertEqual(june["prior_nights"], 20)

    def test_override_does_not_touch_reserved(self):
        self._create_reservation(
            check_in=date(2026, 7, 10),
            amount=Decimal("300.00"),
            status=Reservation.Status.EXPECTED,
            nights=2,
        )
        MonthlyStatisticsOverride.objects.create(
            tenant=self.tenant,
            year=2026,
            month=7,
            revenue=Decimal("9999.00"),
            nights=99,
        )
        payload = aggregate_monthly_statistics(self.tenant, 2026)
        july = next(m for m in payload["months"] if m["month"] == 7)
        self.assertEqual(july["current"]["revenue"], "9999.00")
        self.assertEqual(july["current"]["nights"], 99)
        self.assertEqual(july["current"]["reserved_revenue"], "300.00")
        self.assertEqual(july["current"]["reserved_nights"], 2)

    def test_override_for_comparison_year_without_reservations(self):
        MonthlyStatisticsOverride.objects.create(
            tenant=self.tenant,
            year=2025,
            month=5,
            revenue=Decimal("3580.25"),
            nights=45,
            commission=Decimal("120.00"),
        )
        payload = aggregate_monthly_statistics(self.tenant, 2026)
        may_previous = next(m for m in payload["months"] if m["month"] == 5)["previous"]
        self.assertEqual(may_previous["revenue"], "3580.25")
        self.assertEqual(may_previous["commission"], "120.00")
        self.assertEqual(may_previous["nights"], 45)

    def test_override_replaces_reservation_sum_for_same_month(self):
        self._create_reservation(
            check_in=date(2025, 5, 10),
            amount=Decimal("999.00"),
            status=Reservation.Status.CHECKED_OUT,
            nights=2,
        )
        MonthlyStatisticsOverride.objects.create(
            tenant=self.tenant,
            year=2025,
            month=5,
            revenue=Decimal("3580.25"),
            nights=45,
        )
        payload = aggregate_monthly_statistics(self.tenant, 2026)
        may_previous = next(m for m in payload["months"] if m["month"] == 5)["previous"]
        self.assertEqual(may_previous["revenue"], "3580.25")
        self.assertEqual(may_previous["commission"], "0.00")
        self.assertEqual(may_previous["nights"], 45)

    def test_override_isolated_per_tenant(self):
        other = Tenant.objects.create(name="Demo", slug="demo")
        Property.objects.create(tenant=other, name="Demo", slug="demo")
        MonthlyStatisticsOverride.objects.create(
            tenant=other,
            year=2025,
            month=6,
            revenue=Decimal("5000.00"),
            nights=99,
        )
        payload = aggregate_monthly_statistics(self.tenant, 2026)
        june_previous = next(m for m in payload["months"] if m["month"] == 6)["previous"]
        self.assertEqual(june_previous["revenue"], "0.00")
        self.assertEqual(june_previous["nights"], 0)

    def test_occupancy_single_unit_realized(self):
        unit = self._create_unit("R1")
        reservation = self._create_reservation(
            check_in=date(2026, 6, 10),
            amount=Decimal("100.00"),
            status=Reservation.Status.CHECKED_OUT,
            nights=3,
        )
        self._link_unit(reservation, unit)

        payload = aggregate_monthly_statistics(self.tenant, 2026)
        self.assertEqual(payload["active_units"], 1)
        june = next(m for m in payload["months"] if m["month"] == 6)["current"]
        self.assertEqual(june["capacity_room_nights"], 30)
        self.assertEqual(june["occupied_room_nights"], 3)
        self.assertEqual(june["reserved_room_nights"], 3)
        self.assertEqual(june["occupancy_realized_pct"], "10.0")

    def test_occupancy_multi_room_reservation(self):
        u1 = self._create_unit("R1")
        u2 = self._create_unit("R2")
        reservation = self._create_reservation(
            check_in=date(2026, 6, 1),
            amount=Decimal("200.00"),
            status=Reservation.Status.CHECKED_IN,
            nights=3,
            units_count=2,
        )
        self._link_unit(reservation, u1)
        ReservationUnit.objects.create(
            tenant=self.tenant,
            reservation=reservation,
            unit=u2,
            sort_order=1,
            room_name=u2.name,
        )

        payload = aggregate_monthly_statistics(self.tenant, 2026)
        june = next(m for m in payload["months"] if m["month"] == 6)["current"]
        self.assertEqual(june["capacity_room_nights"], 60)
        self.assertEqual(june["occupied_room_nights"], 6)
        self.assertEqual(june["reserved_room_nights"], 6)

    def test_occupancy_cross_month_stay(self):
        unit = self._create_unit("R1")
        reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            check_in=date(2026, 6, 28),
            check_out=date(2026, 7, 3),
            status=Reservation.Status.CHECKED_OUT,
            booker_name="Cross month",
            amount=Decimal("100.00"),
            nights_count=5,
            currency="EUR",
        )
        self._link_unit(reservation, unit)

        payload = aggregate_monthly_statistics(self.tenant, 2026)
        june = next(m for m in payload["months"] if m["month"] == 6)["current"]
        july = next(m for m in payload["months"] if m["month"] == 7)["current"]
        # Jun 28–Jul 3 → nights 28,29,30 (June) and 1,2 (July)
        self.assertEqual(june["occupied_room_nights"], 3)
        self.assertEqual(july["occupied_room_nights"], 2)

    def test_occupancy_expected_increases_reserved_not_occupied(self):
        unit = self._create_unit("R1")
        reservation = self._create_reservation(
            check_in=date(2026, 6, 1),
            amount=Decimal("500.00"),
            status=Reservation.Status.EXPECTED,
            nights=5,
        )
        self._link_unit(reservation, unit)

        payload = aggregate_monthly_statistics(self.tenant, 2026)
        june = next(m for m in payload["months"] if m["month"] == 6)["current"]
        self.assertEqual(june["occupied_room_nights"], 0)
        self.assertEqual(june["reserved_room_nights"], 5)

    def test_occupancy_override_does_not_change_occupancy(self):
        unit = self._create_unit("R1")
        reservation = self._create_reservation(
            check_in=date(2026, 7, 10),
            amount=Decimal("300.00"),
            status=Reservation.Status.CHECKED_IN,
            nights=2,
        )
        self._link_unit(reservation, unit)
        MonthlyStatisticsOverride.objects.create(
            tenant=self.tenant,
            year=2026,
            month=7,
            revenue=Decimal("9999.00"),
            nights=99,
        )

        payload = aggregate_monthly_statistics(self.tenant, 2026)
        july = next(m for m in payload["months"] if m["month"] == 7)["current"]
        self.assertEqual(july["revenue"], "9999.00")
        self.assertEqual(july["nights"], 99)
        self.assertEqual(july["occupied_room_nights"], 2)
        self.assertEqual(july["reserved_room_nights"], 2)
