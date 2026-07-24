"""Guest settings GET/PATCH service (ADR 0008 / PR-D1)."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from typing import Any

from django.db import transaction
from django.utils.dateparse import parse_date

from apps.communications.guest_language_constants import TEMPLATE_LANGS
from apps.properties.guest_info import (
    normalize_guest_info,
    normalize_parking_facts,
)
from apps.properties.guest_settings_events import (
    GuestSettingsUpdated,
    emit_guest_settings_updated,
)
from apps.properties.guest_settings_validation import (
    GUEST_SETTINGS_SCHEMA_VERSION,
    GuestSettingsValidationError,
    normalize_contact_phone,
    validate_guest_settings_payload,
)
from apps.properties.models import Property, SelfServiceMode
from apps.properties.portal_renderer import PortalRenderer, serialize_guest_portal_context
from apps.properties.self_service import normalize_self_service_config


def settings_version_etag(version: int) -> str:
    return f'W/"{int(version)}"'


def parse_if_match_version(if_match: str | None) -> int | None:
    """Parse weak/strong ETag from If-Match into settings_version int."""
    if not if_match:
        return None
    raw = if_match.strip()
    if raw == "*":
        return None
    # Take first tag if comma-separated.
    raw = raw.split(",", 1)[0].strip()
    if raw.startswith("W/"):
        raw = raw[2:].strip()
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        raw = raw[1:-1]
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _media_payload(url: str | None) -> dict[str, Any]:
    cleaned = str(url or "").strip()
    return {"asset_id": None, "url": cleaned}


def _filter_texts(block: Any, *, enabled_languages: frozenset[str]) -> dict[str, str]:
    if not isinstance(block, dict):
        return {}
    out: dict[str, str] = {}
    for lang, value in block.items():
        code = str(lang or "").strip().lower().split("-")[0]
        if code not in enabled_languages:
            continue
        text = str(value or "").strip()
        if text:
            out[code] = text
    return out


def _enabled_languages(property: Property) -> frozenset[str]:
    # v1: fixed TEMPLATE_LANGS until localization settings ship.
    del property
    return frozenset(TEMPLATE_LANGS)


class GuestSettingsConflict(Exception):
    """Optimistic lock mismatch — caller should return 409 with current body."""

    def __init__(self, *, property: Property, dto: dict[str, Any]):
        self.property = property
        self.dto = dto
        super().__init__("Guest settings version conflict")


@dataclass(frozen=True)
class GuestSettingsPatchResult:
    property: Property
    dto: dict[str, Any]
    change_summary: tuple[str, ...]


def get_guest_settings_dto(property: Property) -> dict[str, Any]:
    info = normalize_guest_info(property.guest_info)
    facts = info.get("facts") or {}
    links = info.get("links") or {}
    assets = info.get("assets") or {}
    texts = info.get("texts") or {}
    guide_raw = info.get("guide") if isinstance(info.get("guide"), dict) else {}
    enabled = _enabled_languages(property)

    wifi_raw = facts.get("wifi") if isinstance(facts.get("wifi"), dict) else {}
    wifi: dict[str, Any] = {
        "ssid": str(wifi_raw.get("ssid") or ""),
        "password": str(wifi_raw.get("password") or ""),
    }
    instructions = _filter_texts(wifi_raw.get("instructions"), enabled_languages=enabled)
    if instructions:
        wifi["instructions"] = instructions

    parking = normalize_parking_facts(facts.get("parking")) if facts.get("parking") else {}

    entrance_texts = _filter_texts(texts.get("entrance"), enabled_languages=enabled)
    arrival: dict[str, Any] = {
        "texts": entrance_texts,
        "maps_url": str(links.get("maps_url") or ""),
        "entrance": {
            "media": _media_payload(assets.get("entrance_image")),
        },
    }

    breakfast_raw = facts.get("breakfast") if isinstance(facts.get("breakfast"), dict) else {}
    breakfast_texts = {
        k: str(v).strip()
        for k, v in breakfast_raw.items()
        if k != "hours" and str(v or "").strip() and str(k).split("-")[0].lower() in enabled
    }
    breakfast: dict[str, Any] = {
        "texts": breakfast_texts,
        "hours": str(breakfast_raw.get("hours") or ""),
    }

    contact_raw = property.contact if isinstance(property.contact, dict) else {}
    contact = {
        "phone": str(contact_raw.get("phone") or contact_raw.get("reception_phone") or ""),
        "whatsapp": str(contact_raw.get("whatsapp") or ""),
    }

    self_service = {
        "mode": property.self_service_mode or SelfServiceMode.OFF,
        "config": normalize_self_service_config(property.self_service_config),
    }

    steps_out: list[dict[str, Any]] = []
    for step in guide_raw.get("steps") or []:
        if not isinstance(step, dict):
            continue
        caption = step.get("caption") if isinstance(step.get("caption"), dict) else {}
        # Guide captions may include langs beyond TEMPLATE_LANGS.
        caption_out = {
            str(k).split("-")[0].lower(): str(v).strip()
            for k, v in caption.items()
            if str(v or "").strip()
        }
        image = str(step.get("image") or "").strip()
        item: dict[str, Any] = {
            "section": str(step.get("section") or "").strip(),
            "caption": caption_out,
            "media": _media_payload(image),
        }
        steps_out.append(item)

    guide: dict[str, Any] = {
        "sections": guide_raw.get("sections") or {},
        "order": list(guide_raw.get("order") or []),
        "enabled": dict(guide_raw.get("enabled") or {}),
        "steps": steps_out,
    }

    return {
        "schema_version": GUEST_SETTINGS_SCHEMA_VERSION,
        "settings_version": int(property.settings_version or 1),
        "wifi": wifi,
        "parking": parking,
        "arrival": arrival,
        "breakfast": breakfast,
        "contact": contact,
        "self_service": self_service,
        "guide": guide,
        "publication": {"state": "published", "draft_available": False},
        "enabled_languages": sorted(enabled),
    }


def _diff_change_summary(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    keys = ("wifi", "parking", "arrival", "breakfast", "contact", "self_service", "guide")
    summary: list[str] = []
    for key in keys:
        if before.get(key) != after.get(key):
            if key == "wifi":
                b_wifi = before.get("wifi") or {}
                a_wifi = after.get("wifi") or {}
                wifi_changes: list[str] = []
                if b_wifi.get("ssid") != a_wifi.get("ssid"):
                    wifi_changes.append("wifi.ssid")
                if b_wifi.get("password") != a_wifi.get("password"):
                    wifi_changes.append("wifi.password")
                if b_wifi.get("instructions") != a_wifi.get("instructions"):
                    wifi_changes.append("wifi.instructions")
                summary.extend(wifi_changes or ["wifi"])
            else:
                summary.append(key)
    # de-dupe preserving order
    seen: set[str] = set()
    out: list[str] = []
    for item in summary:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _merge_wifi(info: dict[str, Any], wifi: dict[str, Any]) -> None:
    facts = dict(info.get("facts") or {})
    current = facts.get("wifi") if isinstance(facts.get("wifi"), dict) else {}
    merged = dict(current)
    if "ssid" in wifi:
        ssid = str(wifi.get("ssid") or "").strip()
        if ssid:
            merged["ssid"] = ssid
        else:
            merged.pop("ssid", None)
    if "password" in wifi:
        password = str(wifi.get("password") or "").strip()
        if password:
            merged["password"] = password
        else:
            merged.pop("password", None)
    if "instructions" in wifi:
        instructions = wifi.get("instructions")
        if isinstance(instructions, dict) and instructions:
            merged["instructions"] = {
                str(k).split("-")[0].lower(): str(v).strip()
                for k, v in instructions.items()
                if str(v or "").strip()
            }
        else:
            merged.pop("instructions", None)
    facts["wifi"] = merged
    info["facts"] = facts


def _merge_parking(info: dict[str, Any], parking: dict[str, Any]) -> None:
    facts = dict(info.get("facts") or {})
    current = facts.get("parking") if isinstance(facts.get("parking"), dict) else {}
    merged = dict(current)
    merged.update(parking)
    facts["parking"] = normalize_parking_facts(merged)
    info["facts"] = facts


def _merge_arrival(info: dict[str, Any], arrival: dict[str, Any]) -> None:
    if "texts" in arrival and isinstance(arrival.get("texts"), dict):
        texts = dict(info.get("texts") or {})
        localized = {
            str(k).split("-")[0].lower(): str(v).strip()
            for k, v in arrival["texts"].items()
            if str(v or "").strip()
        }
        if localized:
            texts["entrance"] = localized
        else:
            texts.pop("entrance", None)
        info["texts"] = texts

    if "maps_url" in arrival:
        links = dict(info.get("links") or {})
        maps_url = str(arrival.get("maps_url") or "").strip()
        links["maps_url"] = maps_url
        info["links"] = links

    entrance = arrival.get("entrance")
    if isinstance(entrance, dict) and "media" in entrance:
        media = entrance.get("media") if isinstance(entrance.get("media"), dict) else {}
        assets = dict(info.get("assets") or {})
        url = str(media.get("url") or "").strip()
        assets["entrance_image"] = url
        info["assets"] = assets


def _merge_breakfast(info: dict[str, Any], breakfast: dict[str, Any]) -> None:
    facts = dict(info.get("facts") or {})
    current = facts.get("breakfast") if isinstance(facts.get("breakfast"), dict) else {}
    merged: dict[str, Any] = {
        k: v for k, v in current.items() if k != "hours"
    }
    if "texts" in breakfast and isinstance(breakfast.get("texts"), dict):
        merged = {
            str(k).split("-")[0].lower(): str(v).strip()
            for k, v in breakfast["texts"].items()
            if str(v or "").strip()
        }
    if "hours" in breakfast:
        hours = str(breakfast.get("hours") or "").strip()
        if hours:
            merged["hours"] = hours
        else:
            merged.pop("hours", None)
    elif "hours" in current:
        merged["hours"] = current["hours"]
    facts["breakfast"] = merged
    info["facts"] = facts


def _merge_guide(info: dict[str, Any], guide: dict[str, Any]) -> None:
    current = info.get("guide") if isinstance(info.get("guide"), dict) else {}
    merged = deepcopy(current)
    for key in ("sections", "order", "enabled"):
        if key in guide:
            merged[key] = guide[key]
    if "steps" in guide and isinstance(guide.get("steps"), list):
        steps: list[dict[str, Any]] = []
        for step in guide["steps"]:
            if not isinstance(step, dict):
                continue
            item: dict[str, Any] = {}
            section = str(step.get("section") or "").strip()
            if section:
                item["section"] = section
            caption = step.get("caption")
            if isinstance(caption, dict) and caption:
                item["caption"] = {
                    str(k).split("-")[0].lower(): str(v).strip()
                    for k, v in caption.items()
                    if str(v or "").strip()
                }
            media = step.get("media") if isinstance(step.get("media"), dict) else {}
            # Accept legacy image field on write for resilience.
            image = str(media.get("url") or step.get("image") or "").strip()
            if image:
                item["image"] = image
            if item:
                steps.append(item)
        merged["steps"] = steps
    info["guide"] = merged


def _merge_contact(property: Property, contact: dict[str, Any]) -> dict[str, Any]:
    current = dict(property.contact) if isinstance(property.contact, dict) else {}
    if "phone" in contact:
        phone = normalize_contact_phone(contact.get("phone"))
        if phone:
            current["phone"] = phone
        else:
            current.pop("phone", None)
    if "whatsapp" in contact:
        wa = normalize_contact_phone(contact.get("whatsapp"))
        if wa:
            current["whatsapp"] = wa
        else:
            current.pop("whatsapp", None)
    return current


class GuestSettingsService:
    """get/patch guest DTO, schema_version, validation, settings_version bump."""

    @staticmethod
    def get(property: Property) -> dict[str, Any]:
        return get_guest_settings_dto(property)

    @staticmethod
    def preview(
        property: Property,
        *,
        language: str | None = None,
        on_date: date | str | None = None,
    ) -> dict[str, Any]:
        resolved_date: date | None = None
        if isinstance(on_date, date):
            resolved_date = on_date
        elif isinstance(on_date, str) and on_date.strip():
            resolved_date = parse_date(on_date.strip())
        ctx = PortalRenderer.render_for_property(
            property,
            language=language,
            on_date=resolved_date,
        )
        return serialize_guest_portal_context(ctx)

    @staticmethod
    @transaction.atomic
    def patch(
        property: Property,
        data: dict[str, Any],
        *,
        expected_version: int | None,
        actor_id: str | None = None,
        updated_by: dict[str, Any] | None = None,
    ) -> GuestSettingsPatchResult:
        validate_guest_settings_payload(data)

        locked = Property.objects.select_for_update().get(pk=property.pk)
        current_version = int(locked.settings_version or 1)
        if expected_version is None or expected_version != current_version:
            raise GuestSettingsConflict(property=locked, dto=get_guest_settings_dto(locked))

        before = get_guest_settings_dto(locked)
        info = normalize_guest_info(locked.guest_info)

        if "wifi" in data and isinstance(data.get("wifi"), dict):
            _merge_wifi(info, data["wifi"])
        if "parking" in data and isinstance(data.get("parking"), dict):
            _merge_parking(info, data["parking"])
        if "arrival" in data and isinstance(data.get("arrival"), dict):
            _merge_arrival(info, data["arrival"])
        if "breakfast" in data and isinstance(data.get("breakfast"), dict):
            _merge_breakfast(info, data["breakfast"])
        if "guide" in data and isinstance(data.get("guide"), dict):
            _merge_guide(info, data["guide"])

        contact_changed = False
        if "contact" in data and isinstance(data.get("contact"), dict):
            locked.contact = _merge_contact(locked, data["contact"])
            contact_changed = True

        self_service_changed = False
        if "self_service" in data and isinstance(data.get("self_service"), dict):
            ss = data["self_service"]
            if "mode" in ss:
                locked.self_service_mode = str(ss.get("mode") or SelfServiceMode.OFF).strip()
                self_service_changed = True
            if "config" in ss:
                locked.self_service_config = normalize_self_service_config(ss.get("config"))
                self_service_changed = True

        # Persist schema_version inside guest_info for storage discovery.
        info["schema_version"] = GUEST_SETTINGS_SCHEMA_VERSION
        locked.guest_info = normalize_guest_info(info)
        locked.settings_version = current_version + 1

        update_fields = ["guest_info", "settings_version", "updated_at"]
        if contact_changed:
            update_fields.append("contact")
        if self_service_changed:
            update_fields.extend(["self_service_mode", "self_service_config"])
        locked.save(update_fields=update_fields)

        after = get_guest_settings_dto(locked)
        change_summary = tuple(_diff_change_summary(before, after))
        emit_guest_settings_updated(
            GuestSettingsUpdated(
                property_id=locked.pk,
                tenant_id=locked.tenant_id,
                section="guest",
                settings_version=locked.settings_version,
                change_summary=change_summary,
                actor_id=actor_id,
                updated_by=dict(updated_by or {}),
            )
        )
        return GuestSettingsPatchResult(
            property=locked,
            dto=after,
            change_summary=change_summary,
        )
