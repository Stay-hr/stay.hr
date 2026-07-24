"""MESSAGE_ORCHESTRATION_* rollout flags + allowlists (ADR 0010 Phase 6).

Decision order:

1. ``MESSAGE_ORCHESTRATION_ENABLED`` — master switch
2. Tenant / property allowlists (fail-closed when both empty)
3. ``MESSAGE_ORCHESTRATION_SHADOW`` — materialize/plan only; no provider send

Phase 7 live cutover: ``suppress_legacy_automated_outbound`` is True only for
allowlisted scope in **live** mode (enabled, not shadow). Wired into
``GuestReminderService`` (pre-arrival + D0) and legacy WhatsApp welcome /
intro entry points; engine provider adapters still call send primitives.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Sequence

from django.conf import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrchestrationDecision:
    """Whether orchestration applies to a tenant/property scope."""

    allowed: bool
    enabled: bool
    shadow: bool
    block_reason: str | None = None
    tenant_slug: str | None = None
    property_slug: str | None = None
    mode: str = "disabled"  # disabled | shadow | live

    @property
    def live_send(self) -> bool:
        """True when the engine may call providers for this scope."""
        return self.allowed and self.enabled and not self.shadow

    @property
    def materialize(self) -> bool:
        """True when TIME materialization may create planned rows."""
        return self.allowed and self.enabled


@dataclass(frozen=True)
class OrchestrationScope:
    """One materialize/process unit resolved from allowlists."""

    tenant_id: int
    tenant_slug: str
    property_id: int | None = None
    property_slug: str | None = None


@dataclass(frozen=True)
class OrchestrationRuntime:
    """Installation-level flag snapshot (no DB)."""

    enabled: bool
    shadow: bool
    tenant_slugs: frozenset[str]
    property_tokens: frozenset[str]
    block_reason: str | None = None

    @property
    def mode(self) -> str:
        if not self.enabled:
            return "disabled"
        if self.shadow:
            return "shadow"
        return "live"


def _normalize_token(value: object) -> str:
    return str(value or "").strip().lower()


def _normalized_list(raw: object) -> frozenset[str]:
    if raw is None:
        return frozenset()
    if isinstance(raw, str):
        parts: Sequence[str] = raw.split(",")
    elif isinstance(raw, (list, tuple, set, frozenset)):
        parts = [str(x) for x in raw]
    else:
        parts = [str(raw)]
    return frozenset(_normalize_token(p) for p in parts if _normalize_token(p))


def orchestration_runtime() -> OrchestrationRuntime:
    """Read flags from Django settings (no I/O)."""
    enabled = bool(getattr(settings, "MESSAGE_ORCHESTRATION_ENABLED", False))
    shadow = bool(getattr(settings, "MESSAGE_ORCHESTRATION_SHADOW", False))
    tenants = _normalized_list(
        getattr(settings, "MESSAGE_ORCHESTRATION_TENANTS", [])
    )
    properties = _normalized_list(
        getattr(settings, "MESSAGE_ORCHESTRATION_PROPERTIES", [])
    )
    block: str | None = None
    if not enabled:
        block = "orchestration_disabled"
    elif not tenants and not properties:
        block = "allowlist_empty"
    return OrchestrationRuntime(
        enabled=enabled,
        shadow=shadow,
        tenant_slugs=tenants,
        property_tokens=properties,
        block_reason=block,
    )


def _parse_property_token(
    token: str,
) -> tuple[str | None, str | None, int | None]:
    """Return ``(tenant_slug, property_slug, property_id)`` from an allowlist token.

    Accepted forms: ``123`` (id), ``prop-slug``, ``tenant:prop-slug``.
    """
    if token.isdigit():
        return None, None, int(token)
    if ":" in token:
        left, right = token.split(":", 1)
        left, right = left.strip(), right.strip()
        if left and right:
            return left, right, None
    return None, token, None


def orchestration_decision(
    *,
    tenant_slug: str | None = None,
    property_slug: str | None = None,
    property_id: int | None = None,
    runtime: OrchestrationRuntime | None = None,
) -> OrchestrationDecision:
    """Central allowlist filter — decision only (no DB).

    Rules:

    - Master off → not allowed (``orchestration_disabled``)
    - Both allowlists empty → fail-closed (``allowlist_empty``)
    - If ``PROPERTIES`` non-empty: property must match id/slug (and tenant when
      token or TENANTS constrains it)
    - Else ``TENANTS`` non-empty: tenant slug must match
    """
    rt = runtime or orchestration_runtime()
    norm_tenant = _normalize_token(tenant_slug) or None
    norm_property = _normalize_token(property_slug) or None

    if not rt.enabled:
        return OrchestrationDecision(
            allowed=False,
            enabled=False,
            shadow=rt.shadow,
            block_reason="orchestration_disabled",
            tenant_slug=norm_tenant,
            property_slug=norm_property,
            mode="disabled",
        )

    if not rt.tenant_slugs and not rt.property_tokens:
        return OrchestrationDecision(
            allowed=False,
            enabled=True,
            shadow=rt.shadow,
            block_reason="allowlist_empty",
            tenant_slug=norm_tenant,
            property_slug=norm_property,
            mode="disabled",
        )

    if rt.property_tokens:
        matched = False
        for token in rt.property_tokens:
            tok_tenant, tok_slug, tok_id = _parse_property_token(token)
            if tok_id is not None:
                if property_id is not None and int(property_id) == tok_id:
                    matched = True
                    break
                continue
            if tok_slug and norm_property and tok_slug == norm_property:
                if tok_tenant and norm_tenant and tok_tenant != norm_tenant:
                    continue
                if tok_tenant and not norm_tenant:
                    continue
                matched = True
                break
        if not matched:
            return OrchestrationDecision(
                allowed=False,
                enabled=True,
                shadow=rt.shadow,
                block_reason="property_not_allowed",
                tenant_slug=norm_tenant,
                property_slug=norm_property,
                mode="disabled",
            )
        # Optional extra tenant gate when TENANTS is also set.
        if rt.tenant_slugs and norm_tenant and norm_tenant not in rt.tenant_slugs:
            return OrchestrationDecision(
                allowed=False,
                enabled=True,
                shadow=rt.shadow,
                block_reason="tenant_not_allowed",
                tenant_slug=norm_tenant,
                property_slug=norm_property,
                mode="disabled",
            )
    else:
        # Tenant allowlist only.
        if not norm_tenant or norm_tenant not in rt.tenant_slugs:
            return OrchestrationDecision(
                allowed=False,
                enabled=True,
                shadow=rt.shadow,
                block_reason="tenant_not_allowed",
                tenant_slug=norm_tenant,
                property_slug=norm_property,
                mode="disabled",
            )

    mode = "shadow" if rt.shadow else "live"
    return OrchestrationDecision(
        allowed=True,
        enabled=True,
        shadow=rt.shadow,
        block_reason=None,
        tenant_slug=norm_tenant,
        property_slug=norm_property,
        mode=mode,
    )


def orchestration_decision_for_reservation(reservation) -> OrchestrationDecision:
    """Resolve decision from a Reservation (or duck-typed) instance."""
    tenant = getattr(reservation, "tenant", None)
    prop = getattr(reservation, "property", None)
    tenant_slug = getattr(tenant, "slug", None) if tenant is not None else None
    if tenant_slug is None:
        tenant_slug = getattr(reservation, "tenant_slug", None)
    property_slug = getattr(prop, "slug", None) if prop is not None else None
    property_id = getattr(reservation, "property_id", None)
    if property_id is None and prop is not None:
        property_id = getattr(prop, "pk", None)
    return orchestration_decision(
        tenant_slug=tenant_slug,
        property_slug=property_slug,
        property_id=property_id,
    )


def suppress_legacy_automated_outbound(
    *,
    tenant_slug: str | None = None,
    property_slug: str | None = None,
    property_id: int | None = None,
    reservation=None,
) -> bool:
    """Phase 7 hook: True when engine owns live automated outbound for this scope.

    Shadow mode keeps legacy senders active (engine plans only).
    """
    if reservation is not None:
        decision = orchestration_decision_for_reservation(reservation)
    else:
        decision = orchestration_decision(
            tenant_slug=tenant_slug,
            property_slug=property_slug,
            property_id=property_id,
        )
    return decision.live_send


def resolve_allowlisted_scopes(
    *,
    runtime: OrchestrationRuntime | None = None,
) -> list[OrchestrationScope]:
    """Resolve DB scopes for the Celery orchestration cycle.

    Returns an empty list when disabled or allowlist is empty / unresolved.
    """
    rt = runtime or orchestration_runtime()
    if not rt.enabled:
        return []
    if not rt.tenant_slugs and not rt.property_tokens:
        return []

    from apps.properties.models import Property
    from apps.tenants.models import Tenant

    scopes: list[OrchestrationScope] = []
    seen: set[tuple[int, int | None]] = set()

    def _add(
        *,
        tenant_id: int,
        tenant_slug: str,
        property_id: int | None = None,
        property_slug: str | None = None,
    ) -> None:
        key = (tenant_id, property_id)
        if key in seen:
            return
        seen.add(key)
        scopes.append(
            OrchestrationScope(
                tenant_id=tenant_id,
                tenant_slug=tenant_slug,
                property_id=property_id,
                property_slug=property_slug,
            )
        )

    if rt.property_tokens:
        for token in sorted(rt.property_tokens):
            tok_tenant, tok_slug, tok_id = _parse_property_token(token)
            qs = Property.objects.select_related("tenant")
            if tok_id is not None:
                qs = qs.filter(pk=tok_id)
            elif tok_slug:
                qs = qs.filter(slug__iexact=tok_slug)
                if tok_tenant:
                    qs = qs.filter(tenant__slug__iexact=tok_tenant)
            else:
                continue
            if rt.tenant_slugs:
                qs = qs.filter(tenant__slug__in=rt.tenant_slugs)
            for prop in qs:
                decision = orchestration_decision(
                    tenant_slug=prop.tenant.slug,
                    property_slug=prop.slug,
                    property_id=prop.pk,
                    runtime=rt,
                )
                if not decision.allowed:
                    continue
                _add(
                    tenant_id=prop.tenant_id,
                    tenant_slug=prop.tenant.slug,
                    property_id=prop.pk,
                    property_slug=prop.slug,
                )
        return scopes

    # Tenant allowlist → whole-tenant scopes (all properties).
    tenants = Tenant.objects.filter(slug__in=rt.tenant_slugs)
    for tenant in tenants:
        decision = orchestration_decision(
            tenant_slug=tenant.slug,
            runtime=rt,
        )
        if not decision.allowed:
            continue
        _add(tenant_id=tenant.pk, tenant_slug=tenant.slug, property_id=None)
    return scopes


def flags_health_snapshot() -> dict:
    """Compact flag state for messaging health / status endpoints."""
    rt = orchestration_runtime()
    return {
        "enabled": rt.enabled,
        "shadow": rt.shadow,
        "mode": rt.mode,
        "tenants": sorted(rt.tenant_slugs),
        "properties": sorted(rt.property_tokens),
        "block_reason": rt.block_reason,
    }


def filter_reservations_by_allowlist(
    reservations: Iterable,
    *,
    runtime: OrchestrationRuntime | None = None,
) -> list:
    """Keep reservations whose tenant/property passes the allowlist."""
    rt = runtime or orchestration_runtime()
    kept = []
    for reservation in reservations:
        tenant = getattr(reservation, "tenant", None)
        prop = getattr(reservation, "property", None)
        decision = orchestration_decision(
            tenant_slug=getattr(tenant, "slug", None),
            property_slug=getattr(prop, "slug", None),
            property_id=getattr(reservation, "property_id", None)
            or getattr(prop, "pk", None),
            runtime=rt,
        )
        if decision.materialize:
            kept.append(reservation)
    return kept
