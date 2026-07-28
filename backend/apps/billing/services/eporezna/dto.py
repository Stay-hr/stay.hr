from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class ParsedForeignServiceInvoice:
    """Parser output — provider-agnostic; no Booking-specific fields."""

    provider: str
    supplier_name: str
    supplier_country: str
    supplier_vat_id: str
    invoice_number: str
    invoice_date: date
    tax_period: str
    period_from: date
    period_to: date
    taxable_amount: Decimal
    currency: str = "EUR"
    raw_fields: dict[str, Any] = field(default_factory=dict)
