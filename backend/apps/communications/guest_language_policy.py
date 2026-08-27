"""Pure guest language selection policy (no DB access)."""

from __future__ import annotations

from datetime import datetime

from apps.communications.conversation_language_store import StoredConversationLanguage
from apps.communications.guest_language_constants import (
    CANONICAL_LANGUAGE_DEFAULT,
    LLM_REPLY_LANGS,
    MESSAGE_OVERRIDES_LLM_THRESHOLD,
    normalize_iso639_1,
)
from apps.communications.guest_language_context import (
    GuestLanguageContext,
    LanguageMode,
    LanguageSource,
)
from apps.communications.language_detection import DetectionResult, detect
from apps.reservations.nationality_display import normalize_country_iso2

HR_COUNTRIES = frozenset({"HR", "RS", "BA", "ME", "SI", "MK"})
DE_COUNTRIES = frozenset({"DE", "AT", "CH", "LI"})
ES_COUNTRIES = frozenset(
    {
        "ES",
        "MX",
        "AR",
        "CO",
        "CL",
        "PE",
        "VE",
        "EC",
        "UY",
        "PY",
        "BO",
        "CR",
        "PA",
        "DO",
        "GT",
        "HN",
        "NI",
        "SV",
        "CU",
        "PR",
    }
)
FR_COUNTRIES = frozenset({"FR", "MC", "LU"})


def language_from_country(iso2: str) -> str:
    """Map ISO2 country code to guest reply language."""
    code = normalize_country_iso2(iso2)
    if not code:
        return CANONICAL_LANGUAGE_DEFAULT
    if code in HR_COUNTRIES:
        return "hr"
    if code in DE_COUNTRIES:
        return "de"
    if code in ES_COUNTRIES:
        return "es"
    if code in FR_COUNTRIES:
        return "fr"
    if code == "IT":
        return "it"
    if code == "PL":
        return "pl"
    if code == "RO":
        return "ro"
    if code == "NL":
        return "nl"
    if code == "CZ":
        return "cs"
    if code == "HU":
        return "hu"
    if code == "UA":
        return "ua"
    if code == "PT":
        return "pt"
    if code == "GR":
        return "el"
    if code == "SK":
        return "sk"
    return CANONICAL_LANGUAGE_DEFAULT


def normalize_reply_language(raw: str | None) -> str | None:
    """Normalize an LLM-reported language, or None when it is not a valid code."""
    base = normalize_iso639_1(raw)
    if not base or base not in LLM_REPLY_LANGS:
        return None
    return base


def _reason_for(source: LanguageSource, *, detail: str = "") -> str:
    if detail:
        return detail
    return {
        LanguageSource.OVERRIDE: "API language override",
        LanguageSource.REPLY_LANGUAGE: "LLM reply_language",
        LanguageSource.MESSAGE: "detected from inbound message",
        LanguageSource.CONVERSATION: "conversation language from prior message",
        LanguageSource.COUNTRY: "reservation country",
        LanguageSource.TENANT_DEFAULT: "tenant default language",
        LanguageSource.FALLBACK: f"fallback ({CANONICAL_LANGUAGE_DEFAULT})",
    }.get(source, source.value)


def _message_context(
    detection: DetectionResult,
    *,
    mode: LanguageMode,
    received_at: datetime | None,
    channel: str,
) -> GuestLanguageContext:
    channel_part = f" {channel}" if channel else ""
    detail = f"detected from inbound{channel_part} message"
    if received_at:
        detail = f"{detail} {received_at.isoformat()}"
    return GuestLanguageContext(
        language=detection.language,
        source=LanguageSource.MESSAGE,
        confidence=detection.confidence,
        mode=mode,
        reason=detail,
    )


def choose(
    *,
    mode: LanguageMode,
    override: str | None = None,
    reply_language: str | None = None,
    message_detection: DetectionResult | None = None,
    message_received_at: datetime | None = None,
    message_channel: str = "",
    conversation: StoredConversationLanguage | None = None,
    country_iso2: str = "",
    tenant_default: str = "",
    property_language: str = "",
) -> GuestLanguageContext:
    """
    Select guest reply language.

    REACTIVE: override → high-confidence message → reply_language → message
              → conversation → country → tenant_default → en
    PROACTIVE: override → country → tenant_default → en

    A message detected with confidence >= MESSAGE_OVERRIDES_LLM_THRESHOLD beats
    the language the LLM reports for itself: an English message with a single
    foreign filler word must not be answered in that foreign language.
    """
    override_lang = normalize_iso639_1(override)
    if override_lang:
        return GuestLanguageContext(
            language=override_lang,
            source=LanguageSource.OVERRIDE,
            confidence=1.0,
            mode=mode,
            reason=_reason_for(LanguageSource.OVERRIDE, detail=f"API language override = {override_lang}"),
        )

    if mode == LanguageMode.REACTIVE:
        detected = (
            message_detection
            if message_detection and message_detection.language not in ("", "unknown")
            else None
        )
        if detected and detected.confidence >= MESSAGE_OVERRIDES_LLM_THRESHOLD:
            return _message_context(
                detected,
                mode=mode,
                received_at=message_received_at,
                channel=message_channel,
            )

        llm_lang = normalize_reply_language(reply_language)
        if llm_lang:
            return GuestLanguageContext(
                language=llm_lang,
                source=LanguageSource.REPLY_LANGUAGE,
                confidence=0.9,
                mode=mode,
                reason=_reason_for(
                    LanguageSource.REPLY_LANGUAGE,
                    detail=f"LLM reply_language = {llm_lang}",
                ),
            )

        if detected:
            return _message_context(
                detected,
                mode=mode,
                received_at=message_received_at,
                channel=message_channel,
            )

        if conversation and conversation.language:
            return GuestLanguageContext(
                language=conversation.language,
                source=LanguageSource.CONVERSATION,
                confidence=0.75,
                mode=mode,
                reason=_reason_for(
                    LanguageSource.CONVERSATION,
                    detail=f"conversation language from prior message ({conversation.language})",
                ),
            )

    country = normalize_country_iso2(country_iso2)
    if country:
        lang = language_from_country(country)
        return GuestLanguageContext(
            language=lang,
            source=LanguageSource.COUNTRY,
            confidence=0.5,
            mode=mode,
            reason=_reason_for(
                LanguageSource.COUNTRY,
                detail=f"reservation country = {country}",
            ),
        )

    for candidate in (tenant_default, property_language):
        base = normalize_iso639_1(candidate)
        if base:
            return GuestLanguageContext(
                language=base,
                source=LanguageSource.TENANT_DEFAULT,
                confidence=0.4,
                mode=mode,
                reason=_reason_for(
                    LanguageSource.TENANT_DEFAULT,
                    detail=f"tenant/property default = {base}",
                ),
            )

    return GuestLanguageContext(
        language=CANONICAL_LANGUAGE_DEFAULT,
        source=LanguageSource.FALLBACK,
        confidence=0.0,
        mode=mode,
        reason=_reason_for(LanguageSource.FALLBACK),
    )


def detect_from_text(text: str) -> DetectionResult:
    """Convenience wrapper used by resolver."""
    return detect(text)
