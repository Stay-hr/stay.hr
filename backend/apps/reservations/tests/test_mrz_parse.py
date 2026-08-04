"""Tests for MRZ parsing helpers."""

from django.test import SimpleTestCase

from apps.reservations.mrz_parse import (
    normalize_residence_address,
    parse_document_number_from_mrz,
    parse_sex_from_mrz,
)


class MrzParseTests(SimpleTestCase):
    def test_parse_sex_td1(self):
        mrz = "IDD<LFJK9GXWK1<<<<<<<<<<<\n7505113M3410071DEU<<<<<<<<<\nWALL<<WALDEMAR<<<<<<<<<<<<<"
        self.assertEqual(parse_sex_from_mrz(mrz), "M")

    def test_parse_sex_td3(self):
        mrz = (
            "P<D<<FISCHER<<HANS<<<<<<<<<<<<<<<<<<<<<<<<<<<\n"
            "L01X00T478D<<8005011M3005017<<<<<<<<<<<<<<04"
        )
        self.assertEqual(parse_sex_from_mrz(mrz), "M")

    def test_parse_sex_unspecified_returns_empty(self):
        mrz = "IDD<LFJK9GXWK1<<<<<<<<<<<\n7505113<3410071D<2405<<<<3\nWALL<<WALDEMAR<<<<<<<<<<<<<"
        self.assertEqual(parse_sex_from_mrz(mrz), "")

    def test_parse_document_number_td1(self):
        mrz = "IDD<<LFJK9GXWK<<<<<<<<<<<<<<<\n7505113M3410071DEU<<<<<<<<<<<6\nWALL<<WALDEMAR<<<<<<<<<<<<<<"
        self.assertEqual(parse_document_number_from_mrz(mrz), "LFJK9GXWK")

    def test_parse_document_number_td3(self):
        mrz = (
            "P<D<<FISCHER<<HANS<<<<<<<<<<<<<<<<<<<<<<<<<<<\n"
            "L01X00T478D<<8005011M3005017<<<<<<<<<<<<<<04"
        )
        self.assertEqual(parse_document_number_from_mrz(mrz), "L01X00T47")
