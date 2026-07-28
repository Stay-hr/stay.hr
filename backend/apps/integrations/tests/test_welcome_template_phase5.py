"""Phase 5 — matrix / override / regional / snapshot / architecture tests.

E2E send path (mocked Meta → stay_welcome_pl + pl) lives in
``apps.communications.tests.test_whatsapp_autocheckin``.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from django.test import SimpleTestCase

from apps.integrations.whatsapp.welcome_template import (
    DEFAULT_WELCOME_TEMPLATES,
    META_APPROVED_LANGUAGES,
    ResolutionMatch,
    ResolutionSource,
    WELCOME_TEMPLATE_REGISTRY,
    resolve_welcome_template,
)
from apps.integrations.whatsapp.welcome_template_config import (
    default_welcome_map,
    default_whatsapp_templates_block,
    merge_welcome_map,
)

BACKEND_ROOT = Path(__file__).resolve().parents[3]
APPS_ROOT = BACKEND_ROOT / "apps"

# Production modules that send welcome templates (must use resolve_welcome_template).
WELCOME_SEND_MODULES = (
    APPS_ROOT / "communications" / "whatsapp_autocheckin_tasks.py",
    APPS_ROOT / "communications" / "guest_message_send.py",
    APPS_ROOT / "communications" / "guest_message_whatsapp_v2.py",
    APPS_ROOT / "integrations" / "whatsapp" / "operator_arrival_confirm.py",
)

LEGACY_HELPER_NAMES = frozenset(
    {
        "welcome_template_name",
        "welcome_meta_language_code",
    }
)

# Frozen registry contract — bump intentionally when adding a Meta-approved lang.
REGISTRY_SNAPSHOT = {
    "cs": {"template_name": "stay_welcome_cs", "meta_language": "cs"},
    "de": {"template_name": "stay_welcome_de", "meta_language": "de"},
    "en": {"template_name": "stay_welcome_en", "meta_language": "en"},
    "es": {"template_name": "stay_welcome_es", "meta_language": "es"},
    "fr": {"template_name": "stay_welcome_fr", "meta_language": "fr"},
    "hr": {"template_name": "stay_welcome_hr", "meta_language": "hr"},
    "hu": {"template_name": "stay_welcome_hu", "meta_language": "hu"},
    "it": {"template_name": "stay_welcome_it", "meta_language": "it"},
    "lt": {"template_name": "stay_welcome_lt", "meta_language": "lt"},
    "nl": {"template_name": "stay_welcome_nl", "meta_language": "nl"},
    "pl": {"template_name": "stay_welcome_pl", "meta_language": "pl"},
    "ro": {"template_name": "stay_welcome_ro", "meta_language": "ro"},
    "sk": {"template_name": "stay_welcome_sk", "meta_language": "sk"},
    "ua": {"template_name": "stay_welcome_ua", "meta_language": "uk"},
}

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


def _iter_production_py_files() -> list[Path]:
    files: list[Path] = []
    for path in APPS_ROOT.rglob("*.py"):
        parts = path.parts
        if "tests" in parts or "migrations" in parts:
            continue
        files.append(path)
    return files


def _collect_name_refs(source: str) -> set[str]:
    """Collect Name / Attribute identifiers referenced in a module."""
    tree = ast.parse(source)
    names: set[str] = set()

    class Visitor(ast.NodeVisitor):
        def visit_Name(self, node: ast.Name) -> None:
            names.add(node.id)
            self.generic_visit(node)

        def visit_Attribute(self, node: ast.Attribute) -> None:
            names.add(node.attr)
            self.generic_visit(node)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            for alias in node.names:
                names.add(alias.name)
                if alias.asname:
                    names.add(alias.asname)
            self.generic_visit(node)

    Visitor().visit(tree)
    return names


def _module_calls_resolve(source: str) -> bool:
    return "resolve_welcome_template" in _collect_name_refs(source)


class WelcomeTemplateMatrixTests(SimpleTestCase):
    """Parametrized matrix — every Meta-approved lang via registry DEFAULT."""

    def test_all_meta_approved_langs_resolve_via_default(self):
        for key in sorted(META_APPROVED_LANGUAGES):
            with self.subTest(language=key):
                definition = WELCOME_TEMPLATE_REGISTRY[key]
                resolved = resolve_welcome_template(language=key)
                self.assertEqual(resolved.template_name, definition.template_name)
                self.assertEqual(resolved.meta_language, definition.meta_language)
                self.assertEqual(resolved.requested_language, key)
                self.assertEqual(resolved.resolved_language, key)
                self.assertEqual(resolved.source, ResolutionSource.DEFAULT)
                self.assertEqual(resolved.match, ResolutionMatch.EXACT)

    def test_matrix_with_incomplete_platform_config(self):
        """Missing map entries still hit registry DEFAULT (not english mismatch)."""
        for key in sorted(META_APPROVED_LANGUAGES):
            with self.subTest(language=key):
                definition = WELCOME_TEMPLATE_REGISTRY[key]
                resolved = resolve_welcome_template(
                    language=key,
                    platform_config=INCOMPLETE_WELCOME_CONFIG,
                )
                self.assertEqual(resolved.template_name, definition.template_name)
                self.assertEqual(resolved.meta_language, definition.meta_language)
                if key in INCOMPLETE_WELCOME_CONFIG["whatsapp_templates"]["welcome"]:
                    self.assertEqual(resolved.source, ResolutionSource.PLATFORM)
                else:
                    self.assertEqual(resolved.source, ResolutionSource.DEFAULT)
                self.assertEqual(resolved.match, ResolutionMatch.EXACT)


class WelcomeTemplateOverrideTests(SimpleTestCase):
    """Property → Platform → DEFAULT hierarchy."""

    def test_property_beats_platform_and_default(self):
        property_config = {
            "whatsapp_templates": {"welcome": {"de": "property_welcome_de"}}
        }
        platform_config = {
            "whatsapp_templates": {"welcome": {"de": "platform_welcome_de"}}
        }
        resolved = resolve_welcome_template(
            language="de",
            property_config=property_config,
            platform_config=platform_config,
        )
        self.assertEqual(resolved.template_name, "property_welcome_de")
        self.assertEqual(resolved.meta_language, "de")
        self.assertEqual(resolved.source, ResolutionSource.PROPERTY)
        self.assertEqual(resolved.match, ResolutionMatch.EXACT)

    def test_platform_beats_default(self):
        platform_config = {
            "whatsapp_templates": {"welcome": {"hu": "platform_welcome_hu"}}
        }
        resolved = resolve_welcome_template(
            language="hu",
            platform_config=platform_config,
        )
        self.assertEqual(resolved.template_name, "platform_welcome_hu")
        self.assertEqual(resolved.source, ResolutionSource.PLATFORM)
        self.assertEqual(resolved.match, ResolutionMatch.EXACT)

    def test_default_when_maps_omit_lang(self):
        resolved = resolve_welcome_template(
            language="lt",
            property_config=INCOMPLETE_WELCOME_CONFIG,
            platform_config=INCOMPLETE_WELCOME_CONFIG,
        )
        self.assertEqual(resolved.template_name, "stay_welcome_lt")
        self.assertEqual(resolved.meta_language, "lt")
        self.assertEqual(resolved.source, ResolutionSource.DEFAULT)

    def test_english_fallback_unknown_lang(self):
        resolved = resolve_welcome_template(language="sv")
        self.assertEqual(resolved.template_name, "stay_welcome_en")
        self.assertEqual(resolved.meta_language, "en")
        self.assertEqual(resolved.resolved_language, "en")
        self.assertEqual(resolved.source, ResolutionSource.ENGLISH)
        self.assertEqual(resolved.match, ResolutionMatch.EXACT)


class WelcomeTemplateRegionalTests(SimpleTestCase):
    """Regional tags: exact key first, then base language."""

    def test_en_us_base_match(self):
        resolved = resolve_welcome_template(language="en-US")
        self.assertEqual(resolved.template_name, "stay_welcome_en")
        self.assertEqual(resolved.meta_language, "en")
        self.assertEqual(resolved.resolved_language, "en")
        self.assertEqual(resolved.match, ResolutionMatch.BASE)
        self.assertEqual(resolved.source, ResolutionSource.DEFAULT)

    def test_en_gb_base_match(self):
        resolved = resolve_welcome_template(language="en_GB")
        self.assertEqual(resolved.template_name, "stay_welcome_en")
        self.assertEqual(resolved.resolved_language, "en")
        self.assertEqual(resolved.match, ResolutionMatch.BASE)

    def test_pl_pl_base_match(self):
        resolved = resolve_welcome_template(language="pl_PL")
        self.assertEqual(resolved.template_name, "stay_welcome_pl")
        self.assertEqual(resolved.meta_language, "pl")
        self.assertEqual(resolved.resolved_language, "pl")
        self.assertEqual(resolved.match, ResolutionMatch.BASE)
        self.assertEqual(resolved.source, ResolutionSource.DEFAULT)

    def test_uk_maps_to_ua(self):
        resolved = resolve_welcome_template(language="uk")
        self.assertEqual(resolved.template_name, "stay_welcome_ua")
        self.assertEqual(resolved.meta_language, "uk")
        self.assertEqual(resolved.resolved_language, "ua")
        self.assertEqual(resolved.match, ResolutionMatch.EXACT)

    def test_regional_property_exact_wins_before_base(self):
        property_config = {
            "whatsapp_templates": {
                "welcome": {
                    "en-us": "custom_welcome_en_us",
                    "en": "custom_welcome_en",
                }
            }
        }
        resolved = resolve_welcome_template(
            language="en-US",
            property_config=property_config,
        )
        self.assertEqual(resolved.template_name, "custom_welcome_en_us")
        self.assertEqual(resolved.resolved_language, "en-us")
        self.assertEqual(resolved.source, ResolutionSource.PROPERTY)
        self.assertEqual(resolved.match, ResolutionMatch.EXACT)

    def test_pt_br_falls_back_to_english(self):
        resolved = resolve_welcome_template(language="pt-BR")
        self.assertEqual(resolved.template_name, "stay_welcome_en")
        self.assertEqual(resolved.meta_language, "en")
        self.assertEqual(resolved.source, ResolutionSource.ENGLISH)
        self.assertEqual(resolved.match, ResolutionMatch.EXACT)


class WelcomeTemplateSnapshotTests(SimpleTestCase):
    """Seed merge payload keys/values stay locked to the registry SoT."""

    def test_registry_matches_frozen_snapshot(self):
        live = {
            key: {
                "template_name": defn.template_name,
                "meta_language": defn.meta_language,
            }
            for key, defn in WELCOME_TEMPLATE_REGISTRY.items()
        }
        self.assertEqual(
            live,
            REGISTRY_SNAPSHOT,
            msg=(
                "WELCOME_TEMPLATE_REGISTRY diverged from Phase 5 snapshot; "
                "update REGISTRY_SNAPSHOT intentionally when adding Meta langs."
            ),
        )

    def test_default_welcome_templates_match_registry(self):
        self.assertEqual(
            dict(DEFAULT_WELCOME_TEMPLATES),
            {k: v["template_name"] for k, v in REGISTRY_SNAPSHOT.items()},
        )

    def test_default_welcome_map_matches_registry_keys(self):
        merged = default_welcome_map()
        self.assertEqual(set(merged.keys()), set(WELCOME_TEMPLATE_REGISTRY.keys()))
        self.assertEqual(set(merged.keys()), set(META_APPROVED_LANGUAGES))
        for key, name in merged.items():
            self.assertEqual(name, WELCOME_TEMPLATE_REGISTRY[key].template_name)

    def test_merge_empty_equals_registry(self):
        merged, added = merge_welcome_map({})
        self.assertEqual(set(merged.keys()), set(WELCOME_TEMPLATE_REGISTRY.keys()))
        self.assertEqual(sorted(added), sorted(WELCOME_TEMPLATE_REGISTRY.keys()))
        for key, name in merged.items():
            self.assertEqual(name, WELCOME_TEMPLATE_REGISTRY[key].template_name)

    def test_seed_templates_block_welcome_keys_match_registry(self):
        block = default_whatsapp_templates_block()
        welcome = block["welcome"]
        self.assertEqual(set(welcome.keys()), set(WELCOME_TEMPLATE_REGISTRY.keys()))
        # Stable JSON shape for CI diffs when keys drift.
        self.assertEqual(
            json.dumps(sorted(welcome.keys())),
            json.dumps(sorted(REGISTRY_SNAPSHOT.keys())),
        )


class WelcomeTemplateArchitectureTests(SimpleTestCase):
    """Guardrails: no split helpers; welcome send sites use the resolver."""

    def test_legacy_helpers_absent_from_production(self):
        violations: list[str] = []
        welcome_mod = (
            APPS_ROOT / "integrations" / "whatsapp" / "welcome_template.py"
        ).resolve()
        for path in _iter_production_py_files():
            if path.resolve() == welcome_mod:
                continue
            source = path.read_text(encoding="utf-8")
            refs = _collect_name_refs(source) & LEGACY_HELPER_NAMES
            if refs:
                rel = path.relative_to(BACKEND_ROOT)
                violations.append(f"{rel}: {sorted(refs)}")
        self.assertEqual(
            violations,
            [],
            msg="Legacy welcome helpers must not appear outside welcome_template.py",
        )

    def test_welcome_send_modules_call_resolve(self):
        missing: list[str] = []
        for path in WELCOME_SEND_MODULES:
            self.assertTrue(path.is_file(), msg=f"missing module: {path}")
            source = path.read_text(encoding="utf-8")
            if not _module_calls_resolve(source):
                missing.append(str(path.relative_to(BACKEND_ROOT)))
        self.assertEqual(
            missing,
            [],
            msg="Welcome send modules must call resolve_welcome_template",
        )

    def test_welcome_send_modules_use_resolved_meta_language(self):
        """Call sites must pass resolved.meta_language (not guest lang alone)."""
        missing: list[str] = []
        for path in WELCOME_SEND_MODULES:
            source = path.read_text(encoding="utf-8")
            if "resolved.meta_language" not in source:
                missing.append(str(path.relative_to(BACKEND_ROOT)))
        self.assertEqual(
            missing,
            [],
            msg="Welcome send must use resolved.meta_language with template_name",
        )

    def test_welcome_send_modules_use_resolved_template_name(self):
        missing: list[str] = []
        for path in WELCOME_SEND_MODULES:
            source = path.read_text(encoding="utf-8")
            if "resolved.template_name" not in source:
                missing.append(str(path.relative_to(BACKEND_ROOT)))
        self.assertEqual(
            missing,
            [],
            msg="Welcome send must use resolved.template_name",
        )
