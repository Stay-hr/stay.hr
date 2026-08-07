"""Unit tests for OCR residence address normalization (#1034 / #190)."""

from __future__ import annotations

from django.test import SimpleTestCase, override_settings

from apps.integrations.evisitor.residence_address import validate_evisitor_residence_address
from apps.reservations.address_normalizer import normalize_address
from apps.reservations.document_intake_service import _guest_updates_from_payload
from apps.reservations.guest_checkin_ocr import person_to_guest_preview


class AddressNormalizerTests(SimpleTestCase):
    def test_fr_postal_city_with_country(self):
        raw = "4 CHEMIN DE LINDIENNIERE, 69450 SAINT-CYR-AU-MONT-D'OR, FRANCE"
        result = normalize_address(raw, source="ocr")
        self.assertTrue(result.applied)
        self.assertTrue(result.success)
        self.assertEqual(result.strategy, "fr_postal_city")
        self.assertEqual(result.original, raw)
        self.assertEqual(
            result.normalized,
            "SAINT-CYR-AU-MONT-D'OR, 4 CHEMIN DE LINDIENNIERE",
        )
        self.assertTrue(validate_evisitor_residence_address(result.normalized).valid)

    def test_fr_postal_city_dijon(self):
        raw = "6 A BD CARNOT, 21000 DIJON"
        result = normalize_address(raw, source="ocr")
        self.assertTrue(result.applied)
        self.assertEqual(result.normalized, "DIJON, 6 A BD CARNOT")
        self.assertTrue(validate_evisitor_residence_address(result.normalized).valid)

    def test_fr_postal_city_dash(self):
        raw = "10 chemin de Braizieux - 69450 Saint Cyr au Mont d'or"
        result = normalize_address(raw, source="ocr")
        self.assertTrue(result.applied)
        self.assertEqual(result.strategy, "fr_postal_city_dash")
        self.assertEqual(
            result.normalized,
            "Saint Cyr au Mont d'or, 10 chemin de Braizieux",
        )

    def test_idempotent_already_city_first(self):
        raw = "DIJON, 6 A BD CARNOT"
        result = normalize_address(raw, source="ocr")
        self.assertFalse(result.applied)
        self.assertIsNone(result.normalized)
        self.assertEqual(result.strategy, "")

    def test_hr_city_first_unchanged(self):
        raw = "Osijek, Dubrovačka 30"
        result = normalize_address(raw, source="ocr")
        self.assertFalse(result.applied)
        self.assertTrue(validate_evisitor_residence_address(raw).valid)

    def test_hr_street_first_no_blind_swap(self):
        """#190 policy: without postal, do not auto-swap street-first."""
        raw = "Dubrovačka 30, Osijek"
        result = normalize_address(raw, source="ocr")
        self.assertFalse(result.applied)
        self.assertIsNone(result.normalized)
        self.assertFalse(validate_evisitor_residence_address(raw).valid)

    def test_zagreb_unchanged(self):
        raw = "Zagreb, Ilica 15"
        result = normalize_address(raw, source="ocr")
        self.assertFalse(result.applied)

    @override_settings(OCR_ADDRESS_NORMALIZATION_ENABLED=False)
    def test_feature_flag_off(self):
        raw = "6 A BD CARNOT, 21000 DIJON"
        result = normalize_address(raw, source="ocr")
        self.assertFalse(result.applied)

    def test_non_ocr_source_noop(self):
        raw = "6 A BD CARNOT, 21000 DIJON"
        result = normalize_address(raw, source="mrz")
        self.assertFalse(result.applied)

    def test_does_not_invent_content(self):
        """Candidate must be a reorder of existing segments only."""
        raw = "4 CHEMIN DE LINDIENNIERE, 69450 SAINT-CYR-AU-MONT-D'OR, FRANCE"
        result = normalize_address(raw, source="ocr")
        self.assertIn("4 CHEMIN DE LINDIENNIERE", result.normalized)
        self.assertIn("SAINT-CYR-AU-MONT-D'OR", result.normalized)
        self.assertNotIn("FRANCE", result.normalized)
        self.assertNotIn("69450", result.normalized)


class AddressNormalizerApplyPathTests(SimpleTestCase):
    def test_guest_updates_persists_normalized_and_keeps_nationality(self):
        updates, suggested = _guest_updates_from_payload(
            {
                "podaci_gosta": {
                    "ime": "Anwar",
                    "prezime": "BOT",
                    "drzavljanstvo": "FRA",
                    "adresa": (
                        "4 CHEMIN DE LINDIENNIERE, 69450 "
                        "SAINT-CYR-AU-MONT-D'OR, FRANCE"
                    ),
                },
                "metapodaci": {"tip_dokumenta": "national_id"},
            }
        )
        self.assertEqual(updates.get("nationality"), "FR")
        self.assertEqual(
            updates.get("address"),
            "SAINT-CYR-AU-MONT-D'OR, 4 CHEMIN DE LINDIENNIERE",
        )
        self.assertEqual(
            suggested.get("ocr_address_original"),
            "4 CHEMIN DE LINDIENNIERE, 69450 SAINT-CYR-AU-MONT-D'OR, FRANCE",
        )
        self.assertEqual(suggested.get("ocr_address_strategy"), "fr_postal_city")

    def test_preview_normalizes_fr_address(self):
        preview = person_to_guest_preview(
            {
                "given_names": "NOUR",
                "surnames": "EL HOUDA",
                "nationality": "FRA",
                "address": "6 A BD CARNOT, 21000 DIJON",
            }
        )
        self.assertEqual(preview["nationality"], "FR")
        self.assertEqual(preview["address"], "DIJON, 6 A BD CARNOT")
        self.assertEqual(
            preview["ocr_address_original"],
            "6 A BD CARNOT, 21000 DIJON",
        )
