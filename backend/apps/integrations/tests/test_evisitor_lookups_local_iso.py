from django.test import SimpleTestCase

from apps.integrations.evisitor.lookups import iso2_to_iso3


class EvisitorLookupsLocalIsoTests(SimpleTestCase):
    def test_iso2_to_iso3_uses_local_catalog_first(self):
        self.assertEqual(iso2_to_iso3("ID"), "IDN")
        self.assertEqual(iso2_to_iso3("RO"), "ROU")

    def test_iso2_to_iso3_unknown_returns_empty(self):
        self.assertEqual(iso2_to_iso3("XX"), "")

    def test_evisitor_specific_iso3_passthrough(self):
        self.assertEqual(iso2_to_iso3("XXK"), "XXK")
