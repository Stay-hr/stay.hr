from decimal import Decimal

from django.test import TestCase

from apps.integrations.channex.config import ChannexRuntimeConfig
from apps.integrations.channex.mapping import (
    UZORITA_PRODUCTION_ROOM_TYPES,
    UZORITA_STAGING_ROOM_TYPES,
    channex_push_rate_for_unit,
)


class ChannexMappingTests(TestCase):
    def test_uzorita_has_four_physical_rooms(self):
        self.assertEqual(len(UZORITA_STAGING_ROOM_TYPES), 4)
        codes = {row["unit_code"] for row in UZORITA_STAGING_ROOM_TYPES}
        self.assertEqual(codes, {"R1", "R2", "R6", "R3"})

    def test_runtime_config_lookup_by_unit_code(self):
        config = ChannexRuntimeConfig.from_integration_dict(
            {
                "environment": "staging",
                "property_id": "prop-uuid",
                "api_key": "key",
                "room_types": list(UZORITA_STAGING_ROOM_TYPES),
            }
        )
        self.assertEqual(
            config.room_type_id_for_unit_code("R1"),
            "e8fc8060-3df5-4e49-bee9-32903786b4ee",
        )
        self.assertEqual(
            config.unit_code_for_room_type_id("6058e4da-0ed4-48a1-a877-fec38685589a"),
            "R3",
        )

    def test_uzorita_production_includes_r4(self):
        codes = {row["unit_code"] for row in UZORITA_PRODUCTION_ROOM_TYPES}
        self.assertIn("R4", codes)
        r4 = next(row for row in UZORITA_PRODUCTION_ROOM_TYPES if row["unit_code"] == "R4")
        self.assertEqual(r4["channex_title"], "Standard King Room R4")

    def test_channex_push_rate_reduction_model(self):
        self.assertEqual(channex_push_rate_for_unit("R3", Decimal("147.00")), Decimal("157.00"))
        self.assertEqual(channex_push_rate_for_unit("R6", Decimal("147.00")), Decimal("157.00"))
        self.assertEqual(channex_push_rate_for_unit("R1", Decimal("112.81")), Decimal("117.81"))
        self.assertEqual(channex_push_rate_for_unit("R4", Decimal("65.00")), Decimal("70.00"))
