"""Shared fixture helpers for billing tests (tourist tax requires billable guests)."""

from __future__ import annotations

from datetime import date

from apps.reservations.models import Guest


def make_guest(
    *,
    tenant,
    reservation,
    first_name: str,
    last_name: str,
    date_of_birth: date | None = date(1990, 1, 1),
    nationality: str = "HR",
    sex: str = "male",
    **kwargs,
) -> Guest:
    """Create a billable guest for tourist-tax / invoice builders.

    Secondary guests need DOB + nationality + sex (or document_number), otherwise
    ``guests_for_checkout`` treats them as unfilled and drops them from tax calc.
    """
    return Guest.objects.create(
        tenant=tenant,
        reservation=reservation,
        first_name=first_name,
        last_name=last_name,
        date_of_birth=date_of_birth,
        nationality=nationality,
        sex=sex,
        **kwargs,
    )
