"""Welcome template IntegrationConfig merge + validation (Phase 3).

Registry SoT lives in ``welcome_template``; this module owns merge-only seeds
and config-map checks (startup, admin, verify).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping

from django.core.exceptions import ImproperlyConfigured

from apps.integrations.whatsapp.welcome_template import (
    DEFAULT_WELCOME_HEADER_IMAGE,
    DEFAULT_WELCOME_TEMPLATES,
    META_APPROVED_LANGUAGES,
    WELCOME_TEMPLATE_REGISTRY,
    _extract_welcome_map,
)

logger = logging.getLogger(__name__)

_TEMPLATE_NAME_PREFIX = "stay_welcome_"


@dataclass(frozen=True)
class WelcomeMapIssue:
    level: str  # "error" | "warning"
    message: str


@dataclass
class WelcomeMapInspection:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    configured_keys: frozenset[str] = frozenset()
    missing_registry_keys: frozenset[str] = frozenset()

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass
class WelcomeTemplateValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    configs_checked: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors


def default_welcome_map() -> dict[str, str]:
    """Full registry-derived welcome map for new configs / merge fill."""
    return {key: name for key, name in DEFAULT_WELCOME_TEMPLATES.items()}


def default_whatsapp_templates_block() -> dict[str, Any]:
    return {
        "header_image_url": DEFAULT_WELCOME_HEADER_IMAGE,
        "welcome": default_welcome_map(),
    }


def merge_welcome_map(
    existing: Mapping[str, Any] | None,
) -> tuple[dict[str, str], list[str]]:
    """Merge registry defaults into an existing welcome map.

    Never overwrites a non-empty custom value. Returns ``(merged, added_keys)``.
    """
    merged: dict[str, str] = {}
    if existing:
        for key, value in existing.items():
            name = str(value or "").strip()
            if not name:
                continue
            merged[str(key).strip().lower()] = name

    added: list[str] = []
    for key, template_name in DEFAULT_WELCOME_TEMPLATES.items():
        current = str(merged.get(key) or "").strip()
        if current:
            continue
        merged[key] = template_name
        added.append(key)
    return merged, added


def merge_welcome_templates_into_config(
    config: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Merge registry welcome langs into ``config['whatsapp_templates']``.

    Preserves ``header_image_url`` and any other unrelated keys. Mutates a copy.
    """
    out = dict(config)
    templates_cfg = out.get("whatsapp_templates")
    if not isinstance(templates_cfg, dict):
        templates_cfg = {}
    else:
        templates_cfg = dict(templates_cfg)

    welcome_raw = templates_cfg.get("welcome")
    welcome_existing: Mapping[str, Any] | None
    if isinstance(welcome_raw, dict):
        welcome_existing = welcome_raw
    else:
        welcome_existing = None

    merged_welcome, added = merge_welcome_map(welcome_existing)
    templates_cfg["welcome"] = merged_welcome
    if not str(templates_cfg.get("header_image_url") or "").strip():
        templates_cfg["header_image_url"] = DEFAULT_WELCOME_HEADER_IMAGE
    out["whatsapp_templates"] = templates_cfg
    return out, added


def inspect_welcome_map(
    welcome: Any,
    *,
    label: str = "welcome",
) -> WelcomeMapInspection:
    """Inspect a ``whatsapp_templates.welcome`` map for hard/soft issues."""
    errors: list[str] = []
    warnings: list[str] = []

    if welcome is None:
        return WelcomeMapInspection(
            warnings=[f"{label}: missing welcome map (resolver uses registry DEFAULT)"],
            missing_registry_keys=META_APPROVED_LANGUAGES,
        )

    if not isinstance(welcome, dict):
        errors.append(f"{label}: welcome must be a JSON object, got {type(welcome).__name__}")
        return WelcomeMapInspection(errors=errors)

    configured: set[str] = set()
    seen_names: dict[str, str] = {}
    for raw_key, raw_value in welcome.items():
        key = str(raw_key).strip()
        if not key:
            errors.append(f"{label}: empty language key")
            continue
        if key != key.lower():
            errors.append(f"{label}: language key must be lowercase, got {key!r}")
        norm_key = key.lower()
        if not isinstance(raw_value, str):
            errors.append(
                f"{label}[{norm_key!r}]: template name must be a string, "
                f"got {type(raw_value).__name__}"
            )
            continue
        name = raw_value.strip()
        if not name:
            errors.append(f"{label}[{norm_key!r}]: empty template name")
            continue
        configured.add(norm_key)
        if not name.startswith(_TEMPLATE_NAME_PREFIX):
            warnings.append(
                f"{label}[{norm_key!r}]: template {name!r} does not start with "
                f"{_TEMPLATE_NAME_PREFIX!r} (custom override?)"
            )
        prev = seen_names.get(name)
        if prev is not None and prev != norm_key:
            errors.append(
                f"{label}: duplicate template_name {name!r} for keys "
                f"{prev!r} and {norm_key!r}"
            )
        else:
            seen_names[name] = norm_key

    missing = frozenset(META_APPROVED_LANGUAGES - configured)
    if missing:
        warnings.append(
            f"{label}: missing registry languages in config "
            f"(resolver still uses DEFAULT): {', '.join(sorted(missing))}"
        )
    for required in ("en", "hr"):
        if required not in configured:
            warnings.append(
                f"{label}: config map missing {required!r} "
                "(registry DEFAULT still covers it)"
            )

    return WelcomeMapInspection(
        errors=errors,
        warnings=warnings,
        configured_keys=frozenset(configured),
        missing_registry_keys=missing,
    )


