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

    def test_strip_opcina_label_comma_form(self):
        result = validate_evisitor_residence_address("Općina Vodice, Ulica 1")
        self.assertTrue(result.valid)
        self.assertEqual(result.city, "Vodice")
        self.assertEqual(result.normalized_address, "Vodice, Ulica 1")


class EvisitorIdCardAddressTests(SimpleTestCase):
    """Croatian ID card form: naselje, Grad/Općina X, ulica broj (#1159)."""

    def test_regression_1159_settlement_differs_from_city(self):
        result = validate_evisitor_residence_address(
            "SESVETE, GRAD ZAGREB, ULICA KRSTE HEGEDUSICA 13 M"
        )
        self.assertTrue(result.valid)
        self.assertEqual(result.city, "ZAGREB")
        self.assertEqual(
            result.normalized_address,
            "SESVETE, GRAD ZAGREB, ULICA KRSTE HEGEDUSICA 13 M",
        )
        self.assertTrue(result.warnings)

    def test_normalized_address_revalidates_to_same_city(self):
        """OCR apply and sync_guest_evisitor_fields persist normalized_address."""
        raw = "SESVETE, GRAD ZAGREB, ULICA KRSTE HEGEDUSICA 13 M"
        first = validate_evisitor_residence_address(raw)
        second = validate_evisitor_residence_address(first.normalized_address)
        self.assertTrue(second.valid)
        self.assertEqual(second.city, first.city)
        self.assertEqual(second.normalized_address, first.normalized_address)

    def test_settlement_equal_to_city_unchanged(self):
        result = validate_evisitor_residence_address(
            "ZAGREB, GRAD ZAGREB, OPOROVEČKI VINOGRADI 66 A"
        )
        self.assertTrue(result.valid)
        self.assertEqual(result.city, "ZAGREB")

    def test_two_segment_settlement_and_city(self):
        result = validate_evisitor_residence_address("Zagreb, Grad Zagreb")
        self.assertTrue(result.valid)
        self.assertEqual(result.city, "Zagreb")
        self.assertEqual(result.normalized_address, "Zagreb, Grad Zagreb")

    def test_opcina_segment(self):
        result = validate_evisitor_residence_address(
            "Privlaka, Općina Privlaka, Ulica 5"
        )
        self.assertTrue(result.valid)
        self.assertEqual(result.city, "Privlaka")

    def test_postal_prefix_stripped_before_jls_lookup(self):
        result = validate_evisitor_residence_address(
            "10360 Sesvete, Grad Zagreb, Ulica 13"
        )
        self.assertTrue(result.valid)
        self.assertEqual(result.city, "Zagreb")
        self.assertEqual(result.normalized_address, "Sesvete, Grad Zagreb, Ulica 13")

    def test_second_segment_without_label_keeps_first_segment(self):
        result = validate_evisitor_residence_address("Split, Hrvatska, Ulica 5")
        self.assertTrue(result.valid)
        self.assertEqual(result.city, "Split")

    def test_street_first_still_rejected(self):
        result = validate_evisitor_residence_address(
            "Ulica Krste Hegedušića 13, Grad Zagreb"
        )
        self.assertFalse(result.valid)
        self.assertEqual(result.city, "")
        self.assertEqual(result.normalized_address, "")
        self.assertIn(MSG_STREET_FIRST, result.errors)
