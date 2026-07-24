from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from apps.communications.models import GuestMessageChannel, GuestOutboundMessageStatus
from apps.properties.guest_settings_events import GuestPortalShared, GuestSettingsUpdated
from apps.properties.guest_settings_service import (
    GuestSettingsConflict,
    GuestSettingsService,
    parse_if_match_version,
    settings_version_etag,
)
from apps.properties.guest_settings_validation import (
    GUEST_SETTINGS_SCHEMA_VERSION,
    GuestSettingsValidationError,
    validate_guest_settings_payload,
)
from apps.properties.models import Property, SelfServiceMode
from apps.properties.portal_renderer import PortalRenderer
from apps.properties.property_settings_service import (
    build_settings_capabilities,
    list_properties_for_tenant,
    settings_surface_enabled,
)
from apps.properties.share_service import ShareService, ShareServiceError
from apps.properties.uzorita_guest_info import UZORITA_GUEST_INFO
from apps.reservations.guest_checkin_session import ensure_active_session, mark_session_completed
from apps.reservations.guest_portal_access import ensure_active_portal_access
from apps.reservations.guest_portal_context import (
    build_guest_portal_context,
    serialize_guest_portal_context,
)
from apps.reservations.models import (
    GuestCheckInSessionCreatedFrom,
    GuestPortalAccessCreatedFrom,
    Reservation,
)
from apps.tenants.models import RECEPTION_DEVICE_SCOPES, ApiApplication, Tenant

class PropertySettingsServiceTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Settings Tenant", slug="settings-tenant")
        self.prop_a = Property.objects.create(
            tenant=self.tenant,
            name="Alpha",
            slug="alpha",
        )
        self.prop_b = Property.objects.create(
            tenant=self.tenant,
            name="Beta",
            slug="beta",
        )

    @override_settings(RECEPTION_PROPERTY_SETTINGS=True)
    def test_capabilities_when_enabled(self):
        payload = build_settings_capabilities()
        self.assertTrue(payload["tabs"]["guest"])
        self.assertTrue(payload["tabs"]["general"])
        self.assertTrue(payload["tabs"]["checkin"])
        self.assertTrue(payload["tabs"]["automation"])
        self.assertTrue(payload["capabilities"]["guest_settings"])
        self.assertTrue(payload["capabilities"]["preview"])
        self.assertTrue(payload["capabilities"]["share"])
        self.assertTrue(payload["capabilities"]["general"])
        self.assertTrue(payload["capabilities"]["checkin"])
        self.assertTrue(payload["capabilities"]["automation"])
        self.assertTrue(settings_surface_enabled(payload))

    @override_settings(RECEPTION_PROPERTY_SETTINGS=False)
    def test_capabilities_when_disabled(self):
        payload = build_settings_capabilities()
        self.assertFalse(any(payload["tabs"].values()))
        self.assertFalse(any(payload["capabilities"].values()))
        self.assertFalse(settings_surface_enabled(payload))

    def test_list_properties_ordered_and_tenant_scoped(self):
        other = Tenant.objects.create(name="Other", slug="other-settings")
        Property.objects.create(tenant=other, name="Other Prop", slug="other")
        rows = list_properties_for_tenant(self.tenant)
        self.assertEqual([row["slug"] for row in rows], ["alpha", "beta"])
        self.assertEqual({row["id"] for row in rows}, {self.prop_a.id, self.prop_b.id})


class GuestSettingsValidationTests(TestCase):
    def test_rejects_future_schema_version(self):
        with self.assertRaises(GuestSettingsValidationError) as ctx:
            validate_guest_settings_payload({"schema_version": 99, "wifi": {"ssid": "x"}})
        self.assertIn("schema_version", ctx.exception.errors)

    def test_rejects_long_ssid(self):
        with self.assertRaises(GuestSettingsValidationError) as ctx:
            validate_guest_settings_payload(
                {
                    "schema_version": 1,
                    "wifi": {"ssid": "x" * 200},
                }
            )
        self.assertIn("wifi.ssid", ctx.exception.errors)

    def test_rejects_bad_maps_url(self):
        with self.assertRaises(GuestSettingsValidationError) as ctx:
            validate_guest_settings_payload(
                {
                    "schema_version": 1,
                    "arrival": {"maps_url": "ftp://example.com/map"},
                }
            )
        self.assertIn("arrival.maps_url", ctx.exception.errors)

    def test_etag_helpers(self):
        self.assertEqual(settings_version_etag(3), 'W/"3"')
        self.assertEqual(parse_if_match_version('W/"3"'), 3)
        self.assertEqual(parse_if_match_version('"3"'), 3)
        self.assertIsNone(parse_if_match_version("bogus"))


class GuestSettingsServiceTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Guest Settings", slug="guest-settings")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Uzorita",
            slug="uzorita",
            guest_info=UZORITA_GUEST_INFO,
            contact={"phone": "+385998388513", "whatsapp": "+385998388513"},
            self_service_mode=SelfServiceMode.SCHEDULE,
            self_service_config={"weekdays": [1]},
        )

    def test_get_dto_shape(self):
        dto = GuestSettingsService.get(self.property)
        self.assertEqual(dto["schema_version"], GUEST_SETTINGS_SCHEMA_VERSION)
        self.assertEqual(dto["settings_version"], 1)
        self.assertEqual(dto["wifi"]["ssid"], "Uzoritarooms")
        self.assertIn("media", dto["arrival"]["entrance"])
        self.assertIsNone(dto["arrival"]["entrance"]["media"]["asset_id"])
        self.assertTrue(dto["arrival"]["entrance"]["media"]["url"])
        self.assertEqual(dto["publication"]["state"], "published")
        self.assertFalse(dto["publication"]["draft_available"])
        if dto["guide"]["steps"]:
            self.assertIn("media", dto["guide"]["steps"][0])
            self.assertIsNone(dto["guide"]["steps"][0]["media"]["asset_id"])

    def test_patch_round_trip_bumps_version_and_emits_event(self):
        from apps.properties import guest_settings_events as events_mod

        seen: list[GuestSettingsUpdated] = []

        def _capture(event: GuestSettingsUpdated):
            seen.append(event)

        events_mod._GUEST_SETTINGS_UPDATED_HANDLERS.append(_capture)
        try:
            result = GuestSettingsService.patch(
                self.property,
                {
                    "schema_version": 1,
                    "wifi": {"ssid": "NewSSID", "password": "newpass"},
                },
                expected_version=1,
                actor_id="app:1",
                updated_by={"api_application_id": 1},
            )
            self.assertEqual(result.dto["wifi"]["ssid"], "NewSSID")
            self.assertEqual(result.dto["settings_version"], 2)
            self.assertIn("wifi.ssid", result.change_summary)
            self.property.refresh_from_db()
            self.assertEqual(self.property.settings_version, 2)
            self.assertTrue(seen)
            self.assertEqual(seen[-1].settings_version, 2)
        finally:
            events_mod._GUEST_SETTINGS_UPDATED_HANDLERS.remove(_capture)

    def test_patch_conflict(self):
        with self.assertRaises(GuestSettingsConflict):
            GuestSettingsService.patch(
                self.property,
                {"schema_version": 1, "wifi": {"ssid": "x"}},
                expected_version=99,
            )

    def test_preview_key_guide_respects_on_date(self):
        # Tuesday = weekday 1 → key guide on
        tuesday = date(2026, 7, 14)
        monday = date(2026, 7, 13)
        preview_on = GuestSettingsService.preview(
            self.property, language="en", on_date=tuesday
        )
        preview_off = GuestSettingsService.preview(
            self.property, language="en", on_date=monday
        )
        self.assertIn("key_guide", preview_on["sections"])
        self.assertNotIn("key_guide", preview_off["sections"])
        self.assertIsNone(preview_on["reservation_id"])


class PortalRendererUnifyTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Portal Render", slug="portal-render")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Portal Property",
            slug="portal-property",
            guest_info=UZORITA_GUEST_INFO,
            contact={"phone": "+385998388513"},
            self_service_mode=SelfServiceMode.ALWAYS,
            guest_checkin_opens_days_before=7,
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="PR-1",
            check_in=date(2026, 7, 15),
            check_out=date(2026, 7, 18),
            adults_count=1,
            booker_name="Ana",
            amount=Decimal("100.00"),
            booker_country="HR",
        )

    def test_public_and_legacy_builder_match(self):
        access = ensure_active_portal_access(
            self.reservation,
            created_from=GuestPortalAccessCreatedFrom.SYSTEM,
        )
        via_renderer = serialize_guest_portal_context(
            PortalRenderer.render_for_access(access, language="en")
        )
        via_legacy = serialize_guest_portal_context(
            build_guest_portal_context(access, language="en")
        )
        self.assertEqual(via_renderer, via_legacy)
        self.assertIn("welcome", via_renderer["sections"])
        self.assertEqual(via_renderer["reservation_id"], self.reservation.pk)


class ReceptionPropertySettingsAPITests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Settings API", slug="settings-api")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Uzorita",
            slug="uzorita",
            guest_info=UZORITA_GUEST_INFO,
            contact={"phone": "+38599111222"},
            self_service_mode=SelfServiceMode.ALWAYS,
        )
        self.app, self.raw_token = ApiApplication.create_with_token(
            tenant=self.tenant,
            name="Test tablet",
            scopes=RECEPTION_DEVICE_SCOPES,
        )
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.raw_token}"}

    @override_settings(RECEPTION_PROPERTY_SETTINGS=True)
    def test_settings_capabilities(self):
        response = self.client.get("/api/v1/reception/settings/", **self.auth)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["tabs"]["guest"])
        self.assertTrue(data["capabilities"]["guest_settings"])

    @override_settings(RECEPTION_PROPERTY_SETTINGS=False)
    def test_settings_capabilities_disabled(self):
        response = self.client.get("/api/v1/reception/settings/", **self.auth)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(any(data["tabs"].values()))
        self.assertFalse(any(data["capabilities"].values()))

    def test_properties_list(self):
        response = self.client.get("/api/v1/reception/properties/", **self.auth)
        self.assertEqual(response.status_code, 200)
        results = response.json()["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], self.property.id)
        self.assertEqual(results[0]["slug"], "uzorita")
        self.assertEqual(results[0]["name"], "Uzorita")

    def test_properties_list_tenant_isolation(self):
        other = Tenant.objects.create(name="Other API", slug="other-api")
        Property.objects.create(tenant=other, name="Secret", slug="secret")
        response = self.client.get("/api/v1/reception/properties/", **self.auth)
        slugs = [row["slug"] for row in response.json()["results"]]
        self.assertEqual(slugs, ["uzorita"])

    def test_guest_get_etag(self):
        response = self.client.get(
            f"/api/v1/reception/properties/{self.property.id}/settings/guest/",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["ETag"], 'W/"1"')
        data = response.json()
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["wifi"]["ssid"], "Uzoritarooms")

    def test_guest_patch_round_trip(self):
        get_resp = self.client.get(
            f"/api/v1/reception/properties/{self.property.id}/settings/guest/",
            **self.auth,
        )
        etag = get_resp["ETag"]
        patch = self.client.patch(
            f"/api/v1/reception/properties/{self.property.id}/settings/guest/",
            data={
                "schema_version": 1,
                "wifi": {"ssid": "PatchedSSID", "password": "secret"},
            },
            content_type="application/json",
            HTTP_IF_MATCH=etag,
            **self.auth,
        )
        self.assertEqual(patch.status_code, 200)
        self.assertEqual(patch["ETag"], 'W/"2"')
        self.assertEqual(patch.json()["wifi"]["ssid"], "PatchedSSID")

    def test_guest_patch_schema_version_reject(self):
        patch = self.client.patch(
            f"/api/v1/reception/properties/{self.property.id}/settings/guest/",
            data={"schema_version": 9, "wifi": {"ssid": "x"}},
            content_type="application/json",
            HTTP_IF_MATCH='W/"1"',
            **self.auth,
        )
        self.assertEqual(patch.status_code, 400)
        self.assertEqual(patch.json()["code"], "guest_settings_invalid")

    def test_guest_patch_if_match_409(self):
        conflict = self.client.patch(
            f"/api/v1/reception/properties/{self.property.id}/settings/guest/",
            data={"schema_version": 1, "wifi": {"ssid": "x"}},
            content_type="application/json",
            HTTP_IF_MATCH='W/"99"',
            **self.auth,
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["code"], "settings_version_conflict")
        self.assertEqual(conflict["ETag"], 'W/"1"')

    def test_guest_patch_requires_if_match(self):
        response = self.client.patch(
            f"/api/v1/reception/properties/{self.property.id}/settings/guest/",
            data={"schema_version": 1, "wifi": {"ssid": "x"}},
            content_type="application/json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "if_match_required")

    def test_guest_tenant_isolation(self):
        other = Tenant.objects.create(name="Other", slug="other-prop")
        foreign = Property.objects.create(tenant=other, name="Foreign", slug="foreign")
        response = self.client.get(
            f"/api/v1/reception/properties/{foreign.id}/settings/guest/",
            **self.auth,
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "property_not_found")

    def test_reserved_section_stub_404(self):
        response = self.client.get(
            f"/api/v1/reception/properties/{self.property.id}/settings/security/",
            **self.auth,
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "settings_section_not_available")

    def test_preview_shape(self):
        preview = self.client.get(
            f"/api/v1/reception/properties/{self.property.id}/settings/guest/preview/",
            {"lang": "en", "on_date": "2026-07-15"},
            **self.auth,
        )
        self.assertEqual(preview.status_code, 200)
        data = preview.json()
        self.assertIn("sections", data)
        self.assertIn("content", data)
        self.assertIn("welcome", data["sections"])
        self.assertIsNone(data["reservation_id"])

    def test_settings_requires_auth(self):
        response = self.client.get("/api/v1/reception/settings/")
        self.assertEqual(response.status_code, 403)

    def test_general_get_and_patch_round_trip(self):
        get_resp = self.client.get(
            f"/api/v1/reception/properties/{self.property.id}/settings/general/",
            **self.auth,
        )
        self.assertEqual(get_resp.status_code, 200)
        self.assertEqual(get_resp["ETag"], 'W/"1"')
        data = get_resp.json()
        self.assertEqual(data["name"], "Uzorita")
        self.assertEqual(data["slug"], "uzorita")

        patch = self.client.patch(
            f"/api/v1/reception/properties/{self.property.id}/settings/general/",
            data={
                "name": "Uzorita Rooms",
                "address": "Split",
                "timezone": "Europe/Zagreb",
                "language": "hr",
            },
            content_type="application/json",
            HTTP_IF_MATCH=get_resp["ETag"],
            **self.auth,
        )
        self.assertEqual(patch.status_code, 200)
        self.assertEqual(patch["ETag"], 'W/"2"')
        body = patch.json()
        self.assertEqual(body["name"], "Uzorita Rooms")
        self.assertEqual(body["address"], "Split")
        self.assertEqual(body["timezone"], "Europe/Zagreb")
        self.assertEqual(body["language"], "hr")
        self.assertEqual(body["slug"], "uzorita")

    def test_general_patch_rejects_bad_timezone(self):
        response = self.client.patch(
            f"/api/v1/reception/properties/{self.property.id}/settings/general/",
            data={"timezone": "Not/AZone"},
            content_type="application/json",
            HTTP_IF_MATCH='W/"1"',
            **self.auth,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "general_settings_invalid")
        self.assertIn("timezone", response.json()["errors"])

    def test_general_patch_if_match_409(self):
        conflict = self.client.patch(
            f"/api/v1/reception/properties/{self.property.id}/settings/general/",
            data={"name": "X"},
            content_type="application/json",
            HTTP_IF_MATCH='W/"99"',
            **self.auth,
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["code"], "settings_version_conflict")
        self.assertIn("general_settings", conflict.json())

    def test_checkin_get_and_patch_round_trip(self):
        get_resp = self.client.get(
            f"/api/v1/reception/properties/{self.property.id}/settings/checkin/",
            **self.auth,
        )
        self.assertEqual(get_resp.status_code, 200)
        self.assertEqual(get_resp["ETag"], 'W/"1"')
        self.assertEqual(get_resp.json()["check_in_time"], "15:00")

        patch = self.client.patch(
            f"/api/v1/reception/properties/{self.property.id}/settings/checkin/",
            data={
                "check_in_time": "14:00",
                "check_out_time": "10:30",
                "check_in_latest_time": "22:00",
                "guest_checkin_opens_days_before": 3,
            },
            content_type="application/json",
            HTTP_IF_MATCH=get_resp["ETag"],
            **self.auth,
        )
        self.assertEqual(patch.status_code, 200)
        self.assertEqual(patch["ETag"], 'W/"2"')
        body = patch.json()
        self.assertEqual(body["check_in_time"], "14:00")
        self.assertEqual(body["check_out_time"], "10:30")
        self.assertEqual(body["check_in_latest_time"], "22:00")
        self.assertEqual(body["guest_checkin_opens_days_before"], 3)

    def test_checkin_patch_rejects_latest_before_check_in(self):
        response = self.client.patch(
            f"/api/v1/reception/properties/{self.property.id}/settings/checkin/",
            data={
                "check_in_time": "15:00",
                "check_in_latest_time": "14:00",
            },
            content_type="application/json",
            HTTP_IF_MATCH='W/"1"',
            **self.auth,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "checkin_settings_invalid")
        self.assertIn("check_in_latest_time", response.json()["errors"])

    def test_checkin_tenant_isolation(self):
        other = Tenant.objects.create(name="Other Checkin", slug="other-checkin")
        foreign = Property.objects.create(tenant=other, name="Foreign", slug="foreign-ci")
        response = self.client.get(
            f"/api/v1/reception/properties/{foreign.id}/settings/checkin/",
            **self.auth,
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "property_not_found")

    def test_automation_get_and_patch_round_trip(self):
        get_resp = self.client.get(
            f"/api/v1/reception/properties/{self.property.id}/settings/automation/",
            **self.auth,
        )
        self.assertEqual(get_resp.status_code, 200)
        self.assertEqual(get_resp["ETag"], 'W/"1"')
        data = get_resp.json()
        self.assertEqual(data["after_hours_arrival_policy"], "contact")
        self.assertTrue(data["guest_arrival_auto_reply_enabled"])
        self.assertTrue(data["guest_parking_auto_reply_enabled"])

        patch = self.client.patch(
            f"/api/v1/reception/properties/{self.property.id}/settings/automation/",
            data={
                "after_hours_arrival_policy": "not_allowed",
                "after_hours_contact_phone": "+385991234567",
                "guest_arrival_auto_reply_enabled": False,
                "guest_parking_auto_reply_enabled": False,
            },
            content_type="application/json",
            HTTP_IF_MATCH=get_resp["ETag"],
            **self.auth,
        )
        self.assertEqual(patch.status_code, 200)
        self.assertEqual(patch["ETag"], 'W/"2"')
        body = patch.json()
        self.assertEqual(body["after_hours_arrival_policy"], "not_allowed")
        self.assertEqual(body["after_hours_contact_phone"], "+385991234567")
        self.assertFalse(body["guest_arrival_auto_reply_enabled"])
        self.assertFalse(body["guest_parking_auto_reply_enabled"])
        self.property.refresh_from_db()
        self.assertEqual(self.property.after_hours_arrival_policy, "not_allowed")
        self.assertFalse(self.property.guest_arrival_auto_reply_enabled)

    def test_automation_patch_rejects_bad_policy(self):
        response = self.client.patch(
            f"/api/v1/reception/properties/{self.property.id}/settings/automation/",
            data={"after_hours_arrival_policy": "open_door"},
            content_type="application/json",
            HTTP_IF_MATCH='W/"1"',
            **self.auth,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "automation_settings_invalid")
        self.assertIn("after_hours_arrival_policy", response.json()["errors"])

    def test_automation_patch_rejects_long_phone(self):
        response = self.client.patch(
            f"/api/v1/reception/properties/{self.property.id}/settings/automation/",
            data={"after_hours_contact_phone": "1" * 40},
            content_type="application/json",
            HTTP_IF_MATCH='W/"1"',
            **self.auth,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "automation_settings_invalid")
        self.assertIn("after_hours_contact_phone", response.json()["errors"])

    def test_automation_patch_if_match_409(self):
        conflict = self.client.patch(
            f"/api/v1/reception/properties/{self.property.id}/settings/automation/",
            data={"guest_arrival_auto_reply_enabled": False},
            content_type="application/json",
            HTTP_IF_MATCH='W/"99"',
            **self.auth,
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["code"], "settings_version_conflict")
        self.assertIn("automation_settings", conflict.json())

    def test_automation_tenant_isolation(self):
        other = Tenant.objects.create(name="Other Automation", slug="other-auto")
        foreign = Property.objects.create(tenant=other, name="Foreign", slug="foreign-auto")
        response = self.client.get(
            f"/api/v1/reception/properties/{foreign.id}/settings/automation/",
            **self.auth,
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "property_not_found")

    @override_settings(RECEPTION_PROPERTY_SETTINGS=True)
    def test_settings_capabilities_include_general_checkin_automation(self):
        response = self.client.get("/api/v1/reception/settings/", **self.auth)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["tabs"]["general"])
        self.assertTrue(data["tabs"]["checkin"])
        self.assertTrue(data["tabs"]["automation"])
        self.assertTrue(data["capabilities"]["general"])
        self.assertTrue(data["capabilities"]["checkin"])
        self.assertTrue(data["capabilities"]["automation"])


class ShareServiceTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Share Tenant", slug="share-tenant")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Share Prop",
            slug="share-prop",
            guest_checkin_opens_days_before=7,
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="SHARE-1",
            check_in=date(2026, 7, 21),
            check_out=date(2026, 7, 24),
            adults_count=1,
            booker_name="Share Guest",
            booker_email="share@example.com",
            booker_phone="+385911111111",
            amount=Decimal("90.00"),
            status=Reservation.Status.EXPECTED,
            import_source="channex",
        )

    def _complete_checkin(self, created_from: str):
        session = ensure_active_session(self.reservation, created_from=created_from)
        mark_session_completed(session)
        return session

    @override_settings(RECEPTION_PROPERTY_SETTINGS=True)
    def test_unsupported_kind_and_target(self):
        with self.assertRaises(ShareServiceError) as ctx:
            ShareService.share(
                self.property,
                {
                    "kind": "invoice",
                    "target": "reservation",
                    "reservation_id": self.reservation.pk,
                    "channel": "booking",
                },
            )
        self.assertEqual(ctx.exception.code, "unsupported_kind")

        with self.assertRaises(ShareServiceError) as ctx:
            ShareService.share(
                self.property,
                {
                    "kind": "portal",
                    "target": "guest",
                    "reservation_id": self.reservation.pk,
                    "channel": "booking",
                },
            )
        self.assertEqual(ctx.exception.code, "unsupported_target")

    @override_settings(RECEPTION_PROPERTY_SETTINGS=True)
    def test_default_channel_from_completed_checkin(self):
        self._complete_checkin(GuestCheckInSessionCreatedFrom.CHANNEX)
        seen: list[GuestPortalShared] = []

        def capture(event: GuestPortalShared):
            seen.append(event)

        from django.utils import timezone

        from apps.properties import guest_settings_events as events_mod

        events_mod._GUEST_PORTAL_SHARED_HANDLERS.append(capture)
        try:
            with patch(
                "apps.communications.guest_portal_distribute.send_guest_message",
            ) as mock_send:

                def fake_send(**kwargs):
                    draft = kwargs["draft"]
                    draft.sent_at = timezone.now()
                    draft.save(update_fields=["sent_at"])
                    outbound = MagicMock()
                    outbound.status = GuestOutboundMessageStatus.SENT
                    return outbound

                mock_send.side_effect = fake_send
                result = ShareService.share(
                    self.property,
                    {
                        "kind": "portal",
                        "target": "reservation",
                        "reservation_id": self.reservation.pk,
                    },
                    actor_id="app:1",
                )
            self.assertEqual(result.channel, GuestMessageChannel.BOOKING)
            self.assertIn(result.status, {"sent", "queued", "partial"})
            self.assertEqual(len(seen), 1)
            self.assertEqual(seen[0].reservation_id, self.reservation.pk)
            self.assertEqual(seen[0].channel, GuestMessageChannel.BOOKING)
            self.assertEqual(seen[0].kind, "portal")
            self.assertEqual(mock_send.call_count, 2)
        finally:
            events_mod._GUEST_PORTAL_SHARED_HANDLERS.remove(capture)

    @override_settings(RECEPTION_PROPERTY_SETTINGS=True)
    def test_channel_required_without_checkin(self):
        with self.assertRaises(ShareServiceError) as ctx:
            ShareService.share(
                self.property,
                {
                    "kind": "portal",
                    "target": "reservation",
                    "reservation_id": self.reservation.pk,
                },
            )
        self.assertEqual(ctx.exception.code, "channel_required")

    @override_settings(RECEPTION_PROPERTY_SETTINGS=True)
    def test_reservation_must_belong_to_property(self):
        other = Property.objects.create(
            tenant=self.tenant,
            name="Other",
            slug="other-share",
        )
        with self.assertRaises(ShareServiceError) as ctx:
            ShareService.share(
                other,
                {
                    "kind": "portal",
                    "target": "reservation",
                    "reservation_id": self.reservation.pk,
                    "channel": "email",
                },
            )
        self.assertEqual(ctx.exception.code, "reservation_not_found")


