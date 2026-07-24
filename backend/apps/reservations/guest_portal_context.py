"""Guest portal context — thin façade over PortalRenderer (ADR 0008).

Public portal and settings preview share ``PortalRenderer``. Import from here for
backward-compatible call sites; new code may import ``portal_renderer`` directly.
"""

from __future__ import annotations

from apps.properties.portal_renderer import (
    GuestPortalContext,
    PortalRenderer,
    entrance_image_file_for_access,
    guide_step_image_path,
    key_guide_step_file_for_access,
    serialize_guest_portal_context,
)
from apps.reservations.models import GuestPortalAccess

# Re-export section order for tests / callers that imported it from here.
from apps.properties.portal_renderer import PORTAL_SECTION_ORDER  # noqa: F401

__all__ = [
    "GuestPortalContext",
    "PORTAL_SECTION_ORDER",
    "build_guest_portal_context",
    "entrance_image_file_for_access",
    "guide_step_image_path",
    "key_guide_step_file_for_access",
    "serialize_guest_portal_context",
]


def build_guest_portal_context(
    access: GuestPortalAccess,
    *,
    language: str | None = None,
) -> GuestPortalContext:
    return PortalRenderer.render_for_access(access, language=language)
