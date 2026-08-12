"""eVisitor guest eligibility and age helpers.

Every tourist guest requires eVisitor registration. Date-of-birth completeness is
enforced by GuestValidator / build_check_in_payload, not here.

TTPaymentCategory for minors uses official age bands on reservation.check_in;
adults keep the property config default.
"""

from __future__ import annotations

from datetime import date

from apps.reservations.models import Guest

# Official eVisitor TTPaymentCategory codes for standard age bands.
TT_PAYMENT_CATEGORY_UNDER_12 = "1"
TT_PAYMENT_CATEGORY_12_TO_17 = "2"


def _age_on(reference: date, dob: date) -> int:
    years = reference.year - dob.year
    if (reference.month, reference.day) < (dob.month, dob.day):
        years -= 1
    return years


def guest_requires_evisitor(guest: Guest, *, reference_date: date | None = None) -> bool:
    """True for every guest — children and adults must be registered in eVisitor.

    ``guest`` / ``reference_date`` remain for call-site compatibility; age no longer
    gates eligibility. Missing DOB still requires eVisitor (validation happens later).
    """
    return True


def tt_payment_category_for_dob(
    dob: date,
    *,
    reference_date: date,
    default_payment_category: str,
) -> str:
    """Map DOB to TTPaymentCategory using age on ``reference_date`` (check-in).

    Minors override to official codes ``1`` / ``2``. Adults keep
    ``default_payment_category`` unchanged.
    """
    age = _age_on(reference_date, dob)
    if age < 12:
        return TT_PAYMENT_CATEGORY_UNDER_12
    if age < 18:
        return TT_PAYMENT_CATEGORY_12_TO_17
    return (default_payment_category or "").strip()
