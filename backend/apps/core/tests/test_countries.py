from django.test import SimpleTestCase

from apps.core.countries import (
    countries_for_select,
    iso2_to_iso3,
    iso3_to_iso2,
    is_known_iso2,
    is_known_iso3,
)


class CountriesCatalogTests(SimpleTestCase):
    def test_iso2_to_iso3_indonesia(self):
        self.assertEqual(iso2_to_iso3("ID"), "IDN")

    def test_iso2_to_iso3_romania_without_network(self):
        self.assertEqual(iso2_to_iso3("RO"), "ROU")

    def test_iso3_to_iso2_indonesia(self):
        self.assertEqual(iso3_to_iso2("IDN"), "ID")

    def test_is_known_iso2(self):
        self.assertTrue(is_known_iso2("ID"))
        self.assertFalse(is_known_iso2("XX"))

    def test_is_known_iso3(self):
        self.assertTrue(is_known_iso3("IDN"))
        self.assertFalse(is_known_iso3("XXK"))

    def test_catalog_has_expected_size(self):
        countries = countries_for_select()
        self.assertGreaterEqual(len(countries), 240)
        iso2_codes = {row["iso2"] for row in countries}
        self.assertEqual(len(iso2_codes), len(countries))
