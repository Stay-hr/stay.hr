"""Check-in property settings GET/PATCH (ADR 0008 / PR-E)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import Any

from django.db import transaction

from apps.properties.guest_settings_events import (
    PropertySettingsUpdated,
    emit_property_settings_updated,
)
from apps.properties.models import Property
from apps.properties.section_settings_validation import (
    SectionSettingsValidationError,
    format_time_hm,
    validate_checkin_settings_payload,
)


class CheckinSettingsConflict(Exception):
    """Optimistic lock mismatch — caller should return 409 with current body."""

    def __init__(self, *, property: Property, dto: dict[str, Any]):
        self.property = property
        self.dto = dto
        super().__init__("Check-in settings version conflict")


@dataclass(frozen=True)
class CheckinSettingsPatchResult:
    property: Property
    dto: dict[str, Any]
    change_summary: tuple[str, ...]


def _parse_time_value(value: Any) -> time | None:
    if value is None or value == "":
        return None
    if isinstance(value, time):
        return value
    raw = str(value).strip()
    parts = raw.split(":")
    hour = int(parts[0])
    minute = int(parts[1])
    second = int(parts[2]) if len(parts) == 3 else 0
    return time(hour, minute, second)


def get_checkin_settings_dto(property: Property) -> dict[str, Any]:
    return {
        "settings_version": int(property.settings_version or 1),
        "check_in_time": format_time_hm(property.check_in_time) or "15:00",
        "check_out_time": format_time_hm(property.check_out_time) or "11:00",
        "check_in_latest_time": format_time_hm(property.check_in_latest_time),
        "guest_checkin_opens_days_before": int(
            property.guest_checkin_opens_days_before
            if property.guest_checkin_opens_days_before is not None
            else 7
        ),
    }


def _diff_change_summary(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    keys = (
        "check_in_time",
        "check_out_time",
        "check_in_latest_time",
        "guest_checkin_opens_days_before",
    )
    return [key for key in keys if before.get(key) != after.get(key)]


class CheckinSettingsService:
    """get/patch check-in window DTO + settings_version bump.

    After-hours policy and auto-replies ship under settings/automation (PR-F).
    """

    @staticmethod
    def get(property: Property) -> dict[str, Any]:
        return get_checkin_settings_dto(property)

    @staticmethod
    @transaction.atomic
    def patch(
        property: Property,
        data: dict[str, Any],
        *,
        expected_version: int | None,
        actor_id: str | None = None,
        updated_by: dict[str, Any] | None = None,
    ) -> CheckinSettingsPatchResult:
        validate_checkin_settings_payload(data)

        locked = Property.objects.select_for_update().get(pk=property.pk)
        current_version = int(locked.settings_version or 1)
        if expected_version is None or expected_version != current_version:
            raise CheckinSettingsConflict(
                property=locked, dto=get_checkin_settings_dto(locked)
            )

        before = get_checkin_settings_dto(locked)
        update_fields = ["settings_version", "updated_at"]

        if "check_in_time" in data:
            locked.check_in_time = _parse_time_value(data.get("check_in_time"))
            update_fields.append("check_in_time")
        if "check_out_time" in data:
            locked.check_out_time = _parse_time_value(data.get("check_out_time"))
            update_fields.append("check_out_time")
        if "check_in_latest_time" in data:
            locked.check_in_latest_time = _parse_time_value(data.get("check_in_latest_time"))
            update_fields.append("check_in_latest_time")
        if "guest_checkin_opens_days_before" in data:
            locked.guest_checkin_opens_days_before = int(
                data.get("guest_checkin_opens_days_before")
            )
            update_fields.append("guest_checkin_opens_days_before")

        # Cross-field guard when only latest is patched against stored check-in.
        if (
            locked.check_in_latest_time is not None
            and locked.check_in_time is not None
            and locked.check_in_latest_time <= locked.check_in_time
        ):
            raise SectionSettingsValidationError(
                {
                    "check_in_latest_time": "Latest arrival must be after check-in time.",
                }
            )

        locked.settings_version = current_version + 1
        locked.save(update_fields=list(dict.fromkeys(update_fields)))

        after = get_checkin_settings_dto(locked)
        change_summary = tuple(_diff_change_summary(before, after))
        emit_property_settings_updated(
            PropertySettingsUpdated(
                property_id=locked.pk,
                tenant_id=locked.tenant_id,
                section="checkin",
                settings_version=locked.settings_version,
                change_summary=change_summary,
                actor_id=actor_id,
                updated_by=dict(updated_by or {}),
            )
        )
        return CheckinSettingsPatchResult(
            property=locked,
            dto=after,
            change_summary=change_summary,
        )


__all__ = [
    "CheckinSettingsConflict",
    "CheckinSettingsPatchResult",
    "CheckinSettingsService",
    "get_checkin_settings_dto",
]
