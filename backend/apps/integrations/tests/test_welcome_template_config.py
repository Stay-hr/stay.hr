from __future__ import annotations

from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase

from apps.integrations.models import IntegrationConfig
from apps.integrations.whatsapp.welcome_template import (
    DEFAULT_WELCOME_TEMPLATES,
    META_APPROVED_LANGUAGES,
    WELCOME_TEMPLATE_REGISTRY,
)
from apps.integrations.whatsapp.welcome_template_config import (
    default_welcome_map,
    inspect_welcome_map,
    merge_welcome_map,
    merge_welcome_templates_all,
    merge_welcome_templates_into_config,
    validate_welcome_templates,
    welcome_templates_health_snapshot,
)
from apps.tenants.models import Tenant


INCOMPLETE_WELCOME = {
    "hr": "stay_welcome_hr",
    "en": "stay_welcome_en",
    "de": "stay_welcome_de",
    "es": "stay_welcome_es",
    "fr": "stay_welcome_fr",
    "it": "stay_welcome_it",
}


class MergeWelcomeMapTests(SimpleTestCase):
    def test_merge_fills_missing_registry_langs(self):
        merged, added = merge_welcome_map(INCOMPLETE_WELCOME)
        self.assertIn("pl", added)
        self.assertEqual(merged["pl"], "stay_welcome_pl")
        self.assertEqual(set(merged.keys()), set(META_APPROVED_LANGUAGES))
        # Existing values preserved
        self.assertEqual(merged["hr"], "stay_welcome_hr")

    def test_merge_never_overwrites_custom(self):
        existing = {**INCOMPLETE_WELCOME, "pl": "custom_welcome_pl"}
        merged, added = merge_welcome_map(existing)
        self.assertNotIn("pl", added)
        self.assertEqual(merged["pl"], "custom_welcome_pl")

    def test_merge_skips_empty_existing_values(self):
        existing = {**INCOMPLETE_WELCOME, "pl": "  "}
        merged, added = merge_welcome_map(existing)
        self.assertIn("pl", added)
        self.assertEqual(merged["pl"], "stay_welcome_pl")

    def test_merge_into_config_preserves_unrelated_keys(self):
        config = {
            "phone_number_id": "123",
            "waba_id": "waba",
            "whatsapp_templates": {
                "header_image_url": "https://example.com/h.png",
                "welcome": dict(INCOMPLETE_WELCOME),
            },
        }
        merged, added = merge_welcome_templates_into_config(config)
        self.assertEqual(merged["phone_number_id"], "123")
        self.assertEqual(merged["waba_id"], "waba")
        self.assertEqual(
            merged["whatsapp_templates"]["header_image_url"],
            "https://example.com/h.png",
        )
        self.assertIn("pl", added)
        self.assertEqual(
            set(merged["whatsapp_templates"]["welcome"].keys()),
            set(DEFAULT_WELCOME_TEMPLATES.keys()),
        )

    def test_default_welcome_map_matches_registry(self):
        self.assertEqual(default_welcome_map(), dict(DEFAULT_WELCOME_TEMPLATES))
        self.assertEqual(
            set(default_welcome_map().keys()),
            set(WELCOME_TEMPLATE_REGISTRY.keys()),
        )


class InspectWelcomeMapTests(SimpleTestCase):
    def test_empty_value_is_error(self):
        inspection = inspect_welcome_map({"pl": ""})
        self.assertFalse(inspection.ok)
        self.assertTrue(any("empty" in e for e in inspection.errors))

    def test_uppercase_key_is_error(self):
        inspection = inspect_welcome_map({"PL": "stay_welcome_pl"})
        self.assertFalse(inspection.ok)

    def test_custom_name_is_warning_not_error(self):
        inspection = inspect_welcome_map(
            {**INCOMPLETE_WELCOME, "pl": "custom_welcome_pl"}
        )
        self.assertTrue(inspection.ok)
        self.assertTrue(any("custom_welcome_pl" in w for w in inspection.warnings))

    def test_missing_registry_langs_are_warnings(self):
        inspection = inspect_welcome_map(INCOMPLETE_WELCOME)
        self.assertTrue(inspection.ok)
        self.assertIn("pl", inspection.missing_registry_keys)
        self.assertTrue(any("missing registry" in w for w in inspection.warnings))


class ValidateAndMergeIntegrationTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Welcome Config Test",
            slug="welcome-config-test",
        )
        self.row = IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.WHATSAPP,
            routing_key="phone-test-1",
            is_active=True,
        )
        self.row.set_config_dict(
            {
                "phone_number_id": "phone-test-1",
                "whatsapp_templates": {
                    "header_image_url": "https://stay.hr/static/whatsapp-header.png",
                    "welcome": dict(INCOMPLETE_WELCOME),
                },
            }
        )
        self.row.save()

    def test_validate_incomplete_map_warns_not_raises(self):
        report = validate_welcome_templates(raise_on_error=True)
        self.assertTrue(report.ok)
        self.assertGreaterEqual(report.configs_checked, 1)
        self.assertTrue(any("missing registry" in w for w in report.warnings))

    def test_validate_empty_template_raises(self):
        config = self.row.get_config_dict()
        config["whatsapp_templates"]["welcome"]["pl"] = ""
        self.row.set_config_dict(config)
        self.row.save()
        with self.assertRaises(ImproperlyConfigured):
            validate_welcome_templates(raise_on_error=True)

    def test_merge_all_adds_pl_and_preserves_custom(self):
        config = self.row.get_config_dict()
        config["whatsapp_templates"]["welcome"]["hu"] = "custom_welcome_hu"
        self.row.set_config_dict(config)
        self.row.save()

        results = merge_welcome_templates_all(dry_run=False)
        mine = next(r for r in results if r["id"] == self.row.pk)
        self.assertIn("pl", mine["added"])
        self.assertNotIn("hu", mine["added"])

        self.row.refresh_from_db()
        welcome = self.row.get_config_dict()["whatsapp_templates"]["welcome"]
        self.assertEqual(welcome["pl"], "stay_welcome_pl")
        self.assertEqual(welcome["hu"], "custom_welcome_hu")
        self.assertEqual(set(welcome.keys()), set(META_APPROVED_LANGUAGES))


class WelcomeTemplatesHealthSnapshotTests(TestCase):
    """Phase 4: system/status messaging.welcome_templates health block."""

    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Welcome Health Test",
            slug="welcome-health-test",
        )
        self.row = IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.WHATSAPP,
            routing_key="phone-health-1",
            is_active=True,
        )

    def _snapshot_for_row(self) -> dict:
        qs = IntegrationConfig.objects.filter(pk=self.row.pk)
        with patch(
            "apps.integrations.models.IntegrationConfig.objects.filter",
            return_value=qs,
        ):
            return welcome_templates_health_snapshot()

    def test_incomplete_map_is_warning_with_missing(self):
        self.row.set_config_dict(
            {
                "phone_number_id": "phone-health-1",
                "whatsapp_templates": {
                    "header_image_url": "https://stay.hr/static/whatsapp-header.png",
                    "welcome": dict(INCOMPLETE_WELCOME),
                },
            }
        )
        self.row.save()

        snap = self._snapshot_for_row()
        self.assertEqual(snap["registry_count"], len(WELCOME_TEMPLATE_REGISTRY))
        self.assertEqual(snap["configs_checked"], 1)
        self.assertEqual(snap["status"], "warning")
        self.assertEqual(snap["reason"], "missing_langs")
        self.assertIn("pl", snap["missing_in_config"])
        self.assertLess(snap["configured"], snap["registry_count"])

    def test_full_map_is_healthy(self):
        self.row.set_config_dict(
            {
                "phone_number_id": "phone-health-1",
                "whatsapp_templates": {
                    "header_image_url": "https://stay.hr/static/whatsapp-header.png",
                    "welcome": default_welcome_map(),
                },
            }
        )
        self.row.save()

        snap = self._snapshot_for_row()
        self.assertEqual(snap["registry_count"], len(META_APPROVED_LANGUAGES))
        self.assertEqual(snap["configs_checked"], 1)
        self.assertEqual(snap["status"], "healthy")
        self.assertEqual(snap["missing_in_config"], [])
        self.assertEqual(snap["configured"], len(META_APPROVED_LANGUAGES))

    def test_empty_template_name_is_critical(self):
        welcome = default_welcome_map()
        welcome["pl"] = ""
        self.row.set_config_dict(
            {
                "phone_number_id": "phone-health-1",
                "whatsapp_templates": {
                    "header_image_url": "https://stay.hr/static/whatsapp-header.png",
                    "welcome": welcome,
                },
            }
        )
        self.row.save()

        snap = self._snapshot_for_row()
        self.assertEqual(snap["status"], "critical")
        self.assertEqual(snap["reason"], "config_errors")