class ReceptionPropertySettingsShareAPITests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Share API", slug="share-api")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Uzorita",
            slug="uzorita",
            guest_checkin_opens_days_before=7,
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="SHARE-API-1",
            check_in=date(2026, 7, 21),
            check_out=date(2026, 7, 24),
            adults_count=1,
            booker_name="API Guest",
            booker_email="api@example.com",
            amount=Decimal("80.00"),
            status=Reservation.Status.EXPECTED,
        )
        self.app, self.raw_token = ApiApplication.create_with_token(
            tenant=self.tenant,
            name="Share tablet",
            scopes=RECEPTION_DEVICE_SCOPES,
        )
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.raw_token}"}
        self.share_url = (
            f"/api/v1/reception/properties/{self.property.id}/settings/share/"
        )

    @override_settings(RECEPTION_PROPERTY_SETTINGS=True)
    def test_share_portal_reservation(self):
        with patch(
            "apps.properties.share_service.send_guest_portal_link",
            return_value={
                "status": "sent",
                "channel": "booking",
                "portal_url": "https://booking.example/g/tok",
                "access_id": 9,
                "draft_id": 1,
                "url_draft_id": 2,
            },
        ) as mock_send:
            response = self.client.post(
                self.share_url,
                data={
                    "kind": "portal",
                    "target": "reservation",
                    "reservation_id": self.reservation.pk,
                    "channel": "booking",
                },
                content_type="application/json",
                **self.auth,
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["kind"], "portal")
        self.assertEqual(data["target"], "reservation")
        self.assertEqual(data["reservation_id"], self.reservation.pk)
        self.assertEqual(data["channel"], "booking")
        self.assertEqual(data["status"], "sent")
        self.assertEqual(data["portal_url"], "https://booking.example/g/tok")
        mock_send.assert_called_once()
        self.assertTrue(mock_send.call_args.kwargs.get("allow_resend"))

    @override_settings(RECEPTION_PROPERTY_SETTINGS=True)
    def test_share_unsupported_kind_400(self):
        response = self.client.post(
            self.share_url,
            data={
                "kind": "guide",
                "target": "reservation",
                "reservation_id": self.reservation.pk,
                "channel": "booking",
            },
            content_type="application/json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "unsupported_kind")

    @override_settings(RECEPTION_PROPERTY_SETTINGS=True)
    def test_share_tenant_isolation(self):
        other = Tenant.objects.create(name="Other Share", slug="other-share-api")
        foreign = Property.objects.create(tenant=other, name="Foreign", slug="foreign")
        response = self.client.post(
            f"/api/v1/reception/properties/{foreign.id}/settings/share/",
            data={
                "kind": "portal",
                "target": "reservation",
                "reservation_id": self.reservation.pk,
                "channel": "email",
            },
            content_type="application/json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "property_not_found")

    @override_settings(RECEPTION_PROPERTY_SETTINGS=False)
    def test_share_disabled_when_settings_off(self):
        response = self.client.post(
            self.share_url,
            data={
                "kind": "portal",
                "target": "reservation",
                "reservation_id": self.reservation.pk,
                "channel": "email",
            },
            content_type="application/json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "settings_section_not_available")