def inspect_integration_welcome_config(
    config: Mapping[str, Any] | None,
    *,
    label: str,
) -> WelcomeMapInspection:
    if not config:
        return inspect_welcome_map(None, label=label)
    templates_cfg = config.get("whatsapp_templates")
    if templates_cfg is None:
        return inspect_welcome_map(None, label=label)
    if not isinstance(templates_cfg, dict):
        return WelcomeMapInspection(
            errors=[
                f"{label}: whatsapp_templates must be a JSON object, "
                f"got {type(templates_cfg).__name__}"
            ]
        )
    return inspect_welcome_map(templates_cfg.get("welcome"), label=label)


def validate_welcome_templates(
    *,
    raise_on_error: bool = True,
) -> WelcomeTemplateValidationReport:
    """Validate live WhatsApp IntegrationConfig welcome maps at startup.

    Registry integrity is already enforced at import of ``welcome_template``.
    Hard config errors → ``ImproperlyConfigured`` when ``raise_on_error``.
    Soft issues (missing langs, custom names) → WARNING logs only.
    """
    report = WelcomeTemplateValidationReport()

    # Belt-and-suspenders: registry must still look sound.
    if not WELCOME_TEMPLATE_REGISTRY:
        report.errors.append("WELCOME_TEMPLATE_REGISTRY is empty")
    for required in ("en", "hr"):
        if required not in WELCOME_TEMPLATE_REGISTRY:
            report.errors.append(f"WELCOME_TEMPLATE_REGISTRY missing {required!r}")
    ua = WELCOME_TEMPLATE_REGISTRY.get("ua")
    if ua is not None and ua.meta_language != "uk":
        report.errors.append(
            f"WELCOME_TEMPLATE_REGISTRY['ua'].meta_language must be 'uk', "
            f"got {ua.meta_language!r}"
        )

    try:
        from apps.integrations.models import IntegrationConfig

        rows = IntegrationConfig.objects.filter(
            provider=IntegrationConfig.Provider.WHATSAPP,
            is_active=True,
        ).select_related("tenant", "property")
    except Exception as exc:  # noqa: BLE001 — DB may be unavailable during migrate
        report.warnings.append(f"skipped IntegrationConfig scan: {exc}")
        _emit_report(report)
        if raise_on_error and report.errors:
            raise ImproperlyConfigured(
                "Welcome template validation failed: " + "; ".join(report.errors)
            )
        return report

    for row in rows.iterator():
        report.configs_checked += 1
        tenant_slug = getattr(row.tenant, "slug", "?")
        prop = getattr(row.property, "slug", None) if row.property_id else None
        label = f"IntegrationConfig(id={row.pk}, tenant={tenant_slug}"
        if prop:
            label += f", property={prop}"
        label += ")"
        try:
            config = row.get_config_dict()
        except Exception as exc:  # noqa: BLE001
            # Stale/debug rows with bad ciphertext must not brick process boot.
            report.warnings.append(f"{label}: failed to read config: {exc}")
            continue
        inspection = inspect_integration_welcome_config(config, label=label)
        report.errors.extend(inspection.errors)
        report.warnings.extend(inspection.warnings)

    _emit_report(report)
    if raise_on_error and report.errors:
        raise ImproperlyConfigured(
            "Welcome template validation failed: " + "; ".join(report.errors)
        )
    return report


def _emit_report(report: WelcomeTemplateValidationReport) -> None:
    for warning in report.warnings:
        logger.warning("welcome_templates_validation %s", warning)
    for error in report.errors:
        logger.error("welcome_templates_validation %s", error)
    if report.ok and not report.warnings:
        logger.info(
            "welcome_templates_validation ok configs_checked=%s registry=%s",
            report.configs_checked,
            len(WELCOME_TEMPLATE_REGISTRY),
        )


