from django.test import TestCase

from apps.properties.models import Property, Unit
from apps.reservations.unit_mapping import resolve_unit, unit_code_from_room_name
from apps.tenants.models import Tenant


class UnitCodeFromRoomNameTests(TestCase):
    def test_deluxe_triple_room_maps_to_r3(self):
        self.assertEqual(unit_code_from_room_name("Deluxe Triple Room"), "R3")

    def test_deluxe_king_room_maps_to_r1(self):
        self.assertEqual(unit_code_from_room_name("Deluxe King Room"), "R1")

    def test_standard_king_room_maps_to_r4(self):
        self.assertEqual(unit_code_from_room_name("Standard King Room"), "R4")
        self.assertEqual(unit_code_from_room_name("Standard King Room R4"), "R4")

    def test_explicit_room_code_takes_precedence(self):
        self.assertEqual(unit_code_from_room_name("Deluxe Triple Room R1"), "R1")


class ResolveUnitTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Uzorita", slug="uzorita-mapping")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Uzorita",
            slug="uzorita",
        )
        self.r1 = Unit.objects.create(
            tenant=self.tenant,
            property=self.property,
            code="R1",
            name="R1",
        )
        self.r3 = Unit.objects.create(
            tenant=self.tenant,
            property=self.property,
            code="R3",
            name="R3",
        )

    def test_resolve_deluxe_triple_room(self):
        unit = resolve_unit(
            tenant=self.tenant,
            property=self.property,
            room_name="Deluxe Triple Room",
        )
        self.assertEqual(unit, self.r3)

    def test_resolve_deluxe_king_room(self):
        unit = resolve_unit(
            tenant=self.tenant,
            property=self.property,
            room_name="Deluxe King Room",
        )
        self.assertEqual(unit, self.r1)
