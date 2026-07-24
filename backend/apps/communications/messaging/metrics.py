"""Lightweight messaging metrics hooks (ADR 0010)."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("apps.communications.messaging.metrics")


def incr(metric: str, *, value: int = 1, **tags: Any) -> None:
    """Emit a counter-style log line; swap for StatsD/OTel later."""
    tag_str = " ".join(f"{k}={v}" for k, v in sorted(tags.items()) if v is not None)
    logger.info("messaging_metric name=%s value=%s %s", metric, value, tag_str)


def observe_ms(metric: str, *, duration_ms: int, **tags: Any) -> None:
    tag_str = " ".join(f"{k}={v}" for k, v in sorted(tags.items()) if v is not None)
    logger.info(
        "messaging_metric name=%s duration_ms=%s %s",
        metric,
        duration_ms,
        tag_str,
    )
