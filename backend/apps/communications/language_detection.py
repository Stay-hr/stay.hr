"""Heuristic guest message language detection."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_EMOJI_ONLY = re.compile(
    r"^[\s\U0001F300-\U0001FAFF\U00002600-\U000026FF\U00002700-\U000027BF]+$"
)
_TOKEN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)

# Hits needed for a detection to be trusted over an LLM-reported language.
_HIGH_CONFIDENCE_HITS = 3
_HIGH_CONFIDENCE_MARGIN = 2
_VERY_HIGH_CONFIDENCE_HITS = 5

_CONFIDENCE_ONE_HIT = 0.5
_CONFIDENCE_TWO_HITS = 0.7
_CONFIDENCE_HIGH = 0.85
_CONFIDENCE_VERY_HIGH = 0.9
_CONFIDENCE_NO_EVIDENCE = 0.35


@dataclass(frozen=True)
class DetectionResult:
    language: str
    confidence: float


@dataclass(frozen=True)
class _Markers:
    """Marker groups with different matching semantics.

    chars: substring match anywhere (diacritics unique to a language)
    stems: token prefix match (keeps `dolaz` matching `dolazimo`)
    words: exact token match (short forms where a prefix would misfire)
    phrases: substring match for multiword cues
    """

    chars: tuple[str, ...] = ()
    stems: tuple[str, ...] = ()
    words: frozenset[str] = field(default_factory=frozenset)
    phrases: tuple[str, ...] = ()


# Order matters: on an equal number of hits the earlier language wins, which
# keeps historical behaviour for overlapping markers (`danke` de before nl,
# `vecer` hr before cs, `camera` it before ro, `tarde` es before pt).
_MARKERS: dict[str, _Markers] = {
    "sk": _Markers(
        chars=("ľ", "ô", "ŕ"),
        stems=("veľk", "izba", "záchod", "ďakuj", "špinav", "kúpeľ", "prie"),
        words=frozenset({"ste", "sme", "kde", "prosím", "prosim", "môžeme", "mozeme"}),
    ),
    "de": _Markers(
        chars=("ß",),
        stems=(
            "schön",
            "danke",
            "zimmer",
            "übernacht",
            "gäste",
            "spät",
            "ankunft",
            "können",
            "spaeter",
            "abend",
            "parkplatz",
        ),
        words=frozenset({"wir", "wann", "wie", "haben", "hallo", "bitte", "uhr", "ist", "und"}),
    ),
    "hr": _Markers(
        stems=(
            "hvala",
            "soba",
            "boravak",
            "gost",
            "žao",
            "čist",
            "dolaz",
            "doći",
            "možemo",
            "večer",
            "vecer",
            "kasnij",
            "moli",
        ),
        words=frozenset(
            {
                "gdje",
                "li",
                "imate",
                "ima",
                "koliko",
                "kada",
                "možete",
                "mozete",
                "trebamo",
                "možda",
                "pozdrav",
                "ćemo",
                "cemo",
            }
        ),
    ),
    "es": _Markers(
        stems=("gracias", "habitación", "llegada", "tarde", "noche", "podemos", "aparcamiento"),
        words=frozenset({"hola", "dónde", "donde", "cuándo", "cuando", "está"}),
        phrases=("por favor",),
    ),
    "fr": _Markers(
        stems=("merci", "chambre", "arriv", "soir", "tard", "pouvons"),
        words=frozenset(
            {"je", "nous", "vous", "bonjour", "voudrais", "une", "pour", "avec", "est", "où"}
        ),
    ),
    "it": _Markers(
        stems=("grazie", "camera", "arrivo", "sera", "tardi", "possiamo", "parcheggio"),
        words=frozenset({"ciao", "buongiorno", "dove", "quando", "abbiamo"}),
        phrases=("per favore",),
    ),
    "pl": _Markers(
        stems=("dziękuj", "dzieku", "pokój", "pokoj", "przyjazd", "późn", "pozn", "możemy", "mozemy"),
        words=frozenset({"czy", "gdzie", "kiedy", "proszę", "prosze", "mamy"}),
    ),
    "ro": _Markers(
        stems=("mulțum", "multum", "cameră", "camera", "sosire", "seară", "seara", "putem"),
        words=frozenset({"bună", "buna", "unde", "când", "cand", "avem", "vrem"}),
    ),
    "nl": _Markers(
        stems=("dank", "kamer", "aankomst", "avond", "laat", "kunnen", "parkeer"),
        words=frozenset({"hoe", "waar", "wanneer", "wij", "alstublieft", "hebben"}),
    ),
    "cs": _Markers(
        stems=("děkuj", "deku", "pokoj", "příjezd", "prijezd", "večer", "vecer", "můžeme", "muzeme"),
        words=frozenset({"kde", "kdy", "prosím", "prosim", "máme", "mame"}),
    ),
    "pt": _Markers(
        stems=("obrigad", "quarto", "chegada", "noite", "tarde", "podemos", "estacionamento"),
        words=frozenset({"olá", "ola", "onde", "quando", "temos"}),
    ),
    "hu": _Markers(
        stems=("köszön", "koszon", "szoba", "érkez", "erkez", "este", "késő", "keso"),
        words=frozenset({"hol", "mikor", "kérem", "kerem", "szia", "van"}),
    ),
    # English deliberately avoids domain nouns that look the same in other
    # languages (parking, hotel, wifi, camera, check-in) and very short words
    # that are common elsewhere (a, i, in, on, do, no, me, am).
    "en": _Markers(
        stems=("hello", "thank", "tomorrow", "tonight", "arriv", "possib"),
        words=frozenset(
            {
                "is",
                "it",
                "also",
                "to",
                "have",
                "has",
                "we",
                "you",
                "your",
                "the",
                "can",
                "could",
                "would",
                "are",
                "will",
                "and",
                "not",
                "but",
                "for",
                "with",
                "at",
                "our",
                "there",
                "here",
                "what",
                "when",
                "where",
                "how",
                "why",
                "which",
                "that",
                "this",
                "please",
                "hi",
                "hey",
                "yes",
                "see",
                "time",
                "available",
                "need",
                "want",
                "like",
                "room",
                "night",
                "nights",
                "stay",
                "day",
                "days",
                "early",
                "late",
                "later",
                "free",
                "just",
                "about",
                "from",
                "get",
                "know",
                "sorry",
                "good",
                "morning",
                "evening",
                "spot",
                "space",
            }
        ),
    ),
}

_ENGLISH = "en"


def _hits(markers: _Markers, *, lowered: str, tokens: tuple[str, ...]) -> int:
    """Number of distinct markers matched (each marker counts at most once)."""
    count = 0
    for char in markers.chars:
        if char in lowered:
            count += 1
    for stem in markers.stems:
        if any(token.startswith(stem) for token in tokens):
            count += 1
    for word in markers.words:
        if word in tokens:
            count += 1
    for phrase in markers.phrases:
        if phrase in lowered:
            count += 1
    return count


def _confidence(hits: int, margin: int) -> float:
    if hits == 1:
        return _CONFIDENCE_ONE_HIT
    if hits == 2:
        return _CONFIDENCE_TWO_HITS
    if margin < _HIGH_CONFIDENCE_MARGIN:
        return _CONFIDENCE_TWO_HITS
    if hits >= _VERY_HIGH_CONFIDENCE_HITS:
        return _CONFIDENCE_VERY_HIGH
    return _CONFIDENCE_HIGH


def detect(text: str) -> DetectionResult:
    """Best-effort language from inbound guest message text.

    Scores every language by marker hits so that isolated foreign words cannot
    decide the result. English must win strictly, because a few of its function
    words also exist in other languages.
    """
    lowered = (text or "").lower()
    stripped = lowered.strip()
    if not stripped:
        return DetectionResult(language="unknown", confidence=0.0)
    if _EMOJI_ONLY.match(stripped):
        return DetectionResult(language="unknown", confidence=0.0)

    tokens = tuple(_TOKEN_RE.findall(stripped))
    scores = {
        language: _hits(markers, lowered=stripped, tokens=tokens)
        for language, markers in _MARKERS.items()
    }

    best_language = ""
    best_hits = 0
    for language, hits in scores.items():
        if hits <= 0:
            continue
        if language == _ENGLISH:
            continue
        if hits > best_hits:
            best_language, best_hits = language, hits

    english_hits = scores.get(_ENGLISH, 0)
    if english_hits > best_hits:
        best_language, best_hits = _ENGLISH, english_hits

    if not best_language:
        return DetectionResult(language=_ENGLISH, confidence=_CONFIDENCE_NO_EVIDENCE)

    runner_up = max(
        (hits for language, hits in scores.items() if language != best_language),
        default=0,
    )
    return DetectionResult(
        language=best_language,
        confidence=_confidence(best_hits, best_hits - runner_up),
    )
