"""Specification tests for document_expectations policy layer."""

from __future__ import annotations

from datetime import date

from django.test import TestCase

from apps.properties.models import Property
from apps.reservations.document_expectations import (
    expected_document_count,
    expected_document_slots,
    missing_document_slots,
)
from apps.reservations.models import Guest, Reservation
from apps.reservations.tests.fixtures.document_intake.load_fixture import (
    build_reservation_from_fixture,
    load_document_intake_fixture,
)
from apps.tenants.models import Tenant


def _make_reservation(
    *,
    tenant: Tenant,
    property: Property,
    adults_count: int | None = 1,
    children_count: int = 0,
    persons_count: int | None = None,
    guest_specs: list[dict] | None = None,
) -> Reservation:
    reservation = Reservation.objects.create(
        tenant=tenant,
        property=property,
        booker_name="Test Booker",
        check_in=date(2026, 7, 1),
        check_out=date(2026, 7, 5),
        status=Reservation.Status.EXPECTED,
        adults_count=adults_count,
        children_count=children_count,
        persons_count=persons_count,
    )
    for spec in guest_specs or []:
        Guest.objects.create(
            tenant=tenant,
            reservation=reservation,
            first_name=spec.get("first_name", "Novi"),
            last_name=spec.get("last_name", "gost"),
            name=spec.get("name", "Novi gost"),
            is_primary=bool(spec.get("is_primary")),
        )
    return reservation


class PolicyInvariantsTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="policy-inv", name="Policy Inv")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Hotel",
            slug="hotel-policy-inv",
        )

    def test_count_is_sole_source_for_slot_cap(self):
        reservation = _make_reservation(
            tenant=self.tenant,
            property=self.property,
            adults_count=4,
            children_count=4,
            persons_count=8,
            guest_specs=[{"is_primary": i == 0} for i in range(8)],
        )
        self.assertEqual(expected_document_count(reservation), 4)
        self.assertEqual(len(expected_document_slots(reservation)), 4)

    def test_expected_checkin_adults_overrides_ota_adults(self):
        reservation = _make_reservation(
            tenant=self.tenant,
            property=self.property,
            adults_count=2,
            persons_count=2,
            guest_specs=[{"is_primary": True}, {"is_primary": False}],
        )
        self.assertEqual(expected_document_count(reservation), 2)
        reservation.expected_checkin_adults = 1
        reservation.save(update_fields=["expected_checkin_adults"])
        self.assertEqual(expected_document_count(reservation), 1)
        reservation.expected_checkin_adults = None
        reservation.save(update_fields=["expected_checkin_adults"])
        self.assertEqual(expected_document_count(reservation), 2)

    def test_hierarchy_missing_never_exceeds_slots(self):
        reservation = _make_reservation(
            tenant=self.tenant,
            property=self.property,
            adults_count=2,
            guest_specs=[
                {"first_name": "A", "last_name": "One", "name": "A One", "is_primary": True},
                {"first_name": "B", "last_name": "Two", "name": "B Two"},
            ],
        )
        missing = missing_document_slots(
            reservation,
            persons=[{"given_names": "A", "surnames": "One"}],
            matches=[
                {
                    "person_index": 0,
                    "auto_apply": True,
                    "guest_id": reservation.guests.order_by("pk").first().pk,
                }
            ],
            images=[],
        )
        self.assertEqual(len(missing), 1)
        self.assertLessEqual(len(missing), len(expected_document_slots(reservation)))


class BusinessScenariosTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="policy-biz", name="Policy Biz")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Uzorita",
            slug="uzorita-policy",
        )

    def test_978_expects_four_adult_documents(self):
        reservation, _guests, ocr_data, _meta = build_reservation_from_fixture(
            tenant=self.tenant,
            property=self.property,
            scenario="978",
        )
        self.assertEqual(reservation.adults_count, 4)
        self.assertEqual(expected_document_count(reservation), 4)
        slots = expected_document_slots(reservation)
        self.assertEqual(len(slots), 4)
        self.assertTrue(slots[0].is_primary)

        matches = [
            {
                "person_index": i,
                "auto_apply": True,
                "guest_id": slot.pk,
                "guest_name": ocr_data["persons"][i].get("given_names", ""),
            }
            for i, slot in enumerate(slots)
        ]
        self.assertEqual(
            missing_document_slots(
                reservation,
                persons=ocr_data["persons"],
                matches=matches,
                images=ocr_data.get("images") or [],
            ),
            [],
        )

    def test_horvat_expects_two_adult_slots_not_child(self):
        reservation, _guests, _ocr, _meta = build_reservation_from_fixture(
            tenant=self.tenant,
            property=self.property,
            scenario="horvat",
        )
        self.assertEqual(expected_document_count(reservation), 2)
        slots = expected_document_slots(reservation)
        self.assertEqual(len(slots), 2)
        slot_ids = {guest.pk for guest in slots}
        ivan = reservation.guests.get(is_primary=True)
        marko = reservation.guests.get(first_name="Marko")
        self.assertIn(ivan.pk, slot_ids)
        self.assertNotIn(marko.pk, slot_ids)

    def test_zero_adults_expects_no_documents(self):
        reservation = _make_reservation(
            tenant=self.tenant,
            property=self.property,
            adults_count=0,
            persons_count=2,
            guest_specs=[
                {"first_name": "Child", "last_name": "One", "name": "Child One", "is_primary": True},
                {"first_name": "Child", "last_name": "Two", "name": "Child Two"},
            ],
        )
        self.assertEqual(expected_document_count(reservation), 0)
        self.assertEqual(expected_document_slots(reservation), [])
        self.assertEqual(
            missing_document_slots(
                reservation,
                persons=[],
                matches=[],
                images=[],
            ),
            [],
        )

    def test_adults_equals_persons(self):
        reservation = _make_reservation(
            tenant=self.tenant,
            property=self.property,
            adults_count=2,
            children_count=0,
            persons_count=2,
            guest_specs=[
                {"first_name": "A", "last_name": "A", "name": "A A", "is_primary": True},
                {"first_name": "B", "last_name": "B", "name": "B B"},
            ],
        )
        self.assertEqual(expected_document_count(reservation), 2)
        self.assertEqual(len(expected_document_slots(reservation)), 2)

    def test_adults_less_than_persons(self):
        reservation = _make_reservation(
            tenant=self.tenant,
            property=self.property,
            adults_count=2,
            children_count=2,
            persons_count=4,
            guest_specs=[
                {"first_name": "Adult", "last_name": "One", "name": "Adult One", "is_primary": True},
                {"first_name": "Adult", "last_name": "Two", "name": "Adult Two"},
                {"first_name": "Child", "last_name": "One", "name": "Child One"},
                {"first_name": "Child", "last_name": "Two", "name": "Child Two"},
            ],
        )
        self.assertEqual(expected_document_count(reservation), 2)
        self.assertEqual(len(expected_document_slots(reservation)), 2)


class ApiInvariantsTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="policy-api", name="Policy API")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Hotel",
            slug="hotel-policy-api",
        )

    def _reservations_for_parametrize(self) -> list[Reservation]:
        fixtures: list[Reservation] = []

        r978, _, _, _ = build_reservation_from_fixture(
            tenant=self.tenant,
            property=self.property,
            scenario="978",
        )
        fixtures.append(r978)

        horvat_tenant = Tenant.objects.create(slug="policy-api-h", name="H")
        horvat_property = Property.objects.create(
            tenant=horvat_tenant,
            name="H",
            slug="h",
        )
        r_horvat, _, _, _ = build_reservation_from_fixture(
            tenant=horvat_tenant,
            property=horvat_property,
            scenario="horvat",
        )
        fixtures.append(r_horvat)

        fixtures.append(
            _make_reservation(
                tenant=self.tenant,
                property=self.property,
                adults_count=0,
                persons_count=1,
                guest_specs=[{"first_name": "Only", "last_name": "Guest", "name": "Only Guest", "is_primary": True}],
            )
        )
        fixtures.append(
            _make_reservation(
                tenant=self.tenant,
                property=self.property,
                adults_count=3,
                persons_count=3,
                guest_specs=[{"is_primary": i == 0} for i in range(3)],
            )
        )
        fixtures.append(
            _make_reservation(
                tenant=self.tenant,
                property=self.property,
                adults_count=2,
                persons_count=5,
                guest_specs=[{"is_primary": i == 0} for i in range(5)],
            )
        )
        return fixtures

    def test_slot_count_matches_expected_count(self):
        for reservation in self._reservations_for_parametrize():
            with self.subTest(reservation_id=reservation.pk, adults=reservation.adults_count):
                self.assertEqual(
                    len(expected_document_slots(reservation)),
                    expected_document_count(reservation),
                )

    def test_missing_never_exceeds_slot_count(self):
        for reservation in self._reservations_for_parametrize():
            ocr = {"persons": [], "images": []}
            if reservation.external_id == "fixture-978":
                ocr = load_document_intake_fixture("978")["ocr"]
            with self.subTest(reservation_id=reservation.pk):
                missing = missing_document_slots(
                    reservation,
                    persons=ocr["persons"],
                    matches=[],
                    images=ocr.get("images") or [],
                )
                self.assertLessEqual(
                    len(missing),
                    len(expected_document_slots(reservation)),
                )

    def test_expected_document_slots_is_deterministic(self):
        for reservation in self._reservations_for_parametrize():
            with self.subTest(reservation_id=reservation.pk):
                slots1 = expected_document_slots(reservation)
                slots2 = expected_document_slots(reservation)
                self.assertEqual(
                    [guest.pk for guest in slots1],
                    [guest.pk for guest in slots2],
                )