def welcome_templates_health_snapshot() -> dict[str, Any]:
    """Health block for ``system/status`` → ``messaging.welcome_templates``.

    Aggregates active WhatsApp IntegrationConfig welcome maps against the
    registry SoT. Fail-soft: DB errors become ``status=warning`` with ``error``.
    """
    registry_count = len(WELCOME_TEMPLATE_REGISTRY)
    snapshot: dict[str, Any] = {
        "registry_count": registry_count,
        "configured": 0,
        "missing_in_config": [],
        "status": "healthy",
        "configs_checked": 0,
    }

    if registry_count == 0 or "en" not in WELCOME_TEMPLATE_REGISTRY:
        snapshot["status"] = "critical"
        snapshot["reason"] = "registry_invalid"
        return snapshot

    try:
        from apps.integrations.models import IntegrationConfig

        rows = IntegrationConfig.objects.filter(
            provider=IntegrationConfig.Provider.WHATSAPP,
            is_active=True,
        ).select_related("tenant", "property")
    except Exception as exc:  # noqa: BLE001 — status must stay available
        snapshot["status"] = "warning"
        snapshot["error"] = str(exc)[:200]
        snapshot["reason"] = "scan_unavailable"
        return snapshot

    missing_union: set[str] = set()
    hard_errors = 0
    min_configured: int | None = None

    for row in rows.iterator():
        snapshot["configs_checked"] += 1
        tenant_slug = getattr(row.tenant, "slug", "?")
        prop = getattr(row.property, "slug", None) if row.property_id else None
        label = f"IntegrationConfig(id={row.pk}, tenant={tenant_slug}"
        if prop:
            label += f", property={prop}"
        label += ")"
        try:
            config = row.get_config_dict()
        except Exception as exc:  # noqa: BLE001
            hard_errors += 1
            snapshot.setdefault("read_errors", []).append(
                f"{label}: {str(exc)[:120]}"
            )
            continue
        inspection = inspect_integration_welcome_config(config, label=label)
        if inspection.errors:
            hard_errors += len(inspection.errors)
        missing_union |= set(inspection.missing_registry_keys)
        configured_count = len(inspection.configured_keys & META_APPROVED_LANGUAGES)
        if min_configured is None or configured_count < min_configured:
            min_configured = configured_count

    if snapshot["configs_checked"] == 0:
        # No live WhatsApp rows — registry alone is healthy enough for status.
        snapshot["configured"] = registry_count
        snapshot["missing_in_config"] = []
        snapshot["status"] = "healthy"
        snapshot["reason"] = "no_whatsapp_configs"
        return snapshot

    missing_sorted = sorted(missing_union)
    snapshot["missing_in_config"] = missing_sorted
    snapshot["configured"] = (
        min_configured if min_configured is not None else 0
    )

    if hard_errors:
        snapshot["status"] = "critical"
        snapshot["reason"] = "config_errors"
    elif missing_sorted:
        snapshot["status"] = "warning"
        snapshot["reason"] = "missing_langs"
    else:
        snapshot["status"] = "healthy"
        snapshot["reason"] = None

    return snapshot


def merge_welcome_templates_for_row(row) -> list[str]:
    """Merge registry welcome langs into one IntegrationConfig; save if changed."""
    config = dict(row.get_config_dict())
    merged, added = merge_welcome_templates_into_config(config)
    if not added:
        return []
    row.set_config_dict(merged)
    row.save(
        update_fields=["config_encrypted", "config", "updated_at"],
    )
    return added


def merge_welcome_templates_all(*, dry_run: bool = False) -> list[dict[str, Any]]:
    """Merge welcome maps on all active WhatsApp IntegrationConfig rows."""
    from apps.integrations.models import IntegrationConfig

    results: list[dict[str, Any]] = []
    rows = IntegrationConfig.objects.filter(
        provider=IntegrationConfig.Provider.WHATSAPP,
        is_active=True,
    ).select_related("tenant", "property")
    for row in rows.iterator():
        try:
            config = dict(row.get_config_dict())
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "merge_welcome_templates skip id=%s read_failed: %s",
                row.pk,
                exc,
            )
            results.append(
                {
                    "id": row.pk,
                    "tenant": getattr(row.tenant, "slug", None),
                    "property_id": row.property_id,
                    "added": [],
                    "welcome_keys": [],
                    "skipped": str(exc),
                }
            )
            continue
        _merged, added = merge_welcome_templates_into_config(config)
        entry = {
            "id": row.pk,
            "tenant": getattr(row.tenant, "slug", None),
            "property_id": row.property_id,
            "added": added,
            "welcome_keys": sorted(_extract_welcome_map(_merged).keys()),
        }
        if added and not dry_run:
            row.set_config_dict(_merged)
            row.save(update_fields=["config_encrypted", "config", "updated_at"])
        results.append(entry)
    return results
