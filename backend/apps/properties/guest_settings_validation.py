"""Validation limits and helpers for Property Settings guest section (ADR 0008).

Frontend should mirror these constants (see web/reception/lib/guestSettingsLimits.ts).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from apps.communications.guest_language_constants import TEMPLATE_LANGS
from apps.integrations.whatsapp.phone import normalize_phone
from apps.properties.guest_info import GUIDE_SECTION_KEYS
from apps.properties.models import SelfServiceMode

GUEST_SETTINGS_SCHEMA_VERSION = 1

MAX_WIFI_SSID_LENGTH = 128
MAX_WIFI_PASSWORD_LENGTH = 128
MAX_MAPS_URL_LENGTH = 2048
MAX_CAPTION_LENGTH = 2000
MAX_GUIDE_STEPS = 40
MAX_PHONE_LENGTH = 32
MAX_BREAKFAST_HOURS_LENGTH = 64
MAX_PARKING_ZONE_LABEL_LENGTH = 255
MAX_PARKING_PRICE_NOTES_LENGTH = 255
MAX_TEXT_FIELD_LENGTH = 8000

SUPPORTED_LANGUAGE_CODES = frozenset(TEMPLATE_LANGS)
ALLOWED_MAPS_URL_SCHEMES = frozenset({"http", "https"})
ALLOWED_SELF_SERVICE_MODES = frozenset(SelfServiceMode.values)


class GuestSettingsValidationError(Exception):
    """Raised when guest settings PATCH payload fails validation."""

    def __init__(self, errors: dict[str, Any]):
        self.errors = errors
        super().__init__("Guest settings validation failed")


def normalize_contact_phone(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    digits = normalize_phone(text)
    if digits:
        return digits if text.startswith("+") or text.isdigit() else text
    return text


def validate_maps_url(url: str) -> str | None:
    """Return error message or None when valid (blank allowed)."""
    cleaned = (url or "").strip()
    if not cleaned:
        return None
    if len(cleaned) > MAX_MAPS_URL_LENGTH:
        return f"Must be at most {MAX_MAPS_URL_LENGTH} characters."
    parsed = urlparse(cleaned)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_MAPS_URL_SCHEMES:
        return "URL must use http or https."
    if not parsed.netloc:
        return "URL must include a host."
    return None


def _lang_errors(block: Any, *, field: str, max_len: int = MAX_TEXT_FIELD_LENGTH) -> dict[str, str]:
    if block is None:
        return {}
    if not isinstance(block, dict):
        return {field: "Must be an object of language → text."}
    errors: dict[str, str] = {}
    for lang, value in block.items():
        code = str(lang or "").strip().lower().split("-")[0]
        if code not in SUPPORTED_LANGUAGE_CODES and len(code) == 2 and code.isalpha():
            # Allow guide extras beyond TEMPLATE_LANGS for captions; reject unknown shape elsewhere.
            if field.startswith("guide."):
                pass
            else:
                errors[f"{field}.{lang}"] = f"Unsupported language code: {lang}."
                continue
        elif code not in SUPPORTED_LANGUAGE_CODES and not field.startswith("guide."):
            errors[f"{field}.{lang}"] = f"Unsupported language code: {lang}."
            continue
        text = str(value or "")
        if len(text) > max_len:
            errors[f"{field}.{lang}"] = f"Must be at most {max_len} characters."
    return errors


def validate_guest_settings_payload(data: Any) -> dict[str, Any]:
    """Validate a guest settings PATCH body. Returns normalized shallow checks only.

    Raises GuestSettingsValidationError with ``{field: message}`` on failure.
    """
    if not isinstance(data, dict):
        raise GuestSettingsValidationError({"non_field_errors": "Payload must be an object."})

    errors: dict[str, str] = {}

    schema_version = data.get("schema_version", GUEST_SETTINGS_SCHEMA_VERSION)
    try:
        schema_version_int = int(schema_version)
    except (TypeError, ValueError):
        errors["schema_version"] = "Must be an integer."
        schema_version_int = -1
    else:
        if schema_version_int != GUEST_SETTINGS_SCHEMA_VERSION:
            errors["schema_version"] = (
                f"Unsupported schema_version {schema_version_int}; "
                f"only {GUEST_SETTINGS_SCHEMA_VERSION} is accepted."
            )

    wifi = data.get("wifi")
    if wifi is not None:
        if not isinstance(wifi, dict):
            errors["wifi"] = "Must be an object."
        else:
            ssid = str(wifi.get("ssid") or "")
            password = str(wifi.get("password") or "")
            if len(ssid) > MAX_WIFI_SSID_LENGTH:
                errors["wifi.ssid"] = f"Must be at most {MAX_WIFI_SSID_LENGTH} characters."
            if len(password) > MAX_WIFI_PASSWORD_LENGTH:
                errors["wifi.password"] = f"Must be at most {MAX_WIFI_PASSWORD_LENGTH} characters."
            if "instructions" in wifi:
                errors.update(_lang_errors(wifi.get("instructions"), field="wifi.instructions"))

    parking = data.get("parking")
    if parking is not None:
        if not isinstance(parking, dict):
            errors["parking"] = "Must be an object."
        else:
            zone = str(parking.get("zone_label") or "")
            if len(zone) > MAX_PARKING_ZONE_LABEL_LENGTH:
                errors["parking.zone_label"] = (
                    f"Must be at most {MAX_PARKING_ZONE_LABEL_LENGTH} characters."
                )
            notes = str(parking.get("price_notes") or "")
            if len(notes) > MAX_PARKING_PRICE_NOTES_LENGTH:
                errors["parking.price_notes"] = (
                    f"Must be at most {MAX_PARKING_PRICE_NOTES_LENGTH} characters."
                )
            if "custom" in parking:
                errors.update(_lang_errors(parking.get("custom"), field="parking.custom"))

    arrival = data.get("arrival")
    if arrival is not None:
        if not isinstance(arrival, dict):
            errors["arrival"] = "Must be an object."
        else:
            if "texts" in arrival:
                errors.update(_lang_errors(arrival.get("texts"), field="arrival.texts"))
            maps_err = validate_maps_url(str(arrival.get("maps_url") or ""))
            if maps_err:
                errors["arrival.maps_url"] = maps_err
            entrance = arrival.get("entrance")
            if entrance is not None:
                if not isinstance(entrance, dict):
                    errors["arrival.entrance"] = "Must be an object."
                else:
                    media = entrance.get("media")
                    if media is not None and not isinstance(media, dict):
                        errors["arrival.entrance.media"] = "Must be an object."

    breakfast = data.get("breakfast")
    if breakfast is not None:
        if not isinstance(breakfast, dict):
            errors["breakfast"] = "Must be an object."
        else:
            if "texts" in breakfast:
                errors.update(_lang_errors(breakfast.get("texts"), field="breakfast.texts"))
            hours = str(breakfast.get("hours") or "")
            if len(hours) > MAX_BREAKFAST_HOURS_LENGTH:
                errors["breakfast.hours"] = (
                    f"Must be at most {MAX_BREAKFAST_HOURS_LENGTH} characters."
                )

    contact = data.get("contact")
    if contact is not None:
        if not isinstance(contact, dict):
            errors["contact"] = "Must be an object."
        else:
            for key in ("phone", "whatsapp"):
                val = str(contact.get(key) or "")
                if len(val) > MAX_PHONE_LENGTH:
                    errors[f"contact.{key}"] = f"Must be at most {MAX_PHONE_LENGTH} characters."

    self_service = data.get("self_service")
    if self_service is not None:
        if not isinstance(self_service, dict):
            errors["self_service"] = "Must be an object."
        else:
            mode = str(self_service.get("mode") or "").strip()
            if mode and mode not in ALLOWED_SELF_SERVICE_MODES:
                errors["self_service.mode"] = f"Invalid mode: {mode}."
            config = self_service.get("config")
            if config is not None and not isinstance(config, dict):
                errors["self_service.config"] = "Must be an object."

    guide = data.get("guide")
    if guide is not None:
        if not isinstance(guide, dict):
            errors["guide"] = "Must be an object."
        else:
            steps = guide.get("steps")
            if steps is not None:
                if not isinstance(steps, list):
                    errors["guide.steps"] = "Must be a list."
                elif len(steps) > MAX_GUIDE_STEPS:
                    errors["guide.steps"] = f"At most {MAX_GUIDE_STEPS} steps allowed."
                else:
                    for index, step in enumerate(steps):
                        if not isinstance(step, dict):
                            errors[f"guide.steps[{index}]"] = "Must be an object."
                            continue
                        section = str(step.get("section") or "").strip()
                        if section and section not in GUIDE_SECTION_KEYS:
                            errors[f"guide.steps[{index}].section"] = f"Unknown section: {section}."
                        caption = step.get("caption")
                        if caption is not None:
                            if not isinstance(caption, dict):
                                errors[f"guide.steps[{index}].caption"] = "Must be an object."
                            else:
                                for lang, value in caption.items():
                                    text = str(value or "")
                                    if len(text) > MAX_CAPTION_LENGTH:
                                        errors[f"guide.steps[{index}].caption.{lang}"] = (
                                            f"Must be at most {MAX_CAPTION_LENGTH} characters."
                                        )
                        media = step.get("media")
                        if media is not None and not isinstance(media, dict):
                            errors[f"guide.steps[{index}].media"] = "Must be an object."

    if errors:
        raise GuestSettingsValidationError(errors)
    return data
