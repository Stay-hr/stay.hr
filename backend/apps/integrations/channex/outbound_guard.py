"""Channex write capability guard (single-writer concurrency control).

Not a security feature: prevents parallel writers (e.g. WSL + hel1) against the
same Channex property. See ADR 0014 (phase 5).
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from django.conf import settings

from apps.integrations.channex.exceptions import ChannexWriteDisabled

logger = logging.getLogger(__name__)

_force_channex_write: ContextVar[bool] = ContextVar("force_channex_write", default=False)

# Process-local diagnostic counter for /system/status and runtime inspection.
# Resets on process restart — not suitable for long-term monitoring.
_blocked_total: int = 0


CHANNEX_WRITE_DISABLED_REASON = "channex_write_disabled"


def can_write_to_channex() -> bool:
    """True if settings flag is on OR an active --force-channex-outbound context."""
    if _force_channex_write.get():
        return True
    return bool(getattr(settings, "CHANNEX_OUTBOUND_ENABLED", True))


def skip_if_channex_write_disabled(
    *,
    task: str,
    tenant: str | None = None,
) -> dict[str, Any] | None:
    """Early-skip payload for Beat/worker tasks when write capability is off.

    Returns ``{"skipped": True, "reason": "channex_write_disabled"}`` (not an
    error) so periodic work does not ingest/ACK/repair/flush. Returns None when
    writes are allowed.
    """
    if can_write_to_channex():
        return None
    extra: dict[str, Any] = {
        "event": "channex_task_skipped",
        "reason": CHANNEX_WRITE_DISABLED_REASON,
        "task": task,
    }
    if tenant is not None:
        extra["tenant"] = tenant
    logger.info(
        "channex task skipped: write disabled task=%s",
        task,
        extra=extra,
    )
    return {"skipped": True, "reason": CHANNEX_WRITE_DISABLED_REASON}


def require_channex_write(
    *,
    method: str | None = None,
    path: str | None = None,
    tenant: str | None = None,
    reason: str = "feature_flag",
) -> None:
    """Raise ChannexWriteDisabled when write capability is off."""
    if can_write_to_channex():
        return
    record_channex_write_blocked(
        method=method or "",
        path=path or "",
        tenant=tenant,
        reason=reason,
    )
    raise ChannexWriteDisabled(
        method=method,
        path=path,
        reason=reason,
    )


@contextmanager
def force_channex_write() -> Iterator[None]:
    """Task/thread-local override for CLI ``--force-channex-outbound`` only."""
    token = _force_channex_write.set(True)
    try:
        yield
    finally:
        _force_channex_write.reset(token)


def record_channex_write_blocked(
    *,
    method: str,
    path: str,
    tenant: str | None = None,
    reason: str = "feature_flag",
) -> None:
    """Structured log + in-process counter (metrics/status wired in phase 4)."""
    global _blocked_total
    _blocked_total += 1
    extra: dict[str, Any] = {
        "event": "channex_outbound_blocked",
        "method": method,
        "endpoint": path,
        "reason": reason,
    }
    if tenant is not None:
        extra["tenant"] = tenant
    logger.info(
        "channex_outbound_blocked method=%s endpoint=%s reason=%s",
        method,
        path,
        reason,
        extra=extra,
    )


def get_channex_outbound_blocked_total() -> int:
    """Process-local blocked write count for runtime inspection (resets on restart)."""
    return _blocked_total


def reset_channex_outbound_blocked_total() -> None:
    """Test helper: reset the in-process counter."""
    global _blocked_total
    _blocked_total = 0
