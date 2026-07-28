"""Map ForeignServiceInvoice rows to Obrazac PDV v11-0 amount fields.

Sole place that implements reverse-charge EU services → form amounts.
``PDVBuilder`` only serializes ``PDVAmounts``.

Mapping (ePorezna UI / paušalist without pretporez deduction):
- II.10 ``Podatak210`` — Primljene usluge iz EU po stopi 25%
- II UKUPNO ``Podatak200`` — same totals when only II.10 is filled
- III.10 ``Podatak310`` — pretporez stays 0 (no deduction)
- IV ``Podatak400`` — obveza za uplatu = II porez − III porez
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable

from apps.billing.models import ForeignServiceInvoice

TWOPLACES = Decimal("0.01")
ZERO = Decimal("0.00")
# Standard HR VAT rate for reverse-charge EU services on Obrazac PDV II.10.
EU_SERVICES_VAT_RATE = Decimal("0.25")


@dataclass(frozen=True)
class PDVAmounts:
    """Domain amounts for Obrazac PDV Tijelo (no XML)."""

    eu_services_base: Decimal
    eu_services_vat: Decimal
    payable: Decimal


class PDVAmountMapper:
    """Sole SoT: ForeignServiceInvoice taxable totals → PDV II.10 / IV."""

    def map(self, invoices: Iterable[ForeignServiceInvoice]) -> PDVAmounts:
        base = sum(
            (Decimal(inv.taxable_amount) for inv in invoices),
            ZERO,
        ).quantize(TWOPLACES, rounding=ROUND_HALF_UP)
        vat = (base * EU_SERVICES_VAT_RATE).quantize(
            TWOPLACES, rounding=ROUND_HALF_UP
        )
        return PDVAmounts(
            eu_services_base=base,
            eu_services_vat=vat,
            # Paušalist: no pretporez (III.10 = 0) → payable = VAT on II.10.
            payable=vat,
        )


def map_invoices_to_pdv_amounts(
    invoices: Iterable[ForeignServiceInvoice],
) -> PDVAmounts:
    return PDVAmountMapper().map(invoices)
