from django.test import TestCase

from apps.reservations.document_intake_ocr_fixup import (
    fixup_document_ocr_result,
    infer_image_side,
    normalize_document_number,
    _surname_from_german_id_front_ocr,
    _surname_from_mrz_lines,
)
from apps.reservations.document_intake_match import _person_name_keys
from apps.reservations.booking_xls_import import _normalize_guest_name_key


class DocumentIntakeOcrFixupTests(TestCase):
    def test_infer_back_from_pesel_and_mrz(self):
        item = {
            "side": "front",
            "ocr_text": "NUMER PESEL/PERSONAL NUMBER\n83060109166\nORGAN WYDAJACY",
            "mrz_lines": ["IPOLDGN7672550<<<<<<<<<<<<<<<"],
        }
        self.assertEqual(infer_image_side(item), "back")

    def test_fixup_moves_misclassified_front_index_to_back(self):
        ocr = {
            "images": [
                {
                    "index": 0,
                    "side": "front",
                    "ocr_text": "NUMER PESEL/PERSONAL NUMBER",
                    "mrz_lines": ["IPOLDGN7672550<<<<<<<<<<<<<<<"],
                }
            ],
            "persons": [
                {
                    "given_names": "JOANNA",
                    "surnames": "GOLEBIOWSKA",
                    "document_number": "DGN767255",
                    "front_image_index": 0,
                    "back_image_index": None,
                }
            ],
        }
        fixed = fixup_document_ocr_result(ocr)
        person = fixed["persons"][0]
        self.assertIsNone(person["front_image_index"])
        self.assertEqual(person["back_image_index"], 0)
        self.assertEqual(fixed["images"][0]["side"], "back")

    def test_fixup_passport_back_index_becomes_front(self):
        ocr = {
            "images": [
                {
                    "index": 0,
                    "side": "back",
                    "ocr_text": "PASSPORT",
                    "mrz_lines": ["P<BIHSUJIC<MILE<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<"],
                }
            ],
            "persons": [
                {
                    "given_names": "MILE",
                    "surnames": "SUJIC",
                    "document_type": "passport",
                    "document_number": "B4521395",
                    "front_image_index": None,
                    "back_image_index": 0,
                }
            ],
        }
        fixed = fixup_document_ocr_result(ocr)
        person = fixed["persons"][0]
        self.assertEqual(person["front_image_index"], 0)
        self.assertIsNone(person["back_image_index"])

    def test_normalize_document_number_strips_spaces(self):
        self.assertEqual(normalize_document_number("DGN 767255"), "DGN767255")

    def test_normalize_document_number_strips_hyphen_and_case(self):
        self.assertEqual(normalize_document_number("ab-123 456"), "AB123456")

    def test_german_id_mrz_surname_not_birth_name(self):
        mrz = ["IDD<L5M4ZJ1450<<<<<<<<<<<<<<<", "8403179<3307102D<2108<8", "ENGELAND<JASMIN<<<<<<<<<<<<<"]
        self.assertEqual(_surname_from_mrz_lines(mrz), "ENGELAND")

    def test_german_id_front_field_a_surname(self):
        text = (
            "BUNDESREPUBLIK DEUTSCHLAND\n"
            "PERSONALAUSWEIS\n"
            "[a] Name/Surname/Nom\n"
            "ENGELAND\n"
            "[b] Geburtsname/Name at birth/Nom de naissance\n"
            "VALIDŽIČ\n"
            "Vornamen/Given names/Prénoms\n"
            "JASMIN\n"
        )
        self.assertEqual(_surname_from_german_id_front_ocr(text), "ENGELAND")

    def test_fixup_corrects_validzic_to_engeland(self):
        ocr = {
            "images": [
                {
                    "index": 1,
                    "side": "back",
                    "ocr_text": "MRZ",
                    "mrz_lines": [
                        "IDD<L5M4ZJ1450<<<<<<<<<<<<<<<",
                        "8403179<3307102D<2108<8",
                        "ENGELAND<JASMIN<<<<<<<<<<<<<",
                    ],
                },
                {
                    "index": 3,
                    "side": "front",
                    "ocr_text": (
                        "BUNDESREPUBLIK DEUTSCHLAND\nPERSONALAUSWEIS\nENGELAND\nVALIDZIC\nJASMIN"
                    ),
                    "mrz_lines": [],
                },
            ],
            "persons": [
                {
                    "given_names": "JASMIN",
                    "surnames": "VALIDZIC",
                    "front_image_index": 3,
                    "back_image_index": 1,
                    "mrz_lines": [
                        "IDD<L5M4ZJ1450<<<<<<<<<<<<<<<",
                        "8403179<3307102D<2108<8",
                        "ENGELAND<JASMIN<<<<<<<<<<<<<",
                    ],
                }
            ],
        }
        fixed = fixup_document_ocr_result(ocr)
        self.assertEqual(fixed["persons"][0]["surnames"], "ENGELAND")

    def test_fixup_corrects_gunter_to_engeland_from_mrz(self):
        ocr = {
            "images": [
                {
                    "index": 0,
                    "side": "back",
                    "ocr_text": "",
                    "mrz_lines": [
                        "IDD<L5M44167P3<<<<<<<<<<<<<<<",
                        "6306092<3407297D<2405<9",
                        "ENGELAND<INGO<UWE<GUENTER<<<<",
                    ],
                },
                {
                    "index": 2,
                    "side": "front",
                    "ocr_text": "PERSONALAUSWEIS\nENGELAND\nINGO UWE GÜNTER",
                    "mrz_lines": [],
                },
            ],
            "persons": [
                {
                    "given_names": "INGO UWE",
                    "surnames": "GÜNTER",
                    "front_image_index": 2,
                    "back_image_index": 0,
                    "mrz_lines": [
                        "IDD<L5M44167P3<<<<<<<<<<<<<<<",
                        "6306092<3407297D<2405<9",
                        "ENGELAND<INGO<UWE<GUENTER<<<<",
                    ],
                }
            ],
        }
        fixed = fixup_document_ocr_result(ocr)
        self.assertEqual(fixed["persons"][0]["surnames"], "ENGELAND")

    def test_diacritic_name_keys_match(self):
        person_keys = _person_name_keys(
            {"given_names": "JOANNA JULIA", "surnames": "GOLEBIOWSKA"}
        )
        guest_key = _normalize_guest_name_key("JOANNA JULIA GOŁĘBIOWSKA")
        self.assertIn(guest_key, person_keys)
