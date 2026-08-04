"""Automation property settings GET/PATCH (ADR 0008 / PR-F)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.db import transaction

from apps.properties.guest_settings_events import (
    PropertySettingsUpdated,
    emit_property_settings_updated,
)
from apps.properties.models import AfterHoursArrivalPolicy, Property
from apps.properties.section_settings_validation import (
    validate_automation_settings_payload,
)


class AutomationSettingsConflict(Exception):
    """Optimistic lock mismatch — caller should return 409 with current body."""

    def __init__(self, *, property: Property, dto: dict[str, Any]):
        self.property = property
        self.dto = dto
        super().__init__("Automation settings version conflict")


@dataclass(frozen=True)
class AutomationSettingsPatchResult:
    property: Property
    dto: dict[str, Any]
    change_summary: tuple[str, ...]


def _as_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default


def get_automation_settings_dto(property: Property) -> dict[str, Any]:
    policy = (
        property.after_hours_arrival_policy or AfterHoursArrivalPolicy.CONTACT
    )
    return {
        "settings_version": int(property.settings_version or 1),
        "after_hours_arrival_policy": str(policy),
        "after_hours_contact_phone": str(property.after_hours_contact_phone or ""),
        "guest_arrival_auto_reply_enabled": bool(
            property.guest_arrival_auto_reply_enabled
        ),
        "guest_parking_auto_reply_enabled": bool(
            property.guest_parking_auto_reply_enabled
        ),
        "guest_invoice_auto_reply_enabled": bool(
            property.guest_invoice_auto_reply_enabled
        ),
    }


def _diff_change_summary(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    keys = (
        "after_hours_arrival_policy",
        "after_hours_contact_phone",
        "guest_arrival_auto_reply_enabled",
        "guest_parking_auto_reply_enabled",
        "guest_invoice_auto_reply_enabled",
    )
    return [key for key in keys if before.get(key) != after.get(key)]


class AutomationSettingsService:
    """get/patch after-hours policy + guest auto-reply flags + settings_version bump."""

    @staticmethod
    def get(property: Property) -> dict[str, Any]:
        return get_automation_settings_dto(property)

    @staticmethod
    @transaction.atomic
    def patch(
        property: Property,
        data: dict[str, Any],
        *,
        expected_version: int | None,
        actor_id: str | None = None,
        updated_by: dict[str, Any] | None = None,
    ) -> AutomationSettingsPatchResult:
        validate_automation_settings_payload(data)

        locked = Property.objects.select_for_update().get(pk=property.pk)
        current_version = int(locked.settings_version or 1)
        if expected_version is None or expected_version != current_version:
            raise AutomationSettingsConflict(
                property=locked, dto=get_automation_settings_dto(locked)
            )

        before = get_automation_settings_dto(locked)
        update_fields = ["settings_version", "updated_at"]

        if "after_hours_arrival_policy" in data:
            locked.after_hours_arrival_policy = str(
                data.get("after_hours_arrival_policy") or ""
            ).strip()
            update_fields.append("after_hours_arrival_policy")
        if "after_hours_contact_phone" in data:
            locked.after_hours_contact_phone = str(
                data.get("after_hours_contact_phone") or ""
            ).strip()
            update_fields.append("after_hours_contact_phone")
        if "guest_arrival_auto_reply_enabled" in data:
            locked.guest_arrival_auto_reply_enabled = _as_bool(
                data.get("guest_arrival_auto_reply_enabled"),
                default=True,
            )
            update_fields.append("guest_arrival_auto_reply_enabled")
        if "guest_parking_auto_reply_enabled" in data:
            locked.guest_parking_auto_reply_enabled = _as_bool(
                data.get("guest_parking_auto_reply_enabled"),
                default=True,
            )
            update_fields.append("guest_parking_auto_reply_enabled")
        if "guest_invoice_auto_reply_enabled" in data:
            locked.guest_invoice_auto_reply_enabled = _as_bool(
                data.get("guest_invoice_auto_reply_enabled"),
                default=True,
            )
            update_fields.append("guest_invoice_auto_reply_enabled")

        locked.settings_version = current_version + 1
        locked.save(update_fields=list(dict.fromkeys(update_fields)))

        after = get_automation_settings_dto(locked)
        change_summary = tuple(_diff_change_summary(before, after))
        emit_property_settings_updated(
            PropertySettingsUpdated(
                property_id=locked.pk,
                tenant_id=locked.tenant_id,
                section="automation",
                settings_version=locked.settings_version,
                change_summary=change_summary,
                actor_id=actor_id,
                updated_by=dict(updated_by or {}),
            )
        )
        return AutomationSettingsPatchResult(
            property=locked,
            dto=after,
            change_summary=change_summary,
        )


__all__ = [
    "AutomationSettingsConflict",
    "AutomationSettingsPatchResult",
    "AutomationSettingsService",
    "get_automation_settings_dto",
]
