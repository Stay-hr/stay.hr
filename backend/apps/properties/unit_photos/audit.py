"""Structured audit helpers for unit photos (ADR 0015)."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("apps.properties.unit_photos.audit")


def audit_unit_photo(
    action: str,
    *,
    unit_id: int,
    photo_id: int | None = None,
    checksum: str | None = None,
    actor: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "event": "unit_photo_audit",
        "action": action,
        "unit_id": unit_id,
    }
    if photo_id is not None:
        payload["photo_id"] = photo_id
    if checksum is not None:
        payload["checksum"] = checksum
    if actor is not None:
        payload["actor"] = actor
    if extra:
        payload.update(extra)
    logger.info(
        "unit_photo_audit action=%s unit_id=%s photo_id=%s",
        action,
        unit_id,
        photo_id,
        extra=payload,
    )
