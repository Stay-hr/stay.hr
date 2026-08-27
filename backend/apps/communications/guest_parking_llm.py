"""LLM analysis and reply composition for guest parking questions."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from apps.ai.provider import GuestComposeError, complete_chat_json, llm_model, prompt_version
from apps.communications.guest_language_context import LanguageMode
from apps.communications.guest_language_policy import normalize_reply_language
from apps.communications.guest_language_resolver import GuestLanguageResolver
from apps.communications.guest_parking_patterns import classify_parking_only
from apps.communications.guest_reply_sanitize import strip_reply_boilerplate
from apps.communications.language_detection import detect
from apps.properties.guest_info import build_guest_facts_for_llm, render_parking_reply_text
from apps.reservations.models import Reservation

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParkingLlmResult:
    is_parking_related: bool
    # The language the model claims for its own reply, normalized but never
    # resolved: the caller compares it against the final language, so it must
    # stay the raw claim.
    reported_reply_language: str
    reply_text: str


def build_parking_llm_context(
    reservation: Reservation,
    body: str,
    *,
    channel: str,
) -> dict:
    prop = reservation.property
    ctx = GuestLanguageResolver.resolve(
        reservation,
        mode=LanguageMode.REACTIVE,
        message_text=body,
    )
    lang = ctx.language
    detection = detect(body)
    return {
        "guest_message": (body or "").strip(),
        "channel": channel,
        "detected_language": detection.language,
        "detection_confidence": detection.confidence,
        "reservation": {
            "status": reservation.status,
            "check_in_date": reservation.check_in.isoformat(),
            "guest_name": reservation.booker_name or "",
            "booking_code": reservation.booking_code or reservation.external_id or "",
            "notes": (reservation.notes or "").strip(),
        },
        "guest_facts": build_guest_facts_for_llm(prop, lang),
    }


def _system_prompt() -> str:
    return (
        "You are a professional hotel reception assistant. "
        "Analyze the guest message and decide if it is ONLY about parking "
        "(not arrival time or check-in). "
        "If yes, write a short reply using ONLY facts from guest_facts.parking in the JSON context. "
        "Never invent prices, zones, or policies not present in the context. "
        "Keep a warm, concise reception tone.\n\n"
        "Return the answer body ONLY: 1-3 sentences, no greeting, no sign-off, "
        "no property name, no links, no footer — those are added afterwards.\n\n"
        "Language: reply in the dominant language of guest_message. Isolated foreign words, "
        "a foreign greeting, or emoji must NOT change the language. If unsure, use "
        "detected_language. reply_language must name the language you actually wrote in.\n\n"
        "Return JSON only with keys:\n"
        "- is_parking_related (boolean)\n"
        "- reply_language (ISO 639-1)\n"
        "- reply_text (empty string if not parking-related)"
    )


def _user_prompt(context: dict) -> str:
    return (
        "Analyze the guest message and compose a parking reply if it is a parking-only question.\n"
        f"Data (JSON):\n{json.dumps(context, ensure_ascii=False, indent=2)}"
    )


def _parse_llm_response(raw: dict, *, property_name: str = "") -> ParkingLlmResult:
    is_parking = bool(raw.get("is_parking_related"))
    reply_text = strip_reply_boilerplate(
        (raw.get("reply_text") or "").strip(),
        property_name=property_name,
    )

    if is_parking and not reply_text:
        raise GuestComposeError("LLM marked parking-related but returned no usable reply_text")

    return ParkingLlmResult(
        is_parking_related=is_parking,
        reported_reply_language=normalize_reply_language(raw.get("reply_language")) or "",
        reply_text=reply_text,
    )


def analyze_and_compose_parking_reply(
    reservation: Reservation,
    body: str,
    *,
    channel: str,
) -> ParkingLlmResult:
    context = build_parking_llm_context(reservation, body, channel=channel)
    raw = complete_chat_json(_system_prompt(), _user_prompt(context))
    result = _parse_llm_response(raw, property_name=reservation.property.name)
    logger.info(
        "parking LLM analyzed reservation_id=%s related=%s reported=%s model=%s",
        reservation.pk,
        result.is_parking_related,
        result.reported_reply_language,
        llm_model(),
    )
    return result


def parking_llm_audit_fields() -> dict[str, str]:
    return {"llm_model": llm_model(), "prompt_version": prompt_version()}


def build_parking_auto_reply(
    reservation: Reservation,
    body: str,
    *,
    language: str | None = None,
) -> str:
    ctx = GuestLanguageResolver.resolve(
        reservation,
        mode=LanguageMode.REACTIVE,
        reply_language=language,
        message_text=body,
    )
    lang = ctx.language
    return render_parking_reply_text(
        reservation.property,
        lang,
        variant="standard",
        reservation_notes=reservation.notes or "",
    )


def is_parking_only_message(text: str) -> bool:
    return classify_parking_only(text)
