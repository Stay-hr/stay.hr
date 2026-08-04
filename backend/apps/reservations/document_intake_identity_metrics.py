"""Lightweight identity-consistency counters for document intake."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("apps.reservations.document_intake_identity.metrics")


def incr(metric: str, *, value: int = 1, **tags: Any) -> None:
    """Emit a counter-style log line; swap for StatsD/OTel later."""
    tag_str = " ".join(f"{k}={v}" for k, v in sorted(tags.items()) if v is not None)
    logger.info("identity_metric name=%s value=%s %s", metric, value, tag_str)
