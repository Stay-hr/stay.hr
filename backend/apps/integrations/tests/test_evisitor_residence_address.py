"""Tests for eVisitor residence address validation (CityOfResidence)."""

from django.test import SimpleTestCase

from apps.integrations.evisitor.residence_address import (
    MSG_CANNOT_DETERMINE,
    MSG_STREET_FIRST,
    validate_evisitor_residence_address,
)


class EvisitorResidenceAddressTests(SimpleTestCase):
    def test_canonical_city_street_ok(self):
        result = validate_evisitor_residence_address("Osijek, Dubrovačka 30")
        self.assertTrue(result.valid)
        self.assertEqual(result.city, "Osijek")
        self.assertEqual(result.normalized_address, "Osijek, Dubrovačka 30")
        self.assertEqual(result.errors, ())

    def test_stari_grad_canonical_ok(self):
        result = validate_evisitor_residence_address(
            "Stari Grad, Petra Krešimira IV 3"
        )
        self.assertTrue(result.valid)
        self.assertEqual(result.city, "Stari Grad")
        self.assertEqual(
            result.normalized_address,
            "Stari Grad, Petra Krešimira IV 3",
        )

    def test_postal_prefix_stripped_on_city_segment(self):
        result = validate_evisitor_residence_address("21000 Split, Obala 1")
        self.assertTrue(result.valid)
        self.assertEqual(result.city, "Split")
        self.assertEqual(result.normalized_address, "Split, Obala 1")

    def test_no_comma_city_plus_house_ok_with_warning(self):
        result = validate_evisitor_residence_address("DONJI BITELIĆ 208 A")
        self.assertTrue(result.valid)
        self.assertEqual(result.city, "DONJI BITELIĆ")
        self.assertEqual(result.normalized_address, "DONJI BITELIĆ, 208 A")
        self.assertTrue(result.warnings)

    def test_no_comma_simple_city_house_ok(self):
        result = validate_evisitor_residence_address("Osijek 30")
        self.assertTrue(result.valid)
        self.assertEqual(result.city, "Osijek")
        self.assertEqual(result.normalized_address, "Osijek, 30")

    def test_digit_in_city_segment_with_comma_fails(self):
        result = validate_evisitor_residence_address("Osijek 30, Dubrovačka")
        self.assertFalse(result.valid)
        self.assertEqual(result.city, "")
        self.assertEqual(result.normalized_address, "")
        self.assertIn(MSG_STREET_FIRST, result.errors)

    def test_ambiguous_no_comma_street_fails(self):
        result = validate_evisitor_residence_address("Osijek Dubrovačka 30")
        self.assertFalse(result.valid)
        self.assertEqual(result.normalized_address, "")
        self.assertIn(MSG_CANNOT_DETERMINE, result.errors)

    def test_empty_fails(self):
        result = validate_evisitor_residence_address("  ")
        self.assertFalse(result.valid)
        self.assertEqual(result.normalized_address, "")

    def test_invalid_result_never_half_normalized(self):
        result = validate_evisitor_residence_address("Ulica 1, Zagreb")
        self.assertFalse(result.valid)
        self.assertEqual(result.city, "")
        self.assertEqual(result.normalized_address, "")

    def test_regression_190_street_first(self):
        """Incident #190: street before city must not reach eVisitor."""
        result = validate_evisitor_residence_address("Dubrovačka 30, Osijek")
        self.assertFalse(result.valid)
        self.assertEqual(result.city, "")
        self.assertEqual(result.normalized_address, "")
        self.assertIn(MSG_STREET_FIRST, result.errors)

    def test_regression_886_ambiguous_blob(self):
        """Incident #886: multi-settlement OCR blob cannot determine city."""
        result = validate_evisitor_residence_address(
            "DONJI BITELIĆ HRVACE DONJI BITELIĆ 208 A"
        )
        self.assertFalse(result.valid)
        self.assertEqual(result.city, "")
        self.assertEqual(result.normalized_address, "")
        self.assertIn(MSG_CANNOT_DETERMINE, result.errors)

    def test_regression_886_case_variants(self):
        variants = (
            "donji bitelić hrvace donji bitelić 208 a",
            "DONJI BITELIĆ HRVACE DONJI BITELIĆ 208 A",
            "Donji Bitelić Hrvace Donji Bitelić 208 A",
        )
        for raw in variants:
            with self.subTest(raw=raw):
                result = validate_evisitor_residence_address(raw)
                self.assertFalse(result.valid)
                self.assertEqual(result.normalized_address, "")
                self.assertIn(MSG_CANNOT_DETERMINE, result.errors)

    def test_street_prefix_first_fails(self):
        result = validate_evisitor_residence_address("Ulica Petra Krešimira, Zagreb")
        self.assertFalse(result.valid)
        self.assertIn(MSG_STREET_FIRST, result.errors)

    def test_city_too_many_words_comma_fails(self):
        long_city = "Jedan Dva Tri Četiri Pet Šest"
        result = validate_evisitor_residence_address(f"{long_city}, Ulica 1")
        self.assertFalse(result.valid)
        self.assertEqual(result.normalized_address, "")

    def test_nova_gradiska_no_comma_ok(self):
        """Place names ending in ova/ška must not be treated as street tokens."""
        result = validate_evisitor_residence_address("Nova Gradiška 15")
        self.assertTrue(result.valid)
        self.assertEqual(result.city, "Nova Gradiška")
        self.assertEqual(result.normalized_address, "Nova Gradiška, 15")

    def test_strip_grad_label_comma_form(self):
        result = validate_evisitor_residence_address("Grad Zagreb, Ulica 1")
        self.assertTrue(result.valid)
        self.assertEqual(result.city, "Zagreb")
        self.assertEqual(result.normalized_address, "Zagreb, Ulica 1")

    def test_stari_grad_not_stripped_as_label(self):
        result = validate_evisitor_residence_address("Stari Grad, Ulica 1")
        self.assertTrue(result.valid)
        self.assertEqual(result.city, "Stari Grad")
        self.assertEqual(result.normalized_address, "Stari Grad, Ulica 1")

    def test_strip_grad_label_no_comma_form(self):
        result = validate_evisitor_residence_address("Grad Zagreb 12")
        self.assertTrue(result.valid)
        self.assertEqual(result.city, "Zagreb")
        self.assertEqual(result.normalized_address, "Zagreb, 12")
