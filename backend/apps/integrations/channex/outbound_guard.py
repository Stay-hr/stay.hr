"""Channex write capability guard (single-writer concurrency control).

Not a security feature: prevents parallel writers (e.g. WSL + hel1) against the
same Channex property. See ADR 0014.

Fail-closed: ``CHANNEX_OUTBOUND_ENABLED`` defaults to False. Every allow/block
decision is audited via structured ``channex_outbound_decision`` logs.
"""

from __future__ import annotations

import logging
import os
import socket
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from django.conf import settings

from apps.integrations.channex.exceptions import ChannexWriteDisabled

logger = logging.getLogger(__name__)

_force_channex_write: ContextVar[bool] = ContextVar("force_channex_write", default=False)

# Process-local diagnostic counters for /system/status (reset on restart).
_blocked_total: int = 0
_allowed_total: int = 0

CHANNEX_WRITE_DISABLED_REASON = "channex_write_disabled"


def _outbound_enabled() -> bool:
    return bool(getattr(settings, "CHANNEX_OUTBOUND_ENABLED", False))


def _outbound_maintenance() -> bool:
    return bool(getattr(settings, "CHANNEX_OUTBOUND_MAINTENANCE", False))


def _outbound_allowlist() -> list[str]:
    raw = getattr(settings, "CHANNEX_OUTBOUND_TENANT_SLUGS", None) or []
    return [str(s).strip() for s in raw if str(s).strip()]


def writer_mode() -> str:
    """Human-readable mode for startup banner / status."""
    if _force_channex_write.get():
        return "force-writer"
    if _outbound_maintenance():
        return "maintenance"
    if _outbound_enabled():
        return "writer"
    return "read-only"


def evaluate_outbound_decision(
    *,
    tenant: str | None = None,
) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` without side effects.

    Reasons: ``force``, ``enabled``, ``disabled``, ``allowlist``, ``maintenance``.
    """
    if _force_channex_write.get():
        return True, "force"
    if _outbound_maintenance():
        return False, "maintenance"
    if not _outbound_enabled():
        return False, "disabled"
    allowlist = _outbound_allowlist()
    if allowlist and tenant is not None and tenant not in allowlist:
        return False, "allowlist"
    return True, "enabled"


def can_write_to_channex(*, tenant: str | None = None) -> bool:
    """True if this process may mutate Channex (force, or enabled + allowlist)."""
    allowed, _reason = evaluate_outbound_decision(tenant=tenant)
    return allowed


def _record_decision(
    *,
    allowed: bool,
    reason: str,
    operation: str | None,
    tenant: str | None,
    caller: str | None,
) -> None:
    global _blocked_total, _allowed_total
    if allowed:
        _allowed_total += 1
    else:
        _blocked_total += 1

    mode = writer_mode() if not allowed or reason != "force" else "force-writer"
    extra: dict[str, Any] = {
        "event": "channex_outbound_decision",
        "result": "allowed" if allowed else "blocked",
        "reason": reason,
        "operation": operation or "",
        "caller": caller or "",
        "mode": mode,
        "outbound_enabled": _outbound_enabled(),
    }
    if tenant is not None:
        extra["tenant"] = tenant
    logger.info(
        "channex_outbound_decision result=%s reason=%s operation=%s caller=%s",
        extra["result"],
        reason,
        operation or "",
        caller or "",
        extra=extra,
    )

    if allowed and reason == "force":
        logger.warning(
            "CHANNEX FORCE WRITE operation=%s tenant=%s host=%s user=%s caller=%s",
            operation or "",
            tenant or "",
            socket.gethostname(),
            os.environ.get("USER") or os.environ.get("USERNAME") or "",
            caller or "",
            extra={
                "event": "channex_force_write",
                "operation": operation or "",
                "tenant": tenant or "",
                "host": socket.gethostname(),
                "reason": "force",
                "caller": caller or "",
            },
        )


def assert_can_write(
    *,
    tenant: str | None = None,
    operation: str | None = None,
    caller: str | None = None,
) -> None:
    """Sole write gate: audit decision; raise ``ChannexWriteDisabled`` if blocked."""
    allowed, reason = evaluate_outbound_decision(tenant=tenant)
    _record_decision(
        allowed=allowed,
        reason=reason,
        operation=operation,
        tenant=tenant,
        caller=caller,
    )
    if allowed:
        return
    raise ChannexWriteDisabled(
        method=(operation or "").split(" ", 1)[0] if operation else None,
        path=operation,
        reason=reason,
    )


def skip_if_channex_write_disabled(
    *,
    task: str,
    tenant: str | None = None,
) -> dict[str, Any] | None:
    """Early-skip payload for Beat/worker *write* tasks when capability is off.

    Returns ``{"skipped": True, "reason": ...}`` so periodic write work does not
    raise. Returns None when writes are allowed. Verify-only tasks must not call
    this — verify is safe on read-only hosts.
    """
    allowed, reason = evaluate_outbound_decision(tenant=tenant)
    if allowed:
        return None
    _record_decision(
        allowed=False,
        reason=reason,
        operation=task,
        tenant=tenant,
        caller="beat",
    )
    extra: dict[str, Any] = {
        "event": "channex_task_skipped",
        "reason": reason,
        "task": task,
    }
    if tenant is not None:
        extra["tenant"] = tenant
    logger.info(
        "channex task skipped: reason=%s task=%s",
        reason,
        task,
        extra=extra,
    )
    return {"skipped": True, "reason": reason}


def require_channex_write(
    *,
    method: str | None = None,
    path: str | None = None,
    tenant: str | None = None,
    reason: str = "feature_flag",  # noqa: ARG001 — kept for call-site compat
    caller: str | None = "client",
) -> None:
    """Raise ChannexWriteDisabled when write capability is off (legacy helper)."""
    operation = f"{method or ''} {path or ''}".strip() or path or method
    assert_can_write(tenant=tenant, operation=operation, caller=caller)


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
    """Legacy blocked counter + log (prefer ``assert_can_write`` for new code)."""
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
    return _blocked_total


def get_channex_outbound_allowed_total() -> int:
    return _allowed_total


def reset_channex_outbound_counters() -> None:
    """Test helper: reset in-process counters."""
    global _blocked_total, _allowed_total
    _blocked_total = 0
    _allowed_total = 0


def reset_channex_outbound_blocked_total() -> None:
    """Backward-compatible alias for tests."""
    reset_channex_outbound_counters()


def log_channex_outbound_startup_banner() -> None:
    """Log writer vs read-only mode once at process boot."""
    allowlist = _outbound_allowlist()
    enabled = _outbound_enabled()
    mode = writer_mode()
    logger.warning(
        "Channex outbound: enabled=%s allowlist=%s mode=%s",
        str(enabled).lower(),
        allowlist,
        mode,
        extra={
            "event": "channex_outbound_startup",
            "outbound_enabled": enabled,
            "allowlist": allowlist,
            "mode": mode,
            "maintenance": _outbound_maintenance(),
        },
    )


def channex_outbound_status_snapshot() -> dict[str, Any]:
    """Payload fragment for GET /system/status."""
    return {
        "write_enabled": can_write_to_channex(),
        "outbound_enabled": _outbound_enabled(),
        "maintenance": _outbound_maintenance(),
        "mode": writer_mode(),
        "allowlist": _outbound_allowlist(),
        "outbound_allowed_total": _allowed_total,
        "outbound_blocked_total": _blocked_total,
    }
