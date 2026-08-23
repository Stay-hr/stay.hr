from unittest.mock import Mock

from django.test import SimpleTestCase

from apps.reservations.guest_checkin_orchestrator import (
    GuestCheckInOrchestratorError,
    _apply_guest_fields,
)


class ApplyGuestFieldsNationalityTests(SimpleTestCase):
    def _guest_with_document_country(self) -> Mock:
        guest = Mock()
        guest.nationality = "DE"
        guest.document_country_iso2 = "DE"
        guest.document_country_iso3 = "DEU"
        guest.save = Mock()
        return guest

    def test_patch_nationality_does_not_change_document_country_fields(self):
        guest = self._guest_with_document_country()
        _apply_guest_fields(guest, {"nationality": "ID"})
        self.assertEqual(guest.nationality, "ID")
        self.assertEqual(guest.document_country_iso2, "DE")
        self.assertEqual(guest.document_country_iso3, "DEU")
        guest.save.assert_called_once()

    def test_patch_invalid_nationality_rejected(self):
        guest = self._guest_with_document_country()
        with self.assertRaises(GuestCheckInOrchestratorError) as ctx:
            _apply_guest_fields(guest, {"nationality": "XX"})
        self.assertEqual(ctx.exception.code, "invalid_nationality")
        guest.save.assert_not_called()
