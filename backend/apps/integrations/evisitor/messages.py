"""Parse and format eVisitor API user-facing messages."""

from __future__ import annotations

import json
import re

_UUID_RE = re.compile(
    r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}",
    re.IGNORECASE,
)


def format_evisitor_user_message(raw: str) -> str:
    """
    Turn eVisitor templated UserMessage into readable Croatian text.

    Example input:
      [[[Osoba %0 %1 je već prijavljena... %3|||Lauriane|||Saulnier|||20.5.2026.|||uuid]]]
    """
    text = (raw or "").strip()
    if not text:
        return ""

    if "|||" in text and ("[[[" in text or "%0" in text):
        parts = text.split("|||")
        # Drop template prefix before first placeholder value
        values = [p.strip().rstrip("]]]").strip() for p in parts[1:] if p.strip()]
        if len(values) >= 4:
            first, last, stay_date, reg_id = values[0], values[1], values[2], values[3]
            return (
                f"Osoba {first} {last} je već prijavljena na datum {stay_date} "
                f"te nije odjavljena. ID postojeće prijave: {reg_id}"
            )
        if values:
            return " ".join(values)

    # Strip Rhetos template wrappers if present
    text = text.removeprefix("[[[").removesuffix("]]]").strip()
    return text


def _user_message_from_system_payload(system_message: str) -> str:
    text = (system_message or "").strip()
    if not text:
        return ""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("UserMessage") or "")


_GENERIC_HTTP_ERROR_RE = re.compile(r"^eVisitor .+ HTTP \d+$", re.IGNORECASE)


def _is_generic_http_error(text: str) -> bool:
    return bool(_GENERIC_HTTP_ERROR_RE.match((text or "").strip()))


def resolve_evisitor_error_message(
    *,
    user_message: str = "",
    system_message: str = "",
    fallback: str = "",
) -> str:
    """Return the best human-readable eVisitor error for UI/API."""
    sources: list[str] = []
    user_text = (user_message or "").strip()
    system_user_message = _user_message_from_system_payload(system_message)

    if user_text and not _is_generic_http_error(user_text):
        sources.append(user_text)
    if system_user_message and system_user_message not in sources:
        sources.append(system_user_message)
    if user_text and user_text not in sources:
        sources.append(user_text)

    for raw in sources:
        formatted = format_evisitor_user_message(raw)
        if formatted:
            return formatted
    return (fallback or "").strip()


def parse_existing_registration_id(user_message: str) -> str | None:
    """Extract existing eVisitor registration UUID from 'već prijavljena' errors."""
    formatted = format_evisitor_user_message(user_message)
    haystack = f"{user_message}\n{formatted}".lower()
    if "već prijavljena" not in haystack:
        return None

    if "|||" in user_message:
        parts = user_message.split("|||")
        for part in reversed(parts):
            candidate = part.strip().rstrip("]]]").strip()
            if _UUID_RE.fullmatch(candidate):
                return candidate.lower()

    matches = _UUID_RE.findall(user_message)
    if matches:
        return matches[-1].lower()
    return None


def is_already_checked_out_message(user_message: str) -> bool:
    """True when CheckOutTourist reports the stay is gone / already out / annulled.

    eVisitor often auto-checks-out on ``ForeseenStayUntil``. A later
    ``CheckOutTourist`` then returns this ambiguous OR-message. For guests we
    already successfully checked in, treat it as idempotent checkout success.

    Do **not** call ``CancelTouristCheckOut`` to probe — that reopens the stay
    and can block a subsequent checkout after the edit window ("rok izmjena").
    """
    text = format_evisitor_user_message(user_message) or (user_message or "")
    lowered = text.lower()
    return (
        "ne postoji prijava sa zadanim id" in lowered
        or ("već odjavljena" in lowered and "poništena" in lowered)
        or "vec odjavljena" in lowered
        or "je već odjavljena ili poništena" in lowered
    )
