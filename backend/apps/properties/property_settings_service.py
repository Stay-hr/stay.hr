"""Property Settings capabilities and property list (ADR 0008 / PR-D0)."""

from __future__ import annotations

from typing import Any

from django.conf import settings

from apps.properties.models import Property
from apps.tenants.models import Tenant

SETTINGS_TAB_KEYS = ("general", "guest", "checkin", "automation")
SETTINGS_CAPABILITY_KEYS = (
    "guest_settings",
    "preview",
    "share",
    "automation",
    "checkin",
    "general",
)

# Reserved section slugs (frontend stubs / API 404 until dedicated PRs).
RESERVED_SETTINGS_SECTIONS = (
    "security",
    "integrations",
    "branding",
    "localization",
    "payments",
    "reviews",
    "users",
)

# Section paths under properties/{id}/settings/ — stubbed until their PR ships.
SETTINGS_SECTION_SLUGS = (
    "general",
    "guest",
    "checkin",
    "automation",
    *RESERVED_SETTINGS_SECTIONS,
)


def property_settings_enabled() -> bool:
    return bool(getattr(settings, "RECEPTION_PROPERTY_SETTINGS", True))


def _all_false(keys: tuple[str, ...]) -> dict[str, bool]:
    return {key: False for key in keys}


def build_settings_capabilities() -> dict[str, Any]:
    """Return capabilities + tabs for GET /api/v1/reception/settings/."""
    if not property_settings_enabled():
        return {
            "capabilities": _all_false(SETTINGS_CAPABILITY_KEYS),
            "tabs": _all_false(SETTINGS_TAB_KEYS),
        }

    # Guest (D1/D2) + general/checkin (PR-E) + automation (PR-F).
    return {
        "capabilities": {
            "guest_settings": True,
            "preview": True,
            "share": True,
            "automation": True,
            "checkin": True,
            "general": True,
        },
        "tabs": {
            "general": True,
            "guest": True,
            "checkin": True,
            "automation": True,
        },
    }


def settings_surface_enabled(payload: dict[str, Any] | None = None) -> bool:
    """True when any tab or capability is enabled (nav visibility)."""
    data = payload if payload is not None else build_settings_capabilities()
    tabs = data.get("tabs") or {}
    caps = data.get("capabilities") or {}
    return any(tabs.values()) or any(caps.values())


def list_properties_for_tenant(tenant: Tenant) -> list[dict[str, Any]]:
    """Lightweight property rows for the settings property picker."""
    qs = Property.objects.for_tenant(tenant).order_by("name", "id")
    return [
        {
            "id": prop.id,
            "name": prop.name,
            "slug": prop.slug,
        }
        for prop in qs
    ]


def get_tenant_property_or_none(tenant: Tenant, property_id: int) -> Property | None:
    return Property.objects.for_tenant(tenant).filter(pk=property_id).first()
