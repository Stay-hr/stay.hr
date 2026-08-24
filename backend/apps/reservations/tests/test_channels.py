from types import SimpleNamespace

from django.test import SimpleTestCase

from apps.reservations.channels import reservation_channel
from apps.reservations.channel_sync import (
    IMPORT_SOURCE_BOOKING_PDF,
    IMPORT_SOURCE_BOOKING_XLS,
    IMPORT_SOURCE_CHANNEX,
)


def _row(**kwargs):
    defaults = {"import_source": "", "source": ""}
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class ReservationChannelTests(SimpleTestCase):
    def test_channex_booking_com(self):
        self.assertEqual(
            reservation_channel(
                _row(import_source=IMPORT_SOURCE_CHANNEX, source="Booking.com")
            ),
            {"key": "booking_com", "label": "Booking.com", "transport": "channex"},
        )

    def test_channex_airbnb(self):
        self.assertEqual(
            reservation_channel(_row(import_source=IMPORT_SOURCE_CHANNEX, source="Airbnb")),
            {"key": "airbnb", "label": "Airbnb", "transport": "channex"},
        )

    def test_channex_expedia(self):
        self.assertEqual(
            reservation_channel(
                _row(import_source=IMPORT_SOURCE_CHANNEX, source="Expedia")
            ),
            {"key": "expedia", "label": "Expedia", "transport": "channex"},
        )

    def test_channex_unknown_ota_keeps_source_label(self):
        self.assertEqual(
            reservation_channel(
                _row(import_source=IMPORT_SOURCE_CHANNEX, source="Offline")
            ),
            {"key": "other", "label": "Offline", "transport": "channex"},
        )

    def test_channex_without_ota_name(self):
        self.assertEqual(
            reservation_channel(_row(import_source=IMPORT_SOURCE_CHANNEX, source="")),
            {"key": "other", "label": "Channex", "transport": "channex"},
        )

    def test_channex_fallback_source_name(self):
        self.assertEqual(
            reservation_channel(
                _row(import_source=IMPORT_SOURCE_CHANNEX, source="Channex")
            ),
            {"key": "other", "label": "Channex", "transport": "channex"},
        )

    def test_booking_pdf(self):
        self.assertEqual(
            reservation_channel(
                _row(import_source=IMPORT_SOURCE_BOOKING_PDF, source="Booking.com")
            ),
            {"key": "booking_com", "label": "Booking.com", "transport": "import"},
        )

    def test_booking_xls(self):
        self.assertEqual(
            reservation_channel(_row(import_source=IMPORT_SOURCE_BOOKING_XLS, source="")),
            {"key": "booking_com", "label": "Booking.com", "transport": "import"},
        )

    def test_manual_reception(self):
        self.assertEqual(
            reservation_channel(_row(import_source="manual", source="reception")),
            {"key": "reception", "label": "Reception", "transport": "direct"},
        )

    def test_web_booking(self):
        self.assertEqual(
            reservation_channel(_row(import_source="", source="api")),
            {"key": "web", "label": "Web", "transport": "direct"},
        )

    def test_empty_reservation_is_other(self):
        self.assertEqual(
            reservation_channel(_row()),
            {"key": "other", "label": "Other", "transport": None},
        )

    def test_unknown_direct_source_keeps_label(self):
        self.assertEqual(
            reservation_channel(_row(import_source="", source="Hotels.com")),
            {"key": "other", "label": "Hotels.com", "transport": None},
        )
