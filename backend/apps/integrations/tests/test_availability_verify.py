"""PostGIS tests for Channex GET /availability verify + repair (any tenant)."""

from datetime import date
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from apps.integrations.channex.availability_verify_service import (
    AvailabilityMismatch,
    evaluate_repair_blast_radius,
    find_availability_mismatches,
    repair_availability,
    reset_channex_verify_repair_counters,
    verify_and_repair_availability,
    verify_availability,
)
from apps.integrations.channex.demo_property import CHANNEX_DEMO_PROPERTY_SLUG
from apps.integrations.models import IntegrationConfig
from apps.properties.models import Property, Unit
from apps.reservations.models import Reservation, ReservationUnit
from apps.tenants.models import Tenant


class AvailabilityVerifyTests(TestCase):
    def setUp(self):
        reset_channex_verify_repair_counters()
        # Generic fixture — not tied to production tenant id 2 / uzorita.
        self.tenant = Tenant.objects.create(slug="verify-demo", name="Verify Demo")
        self.property = Property.objects.create(
            tenant=self.tenant,
            slug=CHANNEX_DEMO_PROPERTY_SLUG,
            name="Verify Property",
            timezone="Europe/Zagreb",
        )
        self.unit = Unit.objects.create(
            tenant=self.tenant,
            property=self.property,
            code="R1",
            name="Room 1",
        )
        self.room_type_id = "rt-verify-1"
        self.integration = IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.CHANNEX,
            is_active=True,
        )
        self.integration.set_config_dict(
            {
                "property_id": "prop-verify",
                "certification_property_slug": CHANNEX_DEMO_PROPERTY_SLUG,
                "room_types": [
                    {
                        "unit_code": "R1",
                        "channex_room_type_id": self.room_type_id,
                    }
                ],
            }
        )
        self.integration.save()

    def _mock_client(self, live: dict):
        client = MagicMock()
        client.get_availability.return_value = live
        return client

    def test_find_mismatches_when_channex_open_but_occupied(self):
        ReservationUnit.objects.create(
            tenant=self.tenant,
            reservation=Reservation.objects.create(
                tenant=self.tenant,
                property=self.property,
                check_in=date(2026, 8, 1),
                check_out=date(2026, 8, 2),
                status=Reservation.Status.EXPECTED,
                booker_name="Guest",
                import_source="manual",
            ),
            unit=self.unit,
            room_name="R1",
        )
        live = {self.room_type_id: {"2026-08-01": 1}}
        mismatches, meta = find_availability_mismatches(
            tenant_slug=self.tenant.slug,
            days=1,
            from_date=date(2026, 8, 1),
            client=self._mock_client(live),
        )
        self.assertEqual(meta["units_checked"], 1)
        self.assertEqual(len(mismatches), 1)
        self.assertEqual(mismatches[0].expected, 0)
        self.assertEqual(mismatches[0].actual, 1)

    def test_no_mismatch_when_in_sync(self):
        live = {self.room_type_id: {"2026-08-01": 1}}
        mismatches, meta = find_availability_mismatches(
            tenant_slug=self.tenant.slug,
            days=1,
            from_date=date(2026, 8, 1),
            client=self._mock_client(live),
        )
        self.assertEqual(meta["mismatch_count"], 0)
        self.assertEqual(mismatches, [])

    def test_missing_channex_day_treated_as_mismatch(self):
        live = {self.room_type_id: {}}
        mismatches, _meta = find_availability_mismatches(
            tenant_slug=self.tenant.slug,
            days=1,
            from_date=date(2026, 8, 1),
            client=self._mock_client(live),
        )
        self.assertEqual(len(mismatches), 1)
        self.assertEqual(mismatches[0].actual, -1)
        self.assertEqual(mismatches[0].expected, 1)

    @override_settings(CHANNEX_OUTBOUND_ENABLED=True)
    @patch("apps.integrations.channex.availability_verify_service._notify_mismatches")
    @patch("apps.integrations.channex.availability_verify_service.push_channex_ari")
    @patch("apps.integrations.channex.availability_verify_service.apply_availability_updates")
    def test_repair_re_pushes_expected_availability(
        self, mock_apply, mock_push, mock_notify
    ):
        mock_apply.return_value = []
        mock_push.return_value = []
        live = {self.room_type_id: {"2026-08-01": 0}}  # closed, but stay.hr free
        result = verify_and_repair_availability(
            tenant_slug=self.tenant.slug,
            days=1,
            from_date=date(2026, 8, 1),
            repair=True,
            notify=True,
            client=self._mock_client(live),
        )
        self.assertEqual(result["mismatch_count"], 1)
        self.assertEqual(result["repaired"], 1)
        mock_apply.assert_called_once()
        updates = mock_apply.call_args.args[1]
        self.assertEqual(
            updates,
            [{"unit_code": "R1", "date": "2026-08-01", "availability": 1}],
        )
        mock_push.assert_called_once()
        mock_notify.assert_called_once()

    @patch("apps.integrations.channex.availability_verify_service._notify_mismatches")
    @patch("apps.integrations.channex.availability_verify_service.push_channex_ari")
    @patch("apps.integrations.channex.availability_verify_service.apply_availability_updates")
    def test_verify_only_skips_repair_and_notify_when_disabled(
        self, mock_apply, mock_push, mock_notify
    ):
        live = {self.room_type_id: {"2026-08-01": 0}}
        result = verify_and_repair_availability(
            tenant_slug=self.tenant.slug,
            days=1,
            from_date=date(2026, 8, 1),
            repair=False,
            notify=False,
            client=self._mock_client(live),
        )
        self.assertEqual(result["mismatch_count"], 1)
        self.assertEqual(result["repaired"], 0)
        mock_apply.assert_not_called()
        mock_push.assert_not_called()
        mock_notify.assert_not_called()

    def test_requires_tenant_slug(self):
        result = verify_and_repair_availability(tenant_slug="")
        self.assertTrue(result.get("skipped"))
        self.assertIn("tenant_slug", result.get("reason", ""))

    @override_settings(
        CHANNEX_OUTBOUND_ENABLED=True,
        CHANNEX_ARI_REPAIR_MAX_UNITS=2,
        CHANNEX_ARI_REPAIR_MAX_UNIT_PERCENT=10.0,
        CHANNEX_ARI_REPAIR_MAX_DAYS_PER_UNIT=10,
    )
    @patch("apps.integrations.channex.availability_verify_service._notify_mismatches")
    @patch("apps.integrations.channex.availability_verify_service.push_channex_ari")
    @patch("apps.integrations.channex.availability_verify_service.apply_availability_updates")
    def test_threshold_skips_repair(self, mock_apply, mock_push, mock_notify):
        day = date(2026, 8, 1)
        mismatches = [
            AvailabilityMismatch("R1", "rt1", day, 1, 0),
            AvailabilityMismatch("R2", "rt2", day, 1, 0),
            AvailabilityMismatch("R3", "rt3", day, 1, 0),
        ]
        blast = evaluate_repair_blast_radius(mismatches, units_checked=5)
        self.assertEqual(blast["threshold"], "trip")
        self.assertIn("max_units", blast["reasons"])

        result = repair_availability(
            tenant_slug=self.tenant.slug,
            mismatches=mismatches,
            units_checked=5,
            notify=True,
            caller="cli",
        )
        self.assertTrue(result["repair_skipped"])
        self.assertEqual(result["repair_skip_reason"], "threshold")
        self.assertEqual(result["repaired"], 0)
        mock_apply.assert_not_called()
        mock_push.assert_not_called()
        mock_notify.assert_called_once()

    @override_settings(CHANNEX_OUTBOUND_ENABLED=False)
    @patch("apps.integrations.channex.availability_verify_service._notify_mismatches")
    @patch("apps.integrations.channex.availability_verify_service.push_channex_ari")
    @patch("apps.integrations.channex.availability_verify_service.apply_availability_updates")
    def test_incident_2026_08_01_stale_local_verify_does_not_post(
        self, mock_apply, mock_push, mock_notify
    ):
        """Regression: local expected free vs remote occupied → verify only, no POST.

        Mirrors WSL second-writer incident: stale local DB thought R4 was free
        while channel showed 0; Beat must not repair.
        """
        # Local: no blocking reservation → expected=1
        # Remote: closed (actual=0) as if production guest occupies the room
        live = {self.room_type_id: {"2026-08-01": 0}}
        result = verify_availability(
            tenant_slug=self.tenant.slug,
            days=1,
            from_date=date(2026, 8, 1),
            notify=True,
            client=self._mock_client(live),
        )
        self.assertEqual(result["mismatch_count"], 1)
        self.assertEqual(result["repaired"], 0)
        self.assertEqual(result["mismatches"][0]["expected"], 1)
        self.assertEqual(result["mismatches"][0]["actual"], 0)
        mock_apply.assert_not_called()
        mock_push.assert_not_called()
        mock_notify.assert_called_once()

        # Explicit repair on read-only host must not POST either
        mismatches, meta = find_availability_mismatches(
            tenant_slug=self.tenant.slug,
            days=1,
            from_date=date(2026, 8, 1),
            client=self._mock_client(live),
        )
        repaired = repair_availability(
            tenant_slug=self.tenant.slug,
            mismatches=mismatches,
            units_checked=meta["units_checked"],
            notify=False,
            caller="beat",
        )
        self.assertEqual(repaired["repaired"], 0)
        self.assertTrue(repaired.get("repair_skipped"))
        mock_apply.assert_not_called()
        mock_push.assert_not_called()
