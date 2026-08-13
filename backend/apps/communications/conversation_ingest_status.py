"""ADR 0019 Phase B — conversation ingest timestamps.

Cluster-shared stamps for last webhook / last poll / ingest lag per channel.
This is not ADR 0010 engine health: do not put these fields on ``messaging``.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone as dt_timezone
from typing import Any, Literal

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

CONVERSATION_INGEST_CHANNELS = ("channex", "whatsapp", "email")
ConversationIngestChannel = Literal["channex", "whatsapp", "email"]
ConversationIngestKind = Literal["webhook", "poll"]

_REDIS_KEY_PREFIX = "stay:conversation_ingest:v1"
_REDIS_SOCKET_TIMEOUT = 0.5

_local: dict[str, str] = {}
_redis_client: Any = None
_redis_lock = threading.Lock()


def reset_conversation_ingest_for_tests() -> None:
    """Clear process-local stamps (tests). Redis is skipped while Celery is eager."""
    _local.clear()


def mark_conversation_ingest(
    channel: ConversationIngestChannel,
    kind: ConversationIngestKind,
    *,
    at: datetime | None = None,
) -> None:
    """Record that a webhook or poll path ran for ``channel``."""
    _validate(channel, kind)
    ts = at or timezone.now()
    if timezone.is_naive(ts):
        ts = timezone.make_aware(ts, timezone=dt_timezone.utc)
    iso = ts.isoformat()
    key = _stamp_key(channel, kind)
    _local[key] = iso
    _redis_set(key, iso)


def conversation_ingest_snapshot(*, now: datetime | None = None) -> dict[str, Any]:
    """Payload for ``GET …/system/status/`` ``conversation`` (cluster scope)."""
    clock = now or timezone.now()
    if timezone.is_naive(clock):
        clock = timezone.make_aware(clock, timezone=dt_timezone.utc)

    channels: dict[str, dict[str, Any]] = {}
    for channel in CONVERSATION_INGEST_CHANNELS:
        last_webhook_at = _read_stamp(channel, "webhook")
        last_poll_at = _read_stamp(channel, "poll")
        freshness = _latest(last_webhook_at, last_poll_at)
        lag: int | None = None
        if freshness is not None:
            lag = max(0, int((clock - freshness).total_seconds()))
        channels[channel] = {
            "last_webhook_at": last_webhook_at.isoformat() if last_webhook_at else None,
            "last_poll_at": last_poll_at.isoformat() if last_poll_at else None,
            "ingest_lag_seconds": lag,
        }

    return {
        "metrics_scope": "cluster",
        "channels": channels,
        "generated_at": clock.isoformat(),
    }


def _validate(channel: str, kind: str) -> None:
    if channel not in CONVERSATION_INGEST_CHANNELS:
        raise ValueError(f"Unknown conversation ingest channel: {channel}")
    if kind not in ("webhook", "poll"):
        raise ValueError(f"Unknown conversation ingest kind: {kind}")


def _stamp_key(channel: str, kind: str) -> str:
    return f"{_REDIS_KEY_PREFIX}:{channel}:{kind}"


def _redis_enabled() -> bool:
    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        return False
    url = str(getattr(settings, "REDIS_URL", "") or "")
    return url.startswith("redis://") or url.startswith("rediss://")


def _get_redis():
    global _redis_client
    if not _redis_enabled():
        return None
    with _redis_lock:
        if _redis_client is None:
            import redis

            _redis_client = redis.from_url(
                settings.REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=_REDIS_SOCKET_TIMEOUT,
                socket_timeout=_REDIS_SOCKET_TIMEOUT,
            )
        return _redis_client


def _redis_set(key: str, value: str) -> None:
    global _redis_client
    try:
        client = _get_redis()
        if client is None:
            return
        client.set(key, value)
    except Exception:
        logger.debug("conversation ingest redis set failed", exc_info=True)
        with _redis_lock:
            _redis_client = None


def _redis_get(key: str) -> str | None:
    global _redis_client
    try:
        client = _get_redis()
        if client is None:
            return None
        value = client.get(key)
        if value is None:
            return None
        return str(value)
    except Exception:
        logger.debug("conversation ingest redis get failed", exc_info=True)
        with _redis_lock:
            _redis_client = None
        return None


def _read_stamp(channel: str, kind: ConversationIngestKind) -> datetime | None:
    key = _stamp_key(channel, kind)
    raw = _redis_get(key) or _local.get(key)
    return _parse_iso(raw)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if timezone.is_naive(parsed):
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


def _latest(*values: datetime | None) -> datetime | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return max(present)
