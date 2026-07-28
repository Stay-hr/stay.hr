"""PDV-S fiscal readiness — single source of truth for required TenantFiscalSettings."""

from __future__ import annotations

from dataclasses import dataclass

from apps.billing.models import TenantFiscalSettings
from apps.tenants.models import Tenant

REQUIRED_FIELDS: tuple[str, ...] = (
    "issuer_oib",
    "issuer_first_name",
    "issuer_last_name",
    "issuer_place",
    "issuer_street",
    "tax_office_code",
    "default_preparer",
)


@dataclass(frozen=True)
class ReadinessResult:
    configured: bool
    missing: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "configured": self.configured,
            "missing": list(self.missing),
            "warnings": list(self.warnings),
        }


def fiscal_pdvs_readiness(tenant: Tenant) -> ReadinessResult:
    """Return whether the tenant can export Obrazac PDV-S Zaglavlje."""
    settings = (
        TenantFiscalSettings.objects.filter(tenant=tenant)
        .select_related("default_preparer")
        .first()
    )
    missing: list[str] = []
    if settings is None:
        return ReadinessResult(configured=False, missing=REQUIRED_FIELDS)

    if not (settings.issuer_oib or "").strip():
        missing.append("issuer_oib")
    if not (settings.issuer_first_name or "").strip():
        missing.append("issuer_first_name")
    if not (settings.issuer_last_name or "").strip():
        missing.append("issuer_last_name")
    if not (settings.issuer_place or "").strip():
        missing.append("issuer_place")
    if not (settings.issuer_street or "").strip():
        missing.append("issuer_street")
    if not (settings.tax_office_code or "").strip():
        missing.append("tax_office_code")
    if settings.default_preparer_id is None:
        missing.append("default_preparer")

    return ReadinessResult(
        configured=not missing,
        missing=tuple(missing),
    )
