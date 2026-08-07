"""Deterministic residence-address normalization for machine sources (OCR, …).

Invariant
---------
The normalizer must never produce an address that is not strictly derived from
the original input text. Only deterministic transforms are allowed: reordering
existing segments, and dropping postal code / country when the eVisitor
``City, street`` format requires it. It must not invent, translate, expand, or
guess content.

``validate_evisitor_residence_address`` remains the sole authority for whether
a candidate is acceptable for persistence.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from django.conf import settings

from apps.integrations.evisitor.residence_address import (
    validate_evisitor_residence_address,
)

logger = logging.getLogger("apps.reservations.address_normalizer.metrics")

# FR/EU street tokens (plus HR prefixes used on bilingual IDs).
_STREET_TOKENS = (
    "rue",
    "chemin",
    "avenue",
    "av",
    "ave",
    "bd",
    "blvd",
    "boulevard",
    "place",
    "allée",
    "allee",
    "impasse",
    "route",
    "quai",
    "cours",
    "ulica",
    "ul",
    "avenija",
    "cesta",
    "put",
    "trg",
    "obala",
)

# street, POSTAL CITY[, COUNTRY]
_FR_POSTAL_COMMA_RE = re.compile(
    r"^(?P<street>.+),\s*(?P<postal>\d{5})\s+(?P<city>[^,]+?)(?:\s*,\s*(?P<country>.+))?$",
    re.UNICODE,
)

# street - POSTAL City
_FR_POSTAL_DASH_RE = re.compile(
    r"^(?P<street>.+?)\s*[-–—]\s*(?P<postal>\d{5})\s+(?P<city>.+)$",
    re.UNICODE,
)


@dataclass(frozen=True)
class AddressNormalizeResult:
    original: str
    normalized: str | None
    strategy: str
    applied: bool
    success: bool


def normalize_address(raw: str, *, source: str = "ocr") -> AddressNormalizeResult:
    """Normalize a residence address for a known machine ``source``.

    Today only ``source=\"ocr\"`` is implemented. Other sources are a no-op so
    callers can share one API without inventing content for unhandled paths.
    """
    original = (raw or "").strip()
    empty = AddressNormalizeResult(
        original=original,
        normalized=None,
        strategy="",
        applied=False,
        success=False,
    )
    if not original:
        return empty

    if source != "ocr":
        return empty

    if not getattr(settings, "OCR_ADDRESS_NORMALIZATION_ENABLED", True):
        return empty

    # Idempotent: already eVisitor-valid → do not rewrite.
    if validate_evisitor_residence_address(original).valid:
        return empty

    for strategy, matcher in (
        ("fr_postal_city", _FR_POSTAL_COMMA_RE),
        ("fr_postal_city_dash", _FR_POSTAL_DASH_RE),
    ):
        match = matcher.match(original)
        if not match:
            continue
        street = (match.group("street") or "").strip()
        city = (match.group("city") or "").strip()
        if not street or not city:
            continue
        if not _looks_like_street(street):
            continue
        if city[:1].isdigit():
            continue

        # Reorder existing segments only — no casing/translation changes.
        candidate = f"{city}, {street}"
        validation = validate_evisitor_residence_address(candidate)
        if not validation.valid:
            logger.info(
                "ocr_address_normalized strategy=%s success=false",
                strategy,
            )
            continue

        normalized = validation.normalized_address or candidate
        result = AddressNormalizeResult(
            original=original,
            normalized=normalized,
            strategy=strategy,
            applied=True,
            success=True,
        )
        logger.info(
            "ocr_address_normalized strategy=%s success=true original=%r normalized=%r",
            strategy,
            original,
            normalized,
        )
        return result

    # Recognizable attempt failed, or no pattern matched.
    if _FR_POSTAL_COMMA_RE.match(original) or _FR_POSTAL_DASH_RE.match(original):
        logger.info(
            "ocr_address_normalized strategy=%s success=false",
            "fr_postal_city",
        )
    return empty


def _looks_like_street(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    if any(ch.isdigit() for ch in raw):
        return True
    first = raw.split()[0].casefold().rstrip(".")
    lowered = raw.casefold()
    for token in _STREET_TOKENS:
        t = token.casefold()
        if lowered == t or lowered.startswith(t + " ") or lowered.startswith(t + "."):
            return True
        if first == t.rstrip("."):
            return True
    return False
