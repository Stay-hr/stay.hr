"""Domain events for Property Settings guest section (ADR 0008)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GuestSettingsUpdated:
    property_id: int
    tenant_id: int
    section: str
    settings_version: int
    change_summary: tuple[str, ...]
    actor_id: str | None = None
    updated_by: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PropertySettingsUpdated:
    property_id: int
    tenant_id: int
    section: str
    settings_version: int
    change_summary: tuple[str, ...]
    actor_id: str | None = None
    updated_by: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GuestPortalShared:
    property_id: int
    tenant_id: int
    reservation_id: int
    channel: str
    kind: str
    target: str = "reservation"
    status: str = "sent"
    actor_id: str | None = None
    updated_by: dict[str, Any] = field(default_factory=dict)


_GUEST_SETTINGS_UPDATED_HANDLERS: list[Callable[[GuestSettingsUpdated], None]] = []
_PROPERTY_SETTINGS_UPDATED_HANDLERS: list[Callable[[PropertySettingsUpdated], None]] = []
_GUEST_PORTAL_SHARED_HANDLERS: list[Callable[[GuestPortalShared], None]] = []


def on_guest_settings_updated(handler: Callable[[GuestSettingsUpdated], None]):
    _GUEST_SETTINGS_UPDATED_HANDLERS.append(handler)
    return handler


def on_property_settings_updated(handler: Callable[[PropertySettingsUpdated], None]):
    _PROPERTY_SETTINGS_UPDATED_HANDLERS.append(handler)
    return handler


def on_guest_portal_shared(handler: Callable[[GuestPortalShared], None]):
    _GUEST_PORTAL_SHARED_HANDLERS.append(handler)
    return handler


def emit_property_settings_updated(event: PropertySettingsUpdated) -> None:
    logger.info(
        "property_settings_updated section=%s property=%s version=%s changes=%s actor=%s",
        event.section,
        event.property_id,
        event.settings_version,
        list(event.change_summary),
        event.actor_id,
    )
    for handler in _PROPERTY_SETTINGS_UPDATED_HANDLERS:
        handler(event)


def emit_guest_settings_updated(event: GuestSettingsUpdated) -> None:
    logger.info(
        "guest_settings_saved property=%s version=%s changes=%s actor=%s",
        event.property_id,
        event.settings_version,
        list(event.change_summary),
        event.actor_id,
    )
    for handler in _GUEST_SETTINGS_UPDATED_HANDLERS:
        handler(event)

    # Mirror as PropertySettingsUpdated for section-agnostic consumers.
    emit_property_settings_updated(
        PropertySettingsUpdated(
            property_id=event.property_id,
            tenant_id=event.tenant_id,
            section=event.section,
            settings_version=event.settings_version,
            change_summary=event.change_summary,
            actor_id=event.actor_id,
            updated_by=dict(event.updated_by),
        )
    )


def emit_guest_portal_shared(event: GuestPortalShared) -> None:
    logger.info(
        "portal_shared reservation=%s channel=%s kind=%s status=%s actor=%s",
        event.reservation_id,
        event.channel,
        event.kind,
        event.status,
        event.actor_id,
    )
    for handler in _GUEST_PORTAL_SHARED_HANDLERS:
        handler(event)
