"""Operations alerts when all providers fail (ADR 0010).

Throttle/dedupe prevents alert spam when a provider outage fans out.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Sequence

from django.conf import settings

from apps.communications.messaging.models import MessageDispatch
from apps.communications.messaging.results import DeliveryResult

logger = logging.getLogger(__name__)

# In-process throttle (per worker). Email send can land later; this caps noise now.
_DEFAULT_THROTTLE_SECONDS = 300.0
_lock = threading.Lock()
_last_alert_at: dict[str, float] = {}


def operations_alert_emails() -> list[str]:
    raw = getattr(settings, "OPERATIONS_ALERT_EMAILS", "") or ""
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()]
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def alert_throttle_seconds() -> float:
    raw = getattr(settings, "MESSAGING_ALERT_THROTTLE_SECONDS", None)
    if raw is None:
        return _DEFAULT_THROTTLE_SECONDS
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return _DEFAULT_THROTTLE_SECONDS


def _throttle_key(dispatch: MessageDispatch, results: Sequence[DeliveryResult]) -> str:
    codes = ",".join(
        sorted(
            {
                f"{r.provider}:{r.error_code or r.error_category or 'UNKNOWN'}"
                for r in results
            }
        )
    )
    return f"{dispatch.definition_key}|{codes}"


def _should_emit(key: str, *, now: float | None = None) -> bool:
    """Return True if this alert key is outside the throttle window."""
    clock = time.monotonic() if now is None else now
    window = alert_throttle_seconds()
    with _lock:
        last = _last_alert_at.get(key)
        if last is not None and window > 0 and (clock - last) < window:
            return False
        _last_alert_at[key] = clock
        return True


def reset_alert_throttle_for_tests() -> None:
    with _lock:
        _last_alert_at.clear()


def alert_all_providers_failed(
    dispatch: MessageDispatch,
    results: Sequence[DeliveryResult],
) -> bool:
    """Log when every channel_policy provider failed.

    Returns True if the alert was emitted (not throttled). Email delivery can
    hook here later; throttle applies regardless of transport.
    """
    key = _throttle_key(dispatch, results)
    if not _should_emit(key):
        logger.info(
            "messaging_all_providers_failed_throttled dispatch_id=%s "
            "definition=%s throttle_key=%s",
            dispatch.pk,
            dispatch.definition_key,
            key,
        )
        return False

    summary = "; ".join(
        f"{r.provider}:{r.error_code or r.error_category}:{r.error_message[:80]}"
        for r in results
    )
    logger.error(
        "messaging_all_providers_failed dispatch_id=%s definition=%s "
        "reservation_id=%s correlation_id=%s results=%s alert_emails=%s",
        dispatch.pk,
        dispatch.definition_key,
        dispatch.reservation_id,
        dispatch.correlation_id,
        summary,
        ",".join(operations_alert_emails()) or "-",
    )
    return True
