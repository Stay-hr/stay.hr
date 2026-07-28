from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from apps.integrations.whatsapp import welcome_template as welcome_mod
from apps.integrations.whatsapp.welcome_template import (
    DEFAULT_WELCOME_TEMPLATES,
    META_APPROVED_LANGUAGES,
    ResolutionMatch,
    ResolutionSource,
    TemplateDefinition,
    WELCOME_TEMPLATE_REGISTRY,
    normalize_language,
    resolve_welcome_template,
)


INCOMPLETE_WELCOME_CONFIG = {
    "whatsapp_templates": {
        "welcome": {
            "hr": "stay_welcome_hr",
            "en": "stay_welcome_en",
            "de": "stay_welcome_de",
            "es": "stay_welcome_es",
            "fr": "stay_welcome_fr",
            "it": "stay_welcome_it",
        }
    }
}


class NormalizeLanguageTests(SimpleTestCase):
    def test_en_lowercase(self):
        self.assertEqual(normalize_language("en"), "en")

    def test_en_uppercase(self):
        self.assertEqual(normalize_language("EN"), "en")

    def test_en_us_underscore_preserves_region(self):
        self.assertEqual(normalize_language("en_US"), "en-us")

    def test_en_us_hyphen_preserves_region(self):
        self.assertEqual(normalize_language("en-US"), "en-us")

    def test_pl_pl(self):
        self.assertEqual(normalize_language("pl_PL"), "pl-pl")

    def test_pl_uppercase(self):
        self.assertEqual(normalize_language("PL"), "pl")

    def test_uk_maps_to_ua(self):
        self.assertEqual(normalize_language("uk"), "ua")

    def test_ua_unchanged(self):
        self.assertEqual(normalize_language("ua"), "ua")

    def test_none_defaults_to_en(self):
        self.assertEqual(normalize_language(None), "en")

    def test_empty_defaults_to_en(self):
        self.assertEqual(normalize_language(""), "en")

    def test_whitespace_defaults_to_en(self):
        self.assertEqual(normalize_language("  "), "en")


class WelcomeRegistryTests(SimpleTestCase):
    def test_registry_is_immutable_mapping(self):
        with self.assertRaises(TypeError):
            WELCOME_TEMPLATE_REGISTRY["xx"] = TemplateDefinition(  # type: ignore[index]
                "stay_welcome_xx", "xx"
            )

    def test_template_definition_is_frozen(self):
        definition = WELCOME_TEMPLATE_REGISTRY["pl"]
        with self.assertRaises(Exception):
            definition.template_name = "mutated"  # type: ignore[misc]

    def test_meta_approved_derived_from_registry(self):
        self.assertEqual(META_APPROVED_LANGUAGES, frozenset(WELCOME_TEMPLATE_REGISTRY.keys()))

    def test_expected_meta_keys(self):
        expected = {
            "cs",
            "de",
            "en",
            "es",
            "fr",
            "hr",
            "hu",
            "it",
            "lt",
            "nl",
            "pl",
            "ro",
            "sk",
            "ua",
        }
        self.assertEqual(set(WELCOME_TEMPLATE_REGISTRY.keys()), expected)

    def test_ua_meta_language_is_uk(self):
        self.assertEqual(WELCOME_TEMPLATE_REGISTRY["ua"].meta_language, "uk")

    def test_default_welcome_templates_derived(self):
        self.assertEqual(
            dict(DEFAULT_WELCOME_TEMPLATES),
            {
                key: defn.template_name
                for key, defn in WELCOME_TEMPLATE_REGISTRY.items()
            },
        )

    def test_validate_rejects_missing_en(self):
        bad = {
            "hr": TemplateDefinition("stay_welcome_hr", "hr"),
        }
        with self.assertRaises(ImproperlyConfigured):
            welcome_mod._validate_welcome_template_registry(bad)


