from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from apps.properties.models import BedType, Property, Unit, UnitBed
from apps.tenants.models import Tenant


class SeedUzoritaUnitBedsTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="uzorita", name="Uzorita")
        self.property = Property.objects.create(
            tenant=self.tenant,
            slug="uzorita",
            name="Uzorita",
        )
        self.units = {}
        for code in ("R1", "R2", "R3", "R4", "R6"):
            self.units[code] = Unit.objects.create(
                tenant=self.tenant,
                property=self.property,
                code=code,
                name=f"Room {code}",
            )

    def _run_seed(self):
        out = StringIO()
        call_command(
            "seed_uzorita_unit_beds",
            tenant_slug=self.tenant.slug,
            stdout=out,
        )
        return out.getvalue()

    def test_seed_creates_queen_sofa_for_standard_units(self):
        self._run_seed()
        for code in ("R1", "R2", "R3", "R6"):
            beds = list(
                UnitBed.objects.filter(unit=self.units[code]).order_by("sort_order")
            )
            self.assertEqual(len(beds), 2)
            self.assertEqual(beds[0].bed_type, BedType.QUEEN)
            self.assertEqual(beds[0].count, 1)
            self.assertEqual(beds[1].bed_type, BedType.SOFA)
            self.assertEqual(beds[1].count, 1)

    def test_seed_creates_king_only_for_r4(self):
        self._run_seed()
        beds = list(
            UnitBed.objects.filter(unit=self.units["R4"]).order_by("sort_order")
        )
        self.assertEqual(len(beds), 1)
        self.assertEqual(beds[0].bed_type, BedType.KING)
        self.assertEqual(beds[0].count, 1)

    def test_seed_is_idempotent(self):
        self._run_seed()
        self._run_seed()
        self.assertEqual(UnitBed.objects.filter(tenant=self.tenant).count(), 9)

    def test_seed_skips_missing_unit(self):
        self.units["R3"].delete()
        output = self._run_seed()
        self.assertIn("SKIP R3", output)
        self.assertEqual(UnitBed.objects.filter(tenant=self.tenant).count(), 7)
