import json

from django.test import SimpleTestCase

from apps.core.countries import countries_for_select, countries_json_path


class CountriesParityTests(SimpleTestCase):
    def test_backend_matches_shared_json(self):
        path = countries_json_path()
        self.assertTrue(path.is_file(), f"missing catalog: {path}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        json_pairs = {(row["iso2"], row["iso3"]) for row in raw}
        backend_pairs = {(row["iso2"], row["iso3"]) for row in countries_for_select()}
        self.assertEqual(json_pairs, backend_pairs)

    def test_frontend_catalog_references_shared_json(self):
        repo_root = countries_json_path().parents[1]
        frontend_ts = repo_root / "web" / "booking" / "lib" / "countries.ts"
        if not frontend_ts.is_file():
            self.skipTest("frontend sources not mounted in container image")
        content = frontend_ts.read_text(encoding="utf-8")
        self.assertIn("iso3166-countries.json", content)
