"""Period gate for ePorezna exports — source fiscal data availability."""

from __future__ import annotations

from apps.billing.models import ForeignServiceInvoice
from apps.tenants.models import Tenant


def has_source_fiscal_data(*, tenant: Tenant, period: str) -> bool:
    """Return whether any source fiscal data exists for the tax period.

    PR1 source: ``ForeignServiceInvoice``. Future ledgers / manual entries
    should extend this helper without changing builder wording.
    """
    return ForeignServiceInvoice.objects.filter(
        tenant=tenant,
        tax_period=period,
    ).exists()
