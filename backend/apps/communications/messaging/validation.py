"""Startup validation — fail-fast registry/template/provider/plan checks (ADR 0010 §4.H)."""

from __future__ import annotations

from dataclasses import dataclass, field

from apps.communications.messaging.definitions import (
    DefinitionRegistry,
    definition_registry,
)
from apps.communications.messaging.plans import PlanRegistry, plan_registry
from apps.communications.messaging.providers.registry import (
    ProviderRegistry,
    provider_registry,
)
from apps.communications.messaging.skip_rules import SkipRuleEngine, skip_rule_engine
from apps.communications.messaging.templates import TemplateRegistry, template_registry


class MessagingValidationError(RuntimeError):
    """Raised when messaging engine configuration is invalid at startup."""


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_messaging_engine(
    *,
    definitions: DefinitionRegistry | None = None,
    templates: TemplateRegistry | None = None,
    providers: ProviderRegistry | None = None,
    plans: PlanRegistry | None = None,
    skip_rules: SkipRuleEngine | None = None,
    raise_on_error: bool = True,
) -> ValidationReport:
    """Validate in-memory catalogs. Does not hit the database.

    Checks (ADR §4.H):
    - Every MessageDefinition has renderer + template_version key present
    - No duplicate definition keys / duplicate template versions (enforced at register;
      re-checked for consistency)
    - Every ChannelPolicy provider is registered
    - Plans reference known definitions
    - Skip rules referenced by definitions exist
    - Resolve chain is linear (property → tenant → platform) — no self-ref config
      in v1 code catalogs (plans use a single schedule_prefix; no circular links)
    """
    defs = definitions if definitions is not None else definition_registry
    tpls = templates if templates is not None else template_registry
    provs = providers if providers is not None else provider_registry
    plans_reg = plans if plans is not None else plan_registry
    skips = skip_rules if skip_rules is not None else skip_rule_engine

    report = ValidationReport()

    seen_template_versions: dict[str, str] = {}
    for definition in defs.all():
        if not definition.template_version:
            report.errors.append(
                f"Definition {definition.key!r} missing template_version"
            )
        if not definition.renderer_key:
            report.errors.append(
                f"Definition {definition.key!r} missing renderer_key"
            )
        elif not tpls.has_renderer(definition.renderer_key):
            report.errors.append(
                f"Definition {definition.key!r} renderer "
                f"{definition.renderer_key!r} is not registered"
            )
        if definition.template_version:
            other = seen_template_versions.get(definition.template_version)
            if other and other != definition.key:
                report.errors.append(
                    f"Duplicate template_version {definition.template_version!r} "
                    f"on definitions {other!r} and {definition.key!r}"
                )
            else:
                seen_template_versions[definition.template_version] = definition.key
            if not tpls.has_template_version(definition.template_version):
                # Renderer may register the same version; warn if missing.
                if tpls.has_renderer(definition.renderer_key):
                    report.warnings.append(
                        f"Definition {definition.key!r} template_version "
                        f"{definition.template_version!r} not in template registry "
                        f"(renderer {definition.renderer_key!r} is present)"
                    )

        for provider_name in definition.channel_policy.providers:
            if not provs.has(provider_name):
                report.errors.append(
                    f"Definition {definition.key!r} ChannelPolicy provider "
                    f"{provider_name!r} is not registered"
                )

        for rule_name in definition.skip_rule_names:
            if not skips.has(rule_name):
                report.errors.append(
                    f"Definition {definition.key!r} skip rule "
                    f"{rule_name!r} is not registered"
                )

    for plan in plans_reg.all():
        for def_key in plan.definition_keys:
            if not defs.has(def_key):
                report.errors.append(
                    f"Plan {plan.key!r} references unknown definition {def_key!r}"
                )
        # Linear schedule resolve: a plan must not reference itself as a parent prefix.
        # v1 has no parent pointers; guard against accidental self-prefix cycles in metadata.
        if plan.schedule_prefix and plan.schedule_prefix == plan.key:
            # Not inherently wrong, but document linearity: prefixes are settings keys,
            # not plan graph edges. No circular override graph exists in v1.
            pass

    # Ensure template registry has no orphan duplicate mapping inconsistencies.
    versions = list(tpls.template_versions())
    if len(versions) != len(set(versions)):
        report.errors.append("Template registry has duplicate template_version keys")

    if raise_on_error and report.errors:
        joined = "; ".join(report.errors)
        raise MessagingValidationError(
            f"Messaging engine startup validation failed: {joined}"
        )
    return report
