"""Locale helpers for messaging render (v1 thin)."""

from __future__ import annotations

from typing import Any


def resolve_language(*candidates: Any, default: str = "en") -> str:
    """First non-empty language code, else default."""
    for value in candidates:
        text = str(value or "").strip().lower()
        if text:
            return text[:8]
    return default
