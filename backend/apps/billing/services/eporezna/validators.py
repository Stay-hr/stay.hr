from __future__ import annotations

import calendar
import re
from datetime import date

from apps.billing.models import ForeignServiceInvoice
from apps.billing.services.eporezna.dto import ParsedForeignServiceInvoice
from apps.billing.services.eporezna.errors import InvoiceValidationError

_TAX_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_KNOWN_PROVIDERS = {c.value for c in ForeignServiceInvoice.Provider}


class ForeignServiceInvoiceValidator:
    """Domain checks on parsed DTOs — parsers must not own this logic."""

    def validate(self, dto: ParsedForeignServiceInvoice) -> None:
        errors: list[str] = []

        if dto.provider not in _KNOWN_PROVIDERS:
            errors.append(f"Unknown provider: {dto.provider!r}")

        if not dto.supplier_name.strip():
            errors.append("supplier_name is required")

        country = (dto.supplier_country or "").strip().upper()
        if len(country) != 2 or not country.isalpha():
            errors.append("supplier_country must be ISO 3166-1 alpha-2")

        if not (dto.supplier_vat_id or "").strip():
            errors.append("supplier_vat_id is required")

        if not (dto.invoice_number or "").strip():
            errors.append("invoice_number is required")

        if dto.invoice_date is None:
            errors.append("invoice_date is required")

        if not _TAX_PERIOD_RE.match(dto.tax_period or ""):
            errors.append("tax_period must be YYYY-MM")

        if dto.period_from is None or dto.period_to is None:
            errors.append("period_from and period_to are required")
        elif dto.period_from > dto.period_to:
            errors.append("period_from must be <= period_to")
        elif _TAX_PERIOD_RE.match(dto.tax_period or ""):
            year, month = int(dto.tax_period[:4]), int(dto.tax_period[5:7])
            expected_from = date(year, month, 1)
            expected_to = date(year, month, calendar.monthrange(year, month)[1])
            if dto.period_from != expected_from or dto.period_to != expected_to:
                # Allow Booking periods that are the calendar month; warn via error if mismatch
                if dto.period_from.year != year or dto.period_from.month != month:
                    errors.append("tax_period must match period_from month")

        if dto.taxable_amount is None or dto.taxable_amount <= 0:
            errors.append("taxable_amount must be > 0")

        if not (dto.currency or "").strip():
            errors.append("currency is required")

        if errors:
            raise InvoiceValidationError("; ".join(errors))
