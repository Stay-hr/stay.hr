"""Conservative CityOfResidence extraction for eVisitor.

Business rule: only accept an address when CityOfResidence can be determined
reliably. Prefer rejecting over guessing. ``normalized_address`` is set only
when ``valid=True``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from apps.reservations.mrz_parse import normalize_residence_address

_STREET_PREFIXES = (
    "ulica",
    "ul.",
    "avenija",
    "ave.",
    "trg",
    "obala",
    "cesta",
    "put",
    "šetalište",
    "setaliste",
    "aleja",
)

# Trailing house number: 208, 15A, 208 A
_HOUSE_NUMBER_RE = re.compile(
    r"^(?P<city>.+?)\s+(?P<house>\d+[A-Za-z]?(?:\s+[A-Za-z])?)$",
    re.UNICODE,
)

# Common Slavic street-name endings (no-comma 3a must not treat these as city words).
_STREETISH_TOKEN_RE = re.compile(
    r"(ska|čka|ška|ova|eva)$",
    re.IGNORECASE | re.UNICODE,
)

_MAX_CITY_LEN = 64
_MAX_CITY_WORDS_COMMA = 5
_MAX_CITY_WORDS_NO_COMMA = 4

MSG_REQUIRED = "Adresa je obavezna."
MSG_STREET_FIRST = "Grad mora biti prvi dio adrese."
MSG_DIGIT_IN_CITY = "Naziv grada ne smije sadržavati broj."
MSG_CITY_TOO_LONG = "Naziv grada je predugačak."
MSG_CANNOT_DETERMINE = "Nije moguće odrediti grad prebivališta (CityOfResidence)."


@dataclass(frozen=True)
class AddressValidationResult:
    valid: bool
    city: str = ""
    normalized_address: str = ""
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


def validate_evisitor_residence_address(address: str) -> AddressValidationResult:
    """Return structured city/normalized address or fail closed."""
    raw = (address or "").strip()
    if not raw:
        return _fail(MSG_REQUIRED)

    # Postal-strip when comma present; not a successful result by itself.
    stripped = normalize_residence_address(raw)

    if "," in stripped:
        return _validate_comma_form(stripped)

    return _validate_no_comma_form(stripped)


def _fail(*errors: str) -> AddressValidationResult:
    return AddressValidationResult(
        valid=False,
        city="",
        normalized_address="",
        errors=tuple(errors),
    )


def _ok(
    city: str,
    normalized_address: str,
    *,
    warnings: tuple[str, ...] = (),
) -> AddressValidationResult:
    return AddressValidationResult(
        valid=True,
        city=city,
        normalized_address=normalized_address,
        warnings=warnings,
    )


def _word_count(text: str) -> int:
    return len([w for w in text.split() if w])


def _has_digit(text: str) -> bool:
    return any(ch.isdigit() for ch in text)


def _looks_like_street_segment(text: str) -> bool:
    """True when segment looks like a street (prefix and/or house number)."""
    raw = (text or "").strip()
    if not raw:
        return False
    if _has_digit(raw):
        return True
    first = raw.split()[0].casefold().rstrip(".")
    lowered = raw.casefold()
    for prefix in _STREET_PREFIXES:
        p = prefix.casefold()
        if lowered == p or lowered.startswith(p + " ") or lowered.startswith(p + "."):
            return True
        if first == p.rstrip("."):
            return True
    return False


def _city_has_streetish_token(city: str) -> bool:
    """Reject no-comma parses where a city token looks like a street name."""
    for word in city.split():
        w = word.casefold().rstrip(".")
        if w in {p.rstrip(".") for p in _STREET_PREFIXES}:
            return True
        if _STREETISH_TOKEN_RE.search(w):
            return True
    return False


def _city_shape_errors(city: str, *, max_words: int) -> str | None:
    if not city.strip():
        return MSG_CANNOT_DETERMINE
    if _has_digit(city):
        return MSG_DIGIT_IN_CITY
    if len(city) > _MAX_CITY_LEN or _word_count(city) > max_words:
        return MSG_CITY_TOO_LONG
    return None


def _validate_comma_form(address: str) -> AddressValidationResult:
    city_part, rest = address.split(",", 1)
    city = city_part.strip()
    rest = rest.strip()

    # Street-first (#190) or any digit in city segment — do not auto-swap.
    if _looks_like_street_segment(city):
        return _fail(MSG_STREET_FIRST)
    if _has_digit(city):
        return _fail(MSG_DIGIT_IN_CITY)

    shape_err = _city_shape_errors(city, max_words=_MAX_CITY_WORDS_COMMA)
    if shape_err:
        return _fail(shape_err)

    if not rest:
        return _ok(city, city)

    return _ok(city, f"{city}, {rest}")


def _validate_no_comma_form(address: str) -> AddressValidationResult:
    match = _HOUSE_NUMBER_RE.match(address.strip())
    if not match:
        return _fail(MSG_CANNOT_DETERMINE)

    city = match.group("city").strip()
    house = match.group("house").strip()

    if _looks_like_street_segment(city):
        return _fail(MSG_CANNOT_DETERMINE)

    if _city_has_streetish_token(city):
        return _fail(MSG_CANNOT_DETERMINE)

    shape_err = _city_shape_errors(city, max_words=_MAX_CITY_WORDS_NO_COMMA)
    if shape_err:
        # Map length/digit errors; ambiguous multi-token blobs also hit word limit.
        if shape_err == MSG_CITY_TOO_LONG:
            return _fail(MSG_CANNOT_DETERMINE)
        return _fail(shape_err)

    # Unambiguous: city + house-number-only remainder.
    normalized = f"{city}, {house}"
    return _ok(
        city,
        normalized,
        warnings=("Adresa normalizirana u format Grad, kućni broj.",),
    )
