from __future__ import annotations

from apps.billing.models import Invoice
from apps.reservations.models import Reservation


def resolve_payment_method(reservation: Reservation) -> Invoice.PaymentMethod:
    provider = (reservation.payment_provider or "").lower()
    status = (reservation.payment_status or "").lower()
    source = (reservation.source or "").lower()

    if "booking" in provider or "booking" in source:
        return Invoice.PaymentMethod.BOOKING
    if "cash" in status or "gotov" in status:
        return Invoice.PaymentMethod.CASH
    if "card" in status or "kart" in status:
        return Invoice.PaymentMethod.CARD
    if "transfer" in status or "transak" in status:
        return Invoice.PaymentMethod.TRANSFER
    if reservation.payment_status:
        return Invoice.PaymentMethod.TRANSFER
    return Invoice.PaymentMethod.OTHER


def build_payment_note(reservation: Reservation, payment_method: Invoice.PaymentMethod) -> str:
    if payment_method == Invoice.PaymentMethod.BOOKING:
        provider = reservation.payment_provider or "Booking.com"
        return (
            f"Plaćeno karticom putem posrednika {provider}. "
            "Način plaćanja: KARTICE."
        )
    if payment_method == Invoice.PaymentMethod.CASH:
        return "Plaćeno gotovinom."
    if payment_method == Invoice.PaymentMethod.CARD:
        return "Plaćeno karticom."
    if payment_method == Invoice.PaymentMethod.TRANSFER:
        return "Plaćeno transakcijskim računom."
    return "Plaćeno."


def fisk1_payment_code(payment_method: Invoice.PaymentMethod) -> str:
    mapping = {
        Invoice.PaymentMethod.CASH: "G",
        Invoice.PaymentMethod.CARD: "K",
        Invoice.PaymentMethod.TRANSFER: "T",
        Invoice.PaymentMethod.BOOKING: "K",
        Invoice.PaymentMethod.OTHER: "O",
    }
    return mapping[payment_method]
