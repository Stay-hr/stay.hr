from datetime import date
from unittest.mock import Mock
from uuid import uuid4

from django.test import SimpleTestCase

from apps.integrations.evisitor.config import EvisitorRuntimeConfig
from apps.integrations.evisitor.mapper import build_check_in_payload


class EvisitorMapperIndonesiaTests(SimpleTestCase):
    def setUp(self):
        self.reservation = Mock()
        self.reservation.property_id = 4
        self.reservation.check_in = date(2026, 8, 22)
        self.reservation.check_out = date(2026, 8, 23)

        self.guest = Mock()
        self.guest.reservation = self.reservation
        self.guest.first_name = "Santi Kartika"
        self.guest.last_name = "Sari"
        self.guest.sex = "female"
        self.guest.date_of_birth = date(1990, 1, 1)
        self.guest.nationality = "ID"
        self.guest.document_country_iso2 = "ID"
        self.guest.document_country_iso3 = ""
        self.guest.document_type = "passport"
        self.guest.document_code = ""
        self.guest.document_number = "P1234567"
        self.guest.address = "Jakarta, Jakarta"

        self.config = EvisitorRuntimeConfig(
            enabled=True,
            env="test",
            base_url="https://test.evisitor.hr/test/rest",
            username="user",
            password="pass",
            api_key="key",
            facility_code="12345",
            default_stay_time_from="15:00",
            default_stay_time_until="10:00",
            default_arrival_organisation="01",
            default_offered_service_type="01",
            default_payment_category="01",
        )

    def test_nationality_id_maps_to_idn_without_evisitor_api(self):
        payload = build_check_in_payload(
            self.guest,
            config=self.config,
            registration_id=uuid4(),
        )
        self.assertEqual(payload["Citizenship"], "IDN")
        self.assertEqual(payload["CountryOfBirth"], "IDN")
