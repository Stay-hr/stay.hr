from datetime import date
from decimal import Decimal
from pathlib import Path

from django.test import TestCase

from apps.reservations.booking_pdf_import import (
    extract_pdf_text,
    parse_booking_pdf,
    parse_booking_pdf_text,
)

IMPORTS = Path(__file__).resolve().parents[4] / ".imports"


class BookingPdfImportTests(TestCase):
    def test_parse_active_booking_pdf(self):
        # Binary Booking.com PDFs live under gitignored .imports/; skip on CI/clean clones.
        path = IMPORTS / "5145601516.pdf"
        if not path.exists():
            self.skipTest("Sample PDF not available")
        content = path.read_bytes()
        text = extract_pdf_text(content)
        self.assertIn("5145601516", text)

        row = parse_booking_pdf(content)
        self.assertEqual(row.external_id, "5145601516")
        self.assertEqual(row.booker_name, "Peter Boogaart")
        self.assertEqual(row.booker_email, "pbooga.926896@guest.booking.com")
        self.assertEqual(row.check_in_date, date(2026, 5, 24))
        self.assertEqual(row.check_out_date, date(2026, 5, 25))
        self.assertEqual(row.booking_status, "ok")
        self.assertEqual(row.total_amount, Decimal("75.65"))
        self.assertEqual(row.adults_count, 2)
        self.assertEqual(row.booker_country, "NL")
        self.assertIn("Deluxe King Room", row.room_name)
        self.assertIn("R1", row.room_name)

    def test_parse_canceled_booking_pdf(self):
        # Binary Booking.com PDFs live under gitignored .imports/; skip on CI/clean clones.
        path = IMPORTS / "6250886338.pdf"
        if not path.exists():
            self.skipTest("Sample PDF not available")
        content = path.read_bytes()
        row = parse_booking_pdf(content)
        self.assertEqual(row.external_id, "6250886338")
        self.assertEqual(row.booker_name, "Kristina Mihaljević")
        self.assertEqual(row.check_in_date, date(2026, 5, 23))
        self.assertEqual(row.check_out_date, date(2026, 5, 24))
        self.assertEqual(row.booking_status, "cancelled_by_guest")
        self.assertEqual(row.total_amount, Decimal("88.11"))
        self.assertEqual(row.adults_count, 2)
        self.assertEqual(row.booker_country, "BA")
        self.assertIn("R-6 DELUXE KING", row.room_name)
        self.assertIsNotNone(row.canceled_at)
        self.assertEqual(row.canceled_at.date(), date(2026, 5, 23))

    def test_parse_villalonga_booking_pdf(self):
        path = IMPORTS / "6950508284.pdf"
        if not path.exists():
            self.skipTest("Sample PDF not available")
        content = path.read_bytes()
        row = parse_booking_pdf(content)
        self.assertEqual(row.external_id, "6950508284")
        self.assertEqual(row.booker_name, "Francisco Caimaris Villalonga")
        self.assertEqual(row.booker_country, "ES")
        self.assertEqual(row.adults_count, 2)
        self.assertEqual(row.guest_names, ["Francisco Caimaris Villalonga"])
        self.assertEqual(row.booker_email, "fvilla.491494@guest.booking.com")

    MULTI_ROOM_TEXT = """
Luxury Room Uzorita B&B
Check-in
Sun, May 25, 2026
Check-out
Mon, May 26, 2026
Total guests:
4 adults
Total units
2
Total price
€ 201.65
Guest name:
Kris Meeus
 be
kmeeus.604082@guest.booking.com
Booking number:
5898434847
Deluxe King Room (Luxury Room Uzorita - R2)
€ 109.00
Guest Name
Kurt Meeus
Booked occupancy
2 adults
Total room price
€ 109.00
Deluxe King Room (Luxury Room Uzorita - R1)
€ 92.65
Guest Name
Kris Meeus
Booked occupancy
2 adults
Total room price
€ 92.65
"""

    def test_parse_multi_room_booking_text(self):
        row = parse_booking_pdf_text(self.MULTI_ROOM_TEXT)
        self.assertEqual(row.external_id, "5898434847")
        self.assertEqual(row.booker_name, "Kris Meeus")
        self.assertEqual(row.guest_names, ["Kris Meeus", "Kurt Meeus"])
        self.assertEqual(row.units_count, 2)
        self.assertEqual(row.adults_count, 4)
        self.assertEqual(row.booker_country, "BE")
        self.assertEqual(row.total_amount, Decimal("201.65"))
        self.assertEqual(row.unit_amounts, (Decimal("109.00"), Decimal("92.65")))
        self.assertIn("R1", row.room_name)
        self.assertIn("R2", row.room_name)
        self.assertEqual(row.booker_email, "kmeeus.604082@guest.booking.com")

    def test_parse_multi_room_booking_pdf(self):
        path = IMPORTS / "5898434847.pdf"
        if not path.exists():
            self.skipTest("Sample PDF not available")
        content = path.read_bytes()
        row = parse_booking_pdf(content)
        self.assertEqual(row.external_id, "5898434847")
        self.assertEqual(row.booker_name, "Kris Meeus")
        self.assertEqual(row.guest_names, ["Kris Meeus", "Kurt Meeus"])
        self.assertEqual(row.units_count, 2)
        self.assertEqual(row.adults_count, 4)
        self.assertEqual(row.booker_country, "BE")

    def test_parse_truncated_room_guest_name_deduped(self):
        """Booking PDFs sometimes clip room-block Guest Name (prefix of booker)."""
        text = """
Luxury Room Uzorita B&B
Check-in
Wed, Aug 5, 2026
Check-out
Thu, Aug 6, 2026
Total guests:
2 adults
Total units
1
Total price
€ 79.57
Guest name:
Laurence Lambert de Beaulieu
fr
lbeaul.503187@guest.booking.com
Booking number:
5304300338
Deluxe King Room (Luxury Room Uzorita - R1)
€ 79.57
Guest Name
Laurence Lambert de
Booked occupancy
2 adults
Total room price
€ 79.57
"""
        row = parse_booking_pdf_text(text)
        self.assertEqual(row.external_id, "5304300338")
        self.assertEqual(row.booker_name, "Laurence Lambert de Beaulieu")
        self.assertEqual(row.guest_names, ["Laurence Lambert de Beaulieu"])
        self.assertEqual(row.adults_count, 2)
        self.assertEqual(row.booker_country, "FR")

    def test_parse_truncated_room_guest_name_from_pdf_file(self):
        path = (
            Path(__file__).resolve().parents[4]
            / "data/media/booking_confirmations/uzorita/5304300338.pdf"
        )
        if not path.exists():
            self.skipTest("5304300338.pdf not available")
        row = parse_booking_pdf(path.read_bytes())
        self.assertEqual(row.external_id, "5304300338")
        self.assertEqual(row.guest_names, ["Laurence Lambert de Beaulieu"])

    def test_rejects_empty_pdf(self):
        with self.assertRaises(ValueError):
            parse_booking_pdf(b"")
