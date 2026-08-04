from datetime import date
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase

from apps.integrations.evisitor.config import EvisitorRuntimeConfig
from apps.integrations.evisitor.exceptions import EvisitorValidationError
from apps.integrations.evisitor.mapper import build_check_in_payload
from apps.properties.models import Property
from apps.reservations.models import Guest, Reservation
from apps.tenants.models import Tenant


class EvisitorMapperTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="uzorita", name="Uzorita", default_language="hr")
        self.property = Property.objects.create(tenant=self.tenant, name="Uzorita", slug="uzorita")
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booker_name="Test Guest",
            check_in=date(2026, 6, 11),
            check_out=date(2026, 6, 13),
            status=Reservation.Status.EXPECTED,
        )
        self.guest = Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="Test",
            last_name="Guest",
            name="Test Guest",
            sex="M",
            date_of_birth=date(1990, 1, 1),
            nationality="DE",
            document_type="national_id",
            document_number="L1234567",
            document_country_iso2="DE",
            document_country_iso3="DEU",
            address="Berlin, Grad Berlin",
        )
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

    @patch("apps.integrations.evisitor.mapper.iso2_to_iso3", return_value="DEU")
    def test_time_stay_from_override(self, mock_iso):
        payload = build_check_in_payload(
            self.guest,
            config=self.config,
            registration_id=uuid4(),
            time_stay_from="12:08",
        )
        self.assertEqual(payload["TimeStayFrom"], "12:08")

    @patch("apps.integrations.evisitor.mapper.iso2_to_iso3", return_value="DEU")
    def test_time_stay_from_defaults_to_config(self, mock_iso):
        payload = build_check_in_payload(
            self.guest,
            config=self.config,
            registration_id=uuid4(),
        )
        self.assertEqual(payload["TimeStayFrom"], "15:00")

    @patch("apps.integrations.evisitor.mapper.iso2_to_iso3", return_value="DEU")
    def test_uses_validated_city_exclusively(self, mock_iso):
        payload = build_check_in_payload(
            self.guest,
            config=self.config,
            registration_id=uuid4(),
        )
        self.assertEqual(payload["CityOfResidence"], "Berlin")
        self.assertEqual(payload["CityOfBirth"], "Berlin")
        self.assertEqual(payload["ResidenceAddress"], "Berlin, Grad Berlin")

    @patch("apps.integrations.evisitor.mapper.iso2_to_iso3", return_value="DEU")
    def test_invalid_address_raises_validation_error(self, mock_iso):
        self.guest.address = "DONJI BITELIĆ HRVACE DONJI BITELIĆ 208 A"
        self.guest.save(update_fields=["address", "updated_at"])
        with self.assertRaises(EvisitorValidationError) as ctx:
            build_check_in_payload(
                self.guest,
                config=self.config,
                registration_id=uuid4(),
            )
        self.assertIn("address", ctx.exception.field_errors)

    @patch("apps.integrations.evisitor.mapper.iso2_to_iso3", return_value="DEU")
    def test_street_first_address_raises_validation_error(self, mock_iso):
        self.guest.address = "Dubrovačka 30, Osijek"
        self.guest.save(update_fields=["address", "updated_at"])
        with self.assertRaises(EvisitorValidationError) as ctx:
            build_check_in_payload(
                self.guest,
                config=self.config,
                registration_id=uuid4(),
            )
        self.assertIn("address", ctx.exception.field_errors)
