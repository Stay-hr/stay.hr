"""Shared tax-period parsing for all ePorezna form builders."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class FiscalPeriod:
    """YYYY-MM tax period with inclusive calendar-month bounds.

    ``from_year_month`` is the sole parser — builders must not split period strings.
    """

    period: str
    date_from: date
    date_to: date

    @classmethod
    def from_year_month(cls, period: str) -> FiscalPeriod:
        try:
            year_s, month_s = period.split("-", 1)
            year, month = int(year_s), int(month_s)
            if month < 1 or month > 12:
                raise ValueError
            if len(year_s) != 4:
                raise ValueError
        except ValueError as exc:
            raise ValueError(f"Invalid period {period!r}; expected YYYY-MM") from exc
        start = date(year, month, 1)
        end = date(year, month, calendar.monthrange(year, month)[1])
        return cls(period=f"{year:04d}-{month:02d}", date_from=start, date_to=end)

    @property
    def filename_range(self) -> str:
        return (
            f"{self.date_from.strftime('%Y%m%d')}-"
            f"{self.date_to.strftime('%Y%m%d')}"
        )
