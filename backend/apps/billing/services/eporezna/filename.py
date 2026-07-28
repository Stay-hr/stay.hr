"""Shared ePorezna download filename builder."""

from __future__ import annotations

from apps.billing.services.eporezna.period import FiscalPeriod


def build_filename(*, form: str, oib: str, period: FiscalPeriod) -> str:
    """Return ``{form}_{oib}_{YYYYMMDD-YYYYMMDD}.xml`` (e.g. PDV / PDV-S)."""
    return f"{form}_{oib}_{period.filename_range}.xml"
