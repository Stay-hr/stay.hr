"""Verify WhatsApp welcome template config maps vs registry (and optional Meta)."""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from apps.integrations.models import IntegrationConfig
from apps.integrations.whatsapp.welcome_template import (
    META_APPROVED_LANGUAGES,
    WELCOME_TEMPLATE_REGISTRY,
    _extract_welcome_map,
)
from apps.integrations.whatsapp.welcome_template_config import (
    inspect_integration_welcome_config,
)


class Command(BaseCommand):
    help = (
        "Verify whatsapp_templates.welcome maps against WELCOME_TEMPLATE_REGISTRY. "
        "Optional --live-meta checks APPROVED status via Graph API."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant-slug",
            default="",
            help="Limit to one tenant slug (default: all active WhatsApp configs).",
        )
        parser.add_argument(
            "--live-meta",
            action="store_true",
            help="Also list Meta message templates and compare name+language.",
        )
        parser.add_argument(
            "--fail-on-missing",
            action="store_true",
            help="Exit non-zero if any registry lang is missing from a config map.",
        )

    def handle(self, *args, **options):
        qs = IntegrationConfig.objects.filter(
            provider=IntegrationConfig.Provider.WHATSAPP,
            is_active=True,
        ).select_related("tenant", "property")
        tenant_slug = str(options["tenant_slug"] or "").strip()
        if tenant_slug:
            qs = qs.filter(tenant__slug=tenant_slug)

        rows = list(qs)
        if not rows:
            self.stderr.write(self.style.WARNING("No matching WhatsApp IntegrationConfig."))
            return

        self.stdout.write(
            f"Registry: {len(WELCOME_TEMPLATE_REGISTRY)} langs "
            f"({', '.join(sorted(META_APPROVED_LANGUAGES))})"
        )

        meta_index: dict[tuple[str, str], dict[str, Any]] | None = None
        if options["live_meta"]:
            meta_index = self._load_meta_index(rows[0])

        any_missing = False
        any_errors = False
        for row in rows:
            label = f"id={row.pk} tenant={row.tenant.slug}"
            if row.property_id:
                label += f" property={row.property.slug}"
            try:
                config = row.get_config_dict()
            except Exception as exc:  # noqa: BLE001
                self.stdout.write("")
                self.stdout.write(self.style.NOTICE(f"=== {label} ==="))
                self.stdout.write(self.style.WARNING(f"  SKIP: failed to read config: {exc}"))
                continue
            welcome = _extract_welcome_map(config)
            inspection = inspect_integration_welcome_config(config, label=label)

            self.stdout.write("")
            self.stdout.write(self.style.NOTICE(f"=== {label} ==="))
            for lang in sorted(META_APPROVED_LANGUAGES):
                defn = WELCOME_TEMPLATE_REGISTRY[lang]
                configured = welcome.get(lang, "")
                if configured:
                    mark = "✓"
                    detail = configured
                    if configured != defn.template_name:
                        detail += f" (custom; registry default={defn.template_name})"
                else:
                    mark = "✗"
                    detail = f"missing (resolver DEFAULT → {defn.template_name})"
                    any_missing = True
                meta_note = ""
                if meta_index is not None:
                    key = (defn.template_name, defn.meta_language)
                    item = meta_index.get(key)
                    if item is None:
                        meta_note = " | Meta: NOT FOUND"
                        any_errors = True
                    else:
                        status = str(item.get("status") or "?")
                        meta_note = f" | Meta: {status}"
                        if status.upper() != "APPROVED":
                            any_errors = True
                style = self.style.SUCCESS if mark == "✓" else self.style.WARNING
                self.stdout.write(
                    style(f"  {mark} {lang}: {detail} [meta_lang={defn.meta_language}]{meta_note}")
                )

            for err in inspection.errors:
                any_errors = True
                self.stdout.write(self.style.ERROR(f"  ERROR: {err}"))
            for warn in inspection.warnings:
                self.stdout.write(self.style.WARNING(f"  WARN: {warn}"))

        self.stdout.write("")
        if any_errors or (options["fail_on_missing"] and any_missing):
            self.stderr.write(self.style.ERROR("verify_whatsapp_templates: FAILED"))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("verify_whatsapp_templates: OK"))

    def _load_meta_index(
        self, sample_row: IntegrationConfig
    ) -> dict[tuple[str, str], dict[str, Any]]:
        from apps.integrations.whatsapp.integration_lookup import (
            resolve_whatsapp_integration,
        )
        from apps.integrations.whatsapp.meta_templates import (
            MetaTemplateApiError,
            list_message_templates,
        )

        _integration, runtime = resolve_whatsapp_integration(sample_row.tenant)
        if runtime is None:
            self.stderr.write(
                self.style.ERROR("Cannot resolve WhatsApp runtime for --live-meta")
            )
            raise SystemExit(2)
        waba_id = runtime.effective_waba_id()
        if not waba_id:
            self.stderr.write(self.style.ERROR("waba_id required for --live-meta"))
            raise SystemExit(2)
        try:
            items = list_message_templates(
                waba_id=waba_id,
                access_token=runtime.access_token,
            )
        except MetaTemplateApiError as exc:
            self.stderr.write(self.style.ERROR(f"Meta list failed: {exc}"))
            raise SystemExit(2) from exc

        index: dict[tuple[str, str], dict[str, Any]] = {}
        for item in items:
            name = str(item.get("name") or "").strip()
            language = str(item.get("language") or "").strip()
            if name and language:
                index[(name, language)] = item
        self.stdout.write(f"Meta templates loaded: {len(index)} name/language pairs")
        return index
