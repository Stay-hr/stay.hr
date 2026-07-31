"""Photo sync counters (ADR 0015 Phase B) — log-style, StatsD-ready later."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("apps.integrations.channex.photo_metrics")


def incr(metric: str, *, value: int = 1, **tags: Any) -> None:
    tag_str = " ".join(f"{k}={v}" for k, v in sorted(tags.items()) if v is not None)
    logger.info("photo_metric name=%s value=%s %s", metric, value, tag_str)
