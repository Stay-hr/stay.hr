"""Map ForeignServiceInvoice rows to PDV-S form lines.

This is the sole place that implements mapping rules from stored invoices
to Obrazac PDV-S Isporuke amounts. The builder only serializes ``PDVSLine``.

Invariant: same invoice set → same ordered lines (stable sort by country, vat_id)
so RedBr assignment in the builder is deterministic.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from apps.billing.models import ForeignServiceInvoice

TWOPLACES = Decimal("0.01")
ZERO = Decimal("0.00")


@dataclass(frozen=True)
class PDVSLine:
    """Domain line for one EU supplier in a tax period (no XML)."""

    country_code: str
    vat_id: str
    goods_amount: Decimal
    services_amount: Decimal


def normalize_country_code(raw: str) -> str:
    return (raw or "").strip().upper()


def normalize_vat_id(raw: str, *, country_code: str = "") -> str:
    """Return VAT id without country prefix (e.g. NL8057… → 8057…)."""
    value = (raw or "").strip().upper().replace(" ", "")
    cc = normalize_country_code(country_code)
    if len(value) >= 2 and value[:2].isalpha():
        prefix = value[:2]
        if (cc and prefix == cc) or not cc:
            value = value[2:]
    return value


class PDVSLineMapper:
    """Sole source of truth for ForeignServiceInvoice → PDVSLine rules.

    Works only from invoice fields (country, VAT, amount). No Booking-specific
    branches — new foreign suppliers reuse this without touching the XML layer.
    """

    def map(self, invoices: Iterable[ForeignServiceInvoice]) -> list[PDVSLine]:
        """Aggregate invoices by normalized (country, vat_id).

        Current ForeignServiceInvoice rows are reverse-charge **services**:
        goods_amount=0, services_amount=taxable_amount.
        """
        goods: dict[tuple[str, str], Decimal] = defaultdict(lambda: Decimal("0"))
        services: dict[tuple[str, str], Decimal] = defaultdict(lambda: Decimal("0"))

        for inv in invoices:
            country = normalize_country_code(inv.supplier_country)
            vat_id = normalize_vat_id(inv.supplier_vat_id, country_code=country)
            if not country or not vat_id:
                continue
            key = (country, vat_id)
            amount = Decimal(inv.taxable_amount)
            # Services → I2; goods (I1) stay zero until a future field distinguishes them.
            services[key] += amount
            goods[key] += ZERO

        keys = sorted(set(goods) | set(services))
        lines: list[PDVSLine] = []
        for country, vat_id in keys:
            lines.append(
                PDVSLine(
                    country_code=country,
                    vat_id=vat_id,
                    goods_amount=goods[(country, vat_id)].quantize(TWOPLACES),
                    services_amount=services[(country, vat_id)].quantize(TWOPLACES),
                )
            )
        return lines

    def totals(self, lines: Iterable[PDVSLine]) -> tuple[Decimal, Decimal]:
        goods = sum((line.goods_amount for line in lines), ZERO).quantize(TWOPLACES)
        services = sum((line.services_amount for line in lines), ZERO).quantize(TWOPLACES)
        return goods, services


def map_invoices_to_pdvs_lines(
    invoices: Iterable[ForeignServiceInvoice],
) -> list[PDVSLine]:
    """Convenience wrapper around ``PDVSLineMapper.map``."""
    return PDVSLineMapper().map(invoices)


def totals_from_lines(lines: Iterable[PDVSLine]) -> tuple[Decimal, Decimal]:
    return PDVSLineMapper().totals(lines)
