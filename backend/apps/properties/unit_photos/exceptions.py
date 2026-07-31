"""Domain errors for unit photos (ADR 0015)."""

from __future__ import annotations


class UnitPhotoError(Exception):
    """Base error for unit photo domain operations."""


class UnitPhotoValidationError(UnitPhotoError):
    """Rejected before canonical insert (invalid image)."""


class UnitPhotoStateError(UnitPhotoError):
    """Illegal state transition or invariant violation."""
