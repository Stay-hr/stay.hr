"""Validation for Property Settings general / check-in / automation (ADR 0008)."""

from __future__ import annotations

from datetime import time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from apps.communications.guest_language_constants import TEMPLATE_LANGS
from apps.properties.models import AfterHoursArrivalPolicy

MAX_PROPERTY_NAME_LENGTH = 255
MAX_ADDRESS_LENGTH = 2000
MAX_TIMEZONE_LENGTH = 64
MAX_LANGUAGE_LENGTH = 10
MAX_GUEST_CHECKIN_OPENS_DAYS = 90
MAX_AFTER_HOURS_PHONE_LENGTH = 32

_AVAILABLE_TIMEZONES = frozenset(available_timezones())
_SUPPORTED_LANGUAGES = frozenset(TEMPLATE_LANGS)
_AFTER_HOURS_POLICIES = frozenset(AfterHoursArrivalPolicy.values)


class SectionSettingsValidationError(Exception):
    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        super().__init__("Section settings validation failed")


def _parse_time(value: Any, *, field: str, errors: dict[str, str]) -> time | None:
    if value is None or value == "":
        return None
    if isinstance(value, time):
        return value
    raw = str(value).strip()
    # Accept HH:MM or HH:MM:SS
    parts = raw.split(":")
    if len(parts) not in (2, 3):
        errors[field] = "Invalid time format (use HH:MM)."
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
        second = int(parts[2]) if len(parts) == 3 else 0
        return time(hour, minute, second)
    except (TypeError, ValueError):
        errors[field] = "Invalid time format (use HH:MM)."
        return None


def format_time_hm(value: time | None) -> str | None:
    if value is None:
        return None
    return value.strftime("%H:%M")


def validate_general_settings_payload(data: dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise SectionSettingsValidationError({"_": "Payload must be an object."})

    errors: dict[str, str] = {}

    if "name" in data:
        name = str(data.get("name") or "").strip()
        if not name:
            errors["name"] = "Name is required."
        elif len(name) > MAX_PROPERTY_NAME_LENGTH:
            errors["name"] = f"Name must be at most {MAX_PROPERTY_NAME_LENGTH} characters."

    if "address" in data:
        address = str(data.get("address") or "")
        if len(address) > MAX_ADDRESS_LENGTH:
            errors["address"] = f"Address must be at most {MAX_ADDRESS_LENGTH} characters."

    if "timezone" in data:
        tz = str(data.get("timezone") or "").strip()
        if len(tz) > MAX_TIMEZONE_LENGTH:
            errors["timezone"] = f"Timezone must be at most {MAX_TIMEZONE_LENGTH} characters."
        elif tz:
            if tz not in _AVAILABLE_TIMEZONES:
                # ZoneInfo may still resolve aliases not listed in available_timezones().
                try:
                    ZoneInfo(tz)
                except ZoneInfoNotFoundError:
                    errors["timezone"] = "Unknown IANA timezone."

    if "language" in data:
        lang = str(data.get("language") or "").strip().lower().split("-")[0]
        if lang and (len(lang) > MAX_LANGUAGE_LENGTH or lang not in _SUPPORTED_LANGUAGES):
            errors["language"] = (
                "Unsupported language code. "
                f"Supported: {', '.join(sorted(_SUPPORTED_LANGUAGES))}."
            )

    if errors:
        raise SectionSettingsValidationError(errors)


def validate_checkin_settings_payload(data: dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise SectionSettingsValidationError({"_": "Payload must be an object."})

    errors: dict[str, str] = {}

    check_in: time | None = None
    check_out: time | None = None
    check_in_latest: time | None = None

    if "check_in_time" in data:
        if data.get("check_in_time") in (None, ""):
            errors["check_in_time"] = "Check-in time is required."
        else:
            check_in = _parse_time(
                data.get("check_in_time"), field="check_in_time", errors=errors
            )

    if "check_out_time" in data:
        if data.get("check_out_time") in (None, ""):
            errors["check_out_time"] = "Check-out time is required."
        else:
            check_out = _parse_time(
                data.get("check_out_time"), field="check_out_time", errors=errors
            )

    if "check_in_latest_time" in data:
        # null / "" clears the upper bound
        if data.get("check_in_latest_time") in (None, ""):
            check_in_latest = None
        else:
            check_in_latest = _parse_time(
                data.get("check_in_latest_time"),
                field="check_in_latest_time",
                errors=errors,
            )

    if "guest_checkin_opens_days_before" in data:
        raw = data.get("guest_checkin_opens_days_before")
        try:
            days = int(raw)
        except (TypeError, ValueError):
            errors["guest_checkin_opens_days_before"] = "Must be an integer."
        else:
            if days < 0 or days > MAX_GUEST_CHECKIN_OPENS_DAYS:
                errors["guest_checkin_opens_days_before"] = (
                    f"Must be between 0 and {MAX_GUEST_CHECKIN_OPENS_DAYS}."
                )

    # Cross-field: latest should be after check-in when both present in payload.
    # When only latest is patched, callers should still send check_in for full validation;
    # we only compare values we parsed from this payload.
    if (
        check_in is not None
        and check_in_latest is not None
        and "check_in_latest_time" not in errors
        and check_in_latest <= check_in
    ):
        errors["check_in_latest_time"] = "Latest arrival must be after check-in time."

    if (
        check_in is not None
        and check_out is not None
        and "check_in_time" not in errors
        and "check_out_time" not in errors
        and check_in == check_out
    ):
        errors["check_out_time"] = "Check-out time must differ from check-in time."

    if errors:
        raise SectionSettingsValidationError(errors)


def _parse_bool_field(
    value: Any, *, field: str, errors: dict[str, str]
) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
    errors[field] = "Must be a boolean."
    return None


def validate_automation_settings_payload(data: dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise SectionSettingsValidationError({"_": "Payload must be an object."})

    errors: dict[str, str] = {}

    if "after_hours_arrival_policy" in data:
        policy = str(data.get("after_hours_arrival_policy") or "").strip()
        if policy not in _AFTER_HOURS_POLICIES:
            errors["after_hours_arrival_policy"] = (
                "Invalid policy. "
                f"Allowed: {', '.join(sorted(_AFTER_HOURS_POLICIES))}."
            )

    if "after_hours_contact_phone" in data:
        phone = str(data.get("after_hours_contact_phone") or "").strip()
        if len(phone) > MAX_AFTER_HOURS_PHONE_LENGTH:
            errors["after_hours_contact_phone"] = (
                f"Must be at most {MAX_AFTER_HOURS_PHONE_LENGTH} characters."
            )

    if "guest_arrival_auto_reply_enabled" in data:
        _parse_bool_field(
            data.get("guest_arrival_auto_reply_enabled"),
            field="guest_arrival_auto_reply_enabled",
            errors=errors,
        )

    if "guest_parking_auto_reply_enabled" in data:
        _parse_bool_field(
            data.get("guest_parking_auto_reply_enabled"),
            field="guest_parking_auto_reply_enabled",
            errors=errors,
        )

    if "guest_invoice_auto_reply_enabled" in data:
        _parse_bool_field(
            data.get("guest_invoice_auto_reply_enabled"),
            field="guest_invoice_auto_reply_enabled",
            errors=errors,
        )

    if errors:
        raise SectionSettingsValidationError(errors)
