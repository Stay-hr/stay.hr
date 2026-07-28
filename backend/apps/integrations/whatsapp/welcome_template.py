"""WhatsApp welcome template registry and deterministic resolver (ADR 0011).

Resolution order (fixed; every result records ``source`` + ``match``)::

    1. normalize language          (uk→ua, en-US→en-us then base en, …)
    2. try exact key, then base
       a. property config welcome map
       b. platform config welcome map
       c. WELCOME_TEMPLATE_REGISTRY DEFAULT
    3. english fallback            (stay_welcome_en + meta en, source=ENGLISH)

Callers must send ``resolved.template_name`` with ``resolved.meta_language``
together — never choose name and Meta language independently.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping

from django.core.exceptions import ImproperlyConfigured

from apps.communications.guest_compose import build_compose_context
from apps.communications.guest_language_context import LanguageMode
from apps.communications.guest_language_resolver import GuestLanguageResolver
from apps.communications.guest_email import _email_context
from apps.reservations.models import Reservation

logger = logging.getLogger(__name__)

DEFAULT_WELCOME_HEADER_IMAGE = "https://stay.hr/static/whatsapp-header.png"


@dataclass(frozen=True)
class TemplateDefinition:
    """Immutable welcome template entry: Graph name + Meta language code."""

    template_name: str
    meta_language: str


# Single source of truth for Meta-APPROVED welcome templates (ADR 0011).
# Derived: META_APPROVED_LANGUAGES, DEFAULT_WELCOME_TEMPLATES, seed maps, allowlists.
_WELCOME_TEMPLATE_REGISTRY_DATA: dict[str, TemplateDefinition] = {
    "cs": TemplateDefinition("stay_welcome_cs", "cs"),
    "de": TemplateDefinition("stay_welcome_de", "de"),
    "en": TemplateDefinition("stay_welcome_en", "en"),
    "es": TemplateDefinition("stay_welcome_es", "es"),
    "fr": TemplateDefinition("stay_welcome_fr", "fr"),
    "hr": TemplateDefinition("stay_welcome_hr", "hr"),
    "hu": TemplateDefinition("stay_welcome_hu", "hu"),
    "it": TemplateDefinition("stay_welcome_it", "it"),
    "lt": TemplateDefinition("stay_welcome_lt", "lt"),
    "nl": TemplateDefinition("stay_welcome_nl", "nl"),
    "pl": TemplateDefinition("stay_welcome_pl", "pl"),
    "ro": TemplateDefinition("stay_welcome_ro", "ro"),
    "sk": TemplateDefinition("stay_welcome_sk", "sk"),
    "ua": TemplateDefinition("stay_welcome_ua", "uk"),
}


def _validate_welcome_template_registry(
    registry: Mapping[str, TemplateDefinition],
) -> None:
    if not registry:
        raise ImproperlyConfigured("WELCOME_TEMPLATE_REGISTRY must be non-empty")
    for required in ("en", "hr"):
        if required not in registry:
            raise ImproperlyConfigured(
                f"WELCOME_TEMPLATE_REGISTRY missing required key {required!r}"
            )
    names: set[str] = set()
    for key, definition in registry.items():
        if key != key.lower():
            raise ImproperlyConfigured(
                f"WELCOME_TEMPLATE_REGISTRY key must be lowercase: {key!r}"
            )
        if not definition.template_name.startswith("stay_welcome_"):
            raise ImproperlyConfigured(
                f"WELCOME_TEMPLATE_REGISTRY[{key!r}].template_name must start "
                f"with 'stay_welcome_', got {definition.template_name!r}"
            )
        if definition.template_name in names:
            raise ImproperlyConfigured(
                f"WELCOME_TEMPLATE_REGISTRY duplicate template_name "
                f"{definition.template_name!r}"
            )
        names.add(definition.template_name)
    ua = registry.get("ua")
    if ua is not None and ua.meta_language != "uk":
        raise ImproperlyConfigured(
            f"WELCOME_TEMPLATE_REGISTRY['ua'].meta_language must be 'uk', "
            f"got {ua.meta_language!r}"
        )


_validate_welcome_template_registry(_WELCOME_TEMPLATE_REGISTRY_DATA)

WELCOME_TEMPLATE_REGISTRY: Mapping[str, TemplateDefinition] = MappingProxyType(
    _WELCOME_TEMPLATE_REGISTRY_DATA
)

# Derived from registry — do not hand-maintain a parallel approved list.
META_APPROVED_LANGUAGES: frozenset[str] = frozenset(WELCOME_TEMPLATE_REGISTRY.keys())

# Backward-compatible name→template map (seed / Meta create commands).
DEFAULT_WELCOME_TEMPLATES: Mapping[str, str] = MappingProxyType(
    {key: defn.template_name for key, defn in WELCOME_TEMPLATE_REGISTRY.items()}
)

# Guest/country language key → Meta template language when they differ (ua → uk).
WELCOME_META_LANGUAGE_CODES: Mapping[str, str] = MappingProxyType(
    {
        key: defn.meta_language
        for key, defn in WELCOME_TEMPLATE_REGISTRY.items()
        if defn.meta_language != key
    }
)


class ResolutionSource(StrEnum):
    PROPERTY = "property"
    PLATFORM = "platform"
    DEFAULT = "default"
    ENGLISH = "english"


class ResolutionMatch(StrEnum):
    EXACT = "exact"  # won on exact normalized key
    BASE = "base"  # won on base language (e.g. en-US → en)


@dataclass(frozen=True)
class ResolvedWelcomeTemplate:
    template_name: str
    meta_language: str
    requested_language: str  # raw input (as received)
    resolved_language: str  # key that actually matched
    source: ResolutionSource  # which layer won
    match: ResolutionMatch  # exact vs base within that layer


def normalize_language(language: str | None) -> str:
    """Normalize a guest/language tag to an internal registry lookup key.

    - Empty / None → ``en``
    - Lowercase; ``_`` → ``-``
    - ISO Ukrainian ``uk`` → internal key ``ua`` (base and regional)
    - Regional tags keep the region (``en-US`` → ``en-us``) so the resolver
      can try exact then base.
    """
    if language is None:
        return "en"
    raw = str(language).strip()
    if not raw:
        return "en"
    normalized = raw.lower().replace("_", "-")
    base, sep, rest = normalized.partition("-")
    if base == "uk":
        base = "ua"
    return f"{base}{sep}{rest}" if sep else base


def _base_language(normalized: str) -> str:
    return normalized.split("-", 1)[0]


def _extract_welcome_map(config: dict[str, Any] | None) -> dict[str, str]:
    if not config:
        return {}
    templates_cfg = config.get("whatsapp_templates") or {}
    welcome = templates_cfg.get("welcome") or {}
    if not isinstance(welcome, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in welcome.items():
        name = str(value or "").strip()
        if not name:
            continue
        out[str(key).strip().lower()] = name
    return out


def _meta_language_for_key(key: str) -> str:
    definition = WELCOME_TEMPLATE_REGISTRY.get(key)
    if definition is not None:
        return definition.meta_language
    base = _base_language(key)
    base_defn = WELCOME_TEMPLATE_REGISTRY.get(base)
    if base_defn is not None:
        return base_defn.meta_language
    return key


def _log_welcome_template_resolved(resolved: ResolvedWelcomeTemplate) -> None:
    """Structured resolve audit (Phase 4). WARNING when english fallback wins."""
    msg = (
        "welcome_template_resolved "
        f"requested={resolved.requested_language} "
        f"resolved={resolved.resolved_language} "
        f"source={resolved.source.value} "
        f"match={resolved.match.value} "
        f"template={resolved.template_name} "
        f"meta_language={resolved.meta_language}"
    )
    if resolved.source is ResolutionSource.ENGLISH:
        logger.warning(msg)
    else:
        logger.info(msg)


def resolve_welcome_template(
    *,
    language: str | None,
    property_config: dict[str, Any] | None = None,
    platform_config: dict[str, Any] | None = None,
) -> ResolvedWelcomeTemplate:
    """Resolve ``(template_name, meta_language)`` deterministically; never ``None``.

    See module docstring for the full resolution order (ADR 0011).
    Emits ``welcome_template_resolved`` (WARNING on ``source=english``).
    """
    requested = "" if language is None else str(language)
    exact = normalize_language(language)
    base = _base_language(exact)
    candidates: list[tuple[str, ResolutionMatch]] = [
        (exact, ResolutionMatch.EXACT),
    ]
    if base != exact:
        candidates.append((base, ResolutionMatch.BASE))

    property_welcome = _extract_welcome_map(property_config)
    platform_welcome = _extract_welcome_map(platform_config)
    layers: list[tuple[ResolutionSource, Mapping[str, str] | None]] = [
        (ResolutionSource.PROPERTY, property_welcome),
        (ResolutionSource.PLATFORM, platform_welcome),
        (ResolutionSource.DEFAULT, None),  # registry
    ]

    resolved: ResolvedWelcomeTemplate | None = None
    for key, match in candidates:
        for source, welcome_map in layers:
            if source is ResolutionSource.DEFAULT:
                definition = WELCOME_TEMPLATE_REGISTRY.get(key)
                if definition is None:
                    continue
                resolved = ResolvedWelcomeTemplate(
                    template_name=definition.template_name,
                    meta_language=definition.meta_language,
                    requested_language=requested,
                    resolved_language=key,
                    source=ResolutionSource.DEFAULT,
                    match=match,
                )
                break
            assert welcome_map is not None
            template_name = welcome_map.get(key)
            if not template_name:
                continue
            resolved = ResolvedWelcomeTemplate(
                template_name=template_name,
                meta_language=_meta_language_for_key(key),
                requested_language=requested,
                resolved_language=key,
                source=source,
                match=match,
            )
            break
        if resolved is not None:
            break

    if resolved is None:
        english = WELCOME_TEMPLATE_REGISTRY["en"]
        resolved = ResolvedWelcomeTemplate(
            template_name=english.template_name,
            meta_language=english.meta_language,
            requested_language=requested,
            resolved_language="en",
            source=ResolutionSource.ENGLISH,
            match=ResolutionMatch.EXACT,
        )

    _log_welcome_template_resolved(resolved)
    return resolved


def welcome_header_image_url(config: dict[str, Any]) -> str:
    templates_cfg = config.get("whatsapp_templates") or {}
    header = str(templates_cfg.get("header_image_url") or "").strip()
    return header or DEFAULT_WELCOME_HEADER_IMAGE


def _first_name(reservation: Reservation) -> str:
    booker = (reservation.booker_name or "").strip()
    if booker:
        return booker.split()[0]
    primary = reservation.guests.filter(is_primary=True).first()
    if primary and (primary.first_name or "").strip():
        return primary.first_name.strip()
    return booker or "Guest"


def build_welcome_template_parameters(reservation: Reservation) -> tuple[str, list[str]]:
    """Return (language_code, five positional body parameters for stay_welcome_* templates)."""
    ctx = GuestLanguageResolver.resolve(reservation, mode=LanguageMode.PROACTIVE)
    lang = ctx.language
    ctx = build_compose_context(reservation, language=lang)
    email_ctx = _email_context(reservation)

    params = [
        _first_name(reservation),
        ctx["booking_code"] or str(reservation.pk),
        ctx["property_name"],
        email_ctx["check_in_display"],
        email_ctx["check_out_display"],
    ]
    return lang, params
