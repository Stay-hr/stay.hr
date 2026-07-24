"""Shared placeholder engine for portal render + channel compose (ADR 0008)."""

from __future__ import annotations

from string import Formatter
from typing import Any, Mapping


PLACEHOLDER_KEYS = (
    "guest_name",
    "property_name",
    "room_name",
    "room_code",
    "key_label",
    "checkin_date",
    "checkout_date",
    "wifi_ssid",
    "wifi_password",
)

# Safe samples for property-scoped preview (no reservation).
SAMPLE_PLACEHOLDERS: dict[str, str] = {
    "guest_name": "Guest",
    "property_name": "Property",
    "room_name": "Room",
    "room_code": "R1",
    "key_label": "1",
    "checkin_date": "2026-07-15",
    "checkout_date": "2026-07-18",
    "wifi_ssid": "WiFi",
    "wifi_password": "********",
}


class _SafeFormatDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def apply_placeholders(template: str, context: Mapping[str, Any] | None = None) -> str:
    """Format ``template`` with known placeholders; unknown keys stay as ``{key}``."""
    if not template:
        return ""
    ctx = {key: str((context or {}).get(key) or "") for key in PLACEHOLDER_KEYS}
    # Also accept key_label/room_code style used in guide captions.
    for key, value in (context or {}).items():
        if key not in ctx:
            ctx[key] = str(value or "")
    try:
        return Formatter().vformat(template, (), _SafeFormatDict(ctx))
    except ValueError:
        return template


def sample_placeholder_context(*, property_name: str = "") -> dict[str, str]:
    ctx = dict(SAMPLE_PLACEHOLDERS)
    if property_name:
        ctx["property_name"] = property_name
    return ctx
