"""General property settings GET/PATCH (ADR 0008 / PR-E)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.db import transaction

from apps.properties.guest_settings_events import (
    PropertySettingsUpdated,
    emit_property_settings_updated,
)
from apps.properties.models import Property
from apps.properties.section_settings_validation import (
    validate_general_settings_payload,
)


class GeneralSettingsConflict(Exception):
    """Optimistic lock mismatch — caller should return 409 with current body."""

    def __init__(self, *, property: Property, dto: dict[str, Any]):
        self.property = property
        self.dto = dto
        super().__init__("General settings version conflict")


@dataclass(frozen=True)
class GeneralSettingsPatchResult:
    property: Property
    dto: dict[str, Any]
    change_summary: tuple[str, ...]


def get_general_settings_dto(property: Property) -> dict[str, Any]:
    return {
        "settings_version": int(property.settings_version or 1),
        "name": str(property.name or ""),
        "slug": str(property.slug or ""),
        "address": str(property.address or ""),
        "timezone": str(property.timezone or ""),
        "language": str(property.language or "").strip().lower().split("-")[0]
        if property.language
        else "",
    }


def _diff_change_summary(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    keys = ("name", "address", "timezone", "language")
    summary: list[str] = []
    for key in keys:
        if before.get(key) != after.get(key):
            summary.append(key)
    return summary


class GeneralSettingsService:
    """get/patch general (identity) DTO + settings_version bump."""

    @staticmethod
    def get(property: Property) -> dict[str, Any]:
        return get_general_settings_dto(property)

    @staticmethod
    @transaction.atomic
    def patch(
        property: Property,
        data: dict[str, Any],
        *,
        expected_version: int | None,
        actor_id: str | None = None,
        updated_by: dict[str, Any] | None = None,
    ) -> GeneralSettingsPatchResult:
        validate_general_settings_payload(data)

        locked = Property.objects.select_for_update().get(pk=property.pk)
        current_version = int(locked.settings_version or 1)
        if expected_version is None or expected_version != current_version:
            raise GeneralSettingsConflict(
                property=locked, dto=get_general_settings_dto(locked)
            )

        before = get_general_settings_dto(locked)
        update_fields = ["settings_version", "updated_at"]

        if "name" in data:
            locked.name = str(data.get("name") or "").strip()
            update_fields.append("name")
        if "address" in data:
            locked.address = str(data.get("address") or "")
            update_fields.append("address")
        if "timezone" in data:
            locked.timezone = str(data.get("timezone") or "").strip()
            update_fields.append("timezone")
        if "language" in data:
            lang = str(data.get("language") or "").strip().lower().split("-")[0]
            locked.language = lang
            update_fields.append("language")

        # slug is read-only via this API (admin / ops).
        locked.settings_version = current_version + 1
        locked.save(update_fields=list(dict.fromkeys(update_fields)))

        after = get_general_settings_dto(locked)
        change_summary = tuple(_diff_change_summary(before, after))
        emit_property_settings_updated(
            PropertySettingsUpdated(
                property_id=locked.pk,
                tenant_id=locked.tenant_id,
                section="general",
                settings_version=locked.settings_version,
                change_summary=change_summary,
                actor_id=actor_id,
                updated_by=dict(updated_by or {}),
            )
        )
        return GeneralSettingsPatchResult(
            property=locked,
            dto=after,
            change_summary=change_summary,
        )


__all__ = [
    "GeneralSettingsConflict",
    "GeneralSettingsPatchResult",
    "GeneralSettingsService",
    "get_general_settings_dto",
]
