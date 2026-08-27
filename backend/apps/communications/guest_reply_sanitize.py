"""Strip message boilerplate from LLM reply bodies.

The wrapper that assembles an outbound guest message owns the greeting, the
sign-off, the property name and the stay.hr footer. An LLM asked for a body
sometimes writes a whole letter anyway, so anything the wrapper adds is
removed here to avoid duplicates.
"""

from __future__ import annotations

import re

from apps.communications.guest_compose_defaults import GREETING, SIGN_OFF

# Matched as a literal marker instead of importing FOOTER: keeps this module
# free of a guest_compose import cycle and tolerates URL variants.
_FOOTER_MARKER = "managed by stay.hr"

_GREETING_OPENERS = ("pozdrav", "dear", "hello", "hi", "hey", "good morning", "good evening")

_BLANK_RUN_RE = re.compile(r"\n{3,}")


def _greeting_prefixes() -> tuple[str, ...]:
    """Lowercase greeting starts, e.g. `Bok {name}!` -> `bok`."""
    prefixes = {
        template.split("{")[0].strip().lower()
        for template in GREETING.values()
        if template.split("{")[0].strip()
    }
    prefixes.update(_GREETING_OPENERS)
    return tuple(sorted(prefixes, key=len, reverse=True))


def _is_footer_line(line: str) -> bool:
    return _FOOTER_MARKER in line.lower()


def _is_greeting_line(line: str) -> bool:
    """A short opening line such as `Bok Antoine!`, `Hello,` or `Dear Mr Smith,`."""
    lowered = line.lower()
    if len(lowered.split()) > 4:
        return False
    return any(lowered.startswith(prefix) for prefix in _greeting_prefixes())


def _is_sign_off_line(line: str) -> bool:
    lowered = line.lower().rstrip(",")
    return any(value.lower().rstrip(",") == lowered for value in SIGN_OFF.values())


def strip_reply_boilerplate(text: str, *, property_name: str = "") -> str:
    """Remove greeting, sign-off, property name and footer from an LLM body.

    The property name line is only dropped when `property_name` is supplied and
    the line matches it exactly — without it no line can be safely identified.
    """
    lines = [line.strip() for line in (text or "").replace("\r\n", "\n").split("\n")]
    lines = [line for line in lines if not _is_footer_line(line)]

    while lines and not lines[0]:
        lines.pop(0)
    if lines and _is_greeting_line(lines[0]):
        lines.pop(0)

    property_clean = (property_name or "").strip().lower()
    changed = True
    while changed:
        changed = False
        while lines and not lines[-1]:
            lines.pop()
            changed = True
        if not lines:
            break
        if _is_sign_off_line(lines[-1]):
            lines.pop()
            changed = True
            continue
        if property_clean and lines[-1].lower() == property_clean:
            lines.pop()
            changed = True

    return _BLANK_RUN_RE.sub("\n\n", "\n".join(lines)).strip()
