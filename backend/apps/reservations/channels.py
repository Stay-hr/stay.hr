"""Canonical reservation channel classification for reception APIs."""

from __future__ import annotations

from typing import Any

from apps.reservations.booking_lifecycle import WEB_BOOKING_SOURCE
from apps.reservations.channel_sync import (
    IMPORT_SOURCE_BOOKING_PDF,
    IMPORT_SOURCE_BOOKING_XLS,
    IMPORT_SOURCE_CHANNEX,
)

CHANNEL_OTHER = "other"
CHANNEL_BOOKING_COM = "booking_com"
CHANNEL_AIRBNB = "airbnb"
CHANNEL_EXPEDIA = "expedia"
CHANNEL_WEB = "web"
CHANNEL_RECEPTION = "reception"

TRANSPORT_CHANNEX = "channex"
TRANSPORT_DIRECT = "direct"
TRANSPORT_IMPORT = "import"

IMPORT_SOURCE_MANUAL = "manual"

_OTA_KEYS = {
    "booking.com": CHANNEL_BOOKING_COM,
    "booking": CHANNEL_BOOKING_COM,
    "airbnb": CHANNEL_AIRBNB,
    "expedia": CHANNEL_EXPEDIA,
}

_CHANNEL_LABELS = {
    CHANNEL_BOOKING_COM: "Booking.com",
    CHANNEL_AIRBNB: "Airbnb",
    CHANNEL_EXPEDIA: "Expedia",
    CHANNEL_WEB: "Web",
    CHANNEL_RECEPTION: "Reception",
}


def _normalize(value: str | None) -> str:
    return (value or "").strip().lower()


def _label_for(key: str, source: str, *, fallback: str) -> str:
    if key != CHANNEL_OTHER:
        return _CHANNEL_LABELS[key]
    if source:
        return source
    return fallback


def reservation_channel(reservation: Any) -> dict[str, str | None]:
    """Return the canonical channel payload for a reservation-like object.

    Shape: ``{"key", "label", "transport"}``. Transport is ``channex``,
    ``direct``, ``import``, or ``None``.
    """
    import_source = _normalize(getattr(reservation, "import_source", None))
    source = str(getattr(reservation, "source", None) or "").strip()
    source_key = source.lower()

    if import_source == IMPORT_SOURCE_CHANNEX:
        key = _OTA_KEYS.get(source_key, CHANNEL_OTHER)
        return {
            "key": key,
            "label": _label_for(key, source, fallback="Channex"),
            "transport": TRANSPORT_CHANNEX,
        }

    if import_source in (IMPORT_SOURCE_BOOKING_PDF, IMPORT_SOURCE_BOOKING_XLS):
        return {
            "key": CHANNEL_BOOKING_COM,
            "label": _CHANNEL_LABELS[CHANNEL_BOOKING_COM],
            "transport": TRANSPORT_IMPORT,
        }

    if import_source == IMPORT_SOURCE_MANUAL:
        return {
            "key": CHANNEL_RECEPTION,
            "label": _CHANNEL_LABELS[CHANNEL_RECEPTION],
            "transport": TRANSPORT_DIRECT,
        }

    if not import_source and source_key == WEB_BOOKING_SOURCE:
        return {
            "key": CHANNEL_WEB,
            "label": _CHANNEL_LABELS[CHANNEL_WEB],
            "transport": TRANSPORT_DIRECT,
        }

    return {
        "key": CHANNEL_OTHER,
        "label": _label_for(CHANNEL_OTHER, source, fallback="Other"),
        "transport": None,
    }