class ResolveWelcomeTemplateTests(SimpleTestCase):
    def test_incomplete_config_resolves_pl_via_default(self):
        resolved = resolve_welcome_template(
            language="pl",
            platform_config=INCOMPLETE_WELCOME_CONFIG,
        )
        self.assertEqual(resolved.template_name, "stay_welcome_pl")
        self.assertEqual(resolved.meta_language, "pl")
        self.assertEqual(resolved.requested_language, "pl")
        self.assertEqual(resolved.resolved_language, "pl")
        self.assertEqual(resolved.source, ResolutionSource.DEFAULT)
        self.assertEqual(resolved.match, ResolutionMatch.EXACT)

    def test_never_returns_none(self):
        resolved = resolve_welcome_template(language="sv")
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.source, ResolutionSource.ENGLISH)
        self.assertEqual(resolved.match, ResolutionMatch.EXACT)
        self.assertEqual(resolved.template_name, "stay_welcome_en")
        self.assertEqual(resolved.meta_language, "en")
        self.assertEqual(resolved.resolved_language, "en")

    def test_property_override_wins(self):
        property_config = {
            "whatsapp_templates": {"welcome": {"pl": "custom_welcome_pl"}}
        }
        platform_config = {
            "whatsapp_templates": {"welcome": {"pl": "stay_welcome_pl"}}
        }
        resolved = resolve_welcome_template(
            language="pl",
            property_config=property_config,
            platform_config=platform_config,
        )
        self.assertEqual(resolved.template_name, "custom_welcome_pl")
        self.assertEqual(resolved.meta_language, "pl")
        self.assertEqual(resolved.source, ResolutionSource.PROPERTY)
        self.assertEqual(resolved.match, ResolutionMatch.EXACT)

    def test_platform_before_default(self):
        platform_config = {
            "whatsapp_templates": {"welcome": {"pl": "platform_welcome_pl"}}
        }
        resolved = resolve_welcome_template(
            language="pl",
            platform_config=platform_config,
        )
        self.assertEqual(resolved.template_name, "platform_welcome_pl")
        self.assertEqual(resolved.source, ResolutionSource.PLATFORM)
        self.assertEqual(resolved.match, ResolutionMatch.EXACT)

    def test_regional_exact_then_base(self):
        resolved = resolve_welcome_template(language="en-US")
        self.assertEqual(resolved.template_name, "stay_welcome_en")
        self.assertEqual(resolved.meta_language, "en")
        self.assertEqual(resolved.requested_language, "en-US")
        self.assertEqual(resolved.resolved_language, "en")
        self.assertEqual(resolved.source, ResolutionSource.DEFAULT)
        self.assertEqual(resolved.match, ResolutionMatch.BASE)

    def test_uk_resolves_ua_template(self):
        resolved = resolve_welcome_template(language="uk")
        self.assertEqual(resolved.template_name, "stay_welcome_ua")
        self.assertEqual(resolved.meta_language, "uk")
        self.assertEqual(resolved.resolved_language, "ua")
        self.assertEqual(resolved.source, ResolutionSource.DEFAULT)
        self.assertEqual(resolved.match, ResolutionMatch.EXACT)

    def test_empty_language_resolves_english_default(self):
        resolved = resolve_welcome_template(language="")
        self.assertEqual(resolved.template_name, "stay_welcome_en")
        self.assertEqual(resolved.meta_language, "en")
        self.assertEqual(resolved.resolved_language, "en")
        # Empty normalizes to en and hits registry DEFAULT (not ENGLISH fallback).
        self.assertEqual(resolved.source, ResolutionSource.DEFAULT)
        self.assertEqual(resolved.match, ResolutionMatch.EXACT)

    def test_none_language_resolves_english_default(self):
        resolved = resolve_welcome_template(language=None)
        self.assertEqual(resolved.requested_language, "")
        self.assertEqual(resolved.source, ResolutionSource.DEFAULT)
        self.assertEqual(resolved.resolved_language, "en")


class WelcomeTemplateResolvedLogTests(SimpleTestCase):
    """Phase 4: structured welcome_template_resolved log with source+match."""

    def test_default_resolve_logs_info_with_source_and_match(self):
        with self.assertLogs(
            "apps.integrations.whatsapp.welcome_template", level="INFO"
        ) as cm:
            resolve_welcome_template(language="pl")
        matching = [
            line
            for line in cm.output
            if "welcome_template_resolved" in line and "source=default" in line
        ]
        self.assertEqual(len(matching), 1)
        line = matching[0]
        self.assertIn("requested=pl", line)
        self.assertIn("resolved=pl", line)
        self.assertIn("match=exact", line)
        self.assertIn("template=stay_welcome_pl", line)
        self.assertIn("meta_language=pl", line)

    def test_english_fallback_logs_warning(self):
        with self.assertLogs(
            "apps.integrations.whatsapp.welcome_template", level="WARNING"
        ) as cm:
            resolve_welcome_template(language="sv")
        matching = [
            line
            for line in cm.output
            if "welcome_template_resolved" in line and "source=english" in line
        ]
        self.assertEqual(len(matching), 1)
        line = matching[0]
        self.assertIn("requested=sv", line)
        self.assertIn("resolved=en", line)
        self.assertIn("match=exact", line)
        self.assertIn("template=stay_welcome_en", line)

    def test_regional_base_match_logged(self):
        with self.assertLogs(
            "apps.integrations.whatsapp.welcome_template", level="INFO"
        ) as cm:
            resolve_welcome_template(language="en-US")
        matching = [
            line
            for line in cm.output
            if "welcome_template_resolved" in line and "match=base" in line
        ]
        self.assertEqual(len(matching), 1)
        self.assertIn("source=default", matching[0])


class LegacyHelpersRemovedTests(SimpleTestCase):
    """Phase 2: old public helpers are gone — callers use resolve_welcome_template."""

    def test_welcome_template_name_removed(self):
        self.assertFalse(hasattr(welcome_mod, "welcome_template_name"))

    def test_welcome_meta_language_code_removed(self):
        self.assertFalse(hasattr(welcome_mod, "welcome_meta_language_code"))
