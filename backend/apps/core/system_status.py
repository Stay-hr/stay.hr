"""Shared system status payload for API and daily ops collectors.

SSE block (``get_sse_connection_stats``) is permanent lifecycle
instrumentation (ADR 0005) — keep through Redis EventBus and Uvicorn SSE.

``components`` derives healthy|warning|critical + reason from existing
fields (no new counters) for automated health checks.
"""

from __future__ import annotations

import os
import time

from django.db import connection

from apps.core.component_health import build_components_status
from apps.core.runtime_stats import (
    SYSTEM_STATUS_SCHEMA_VERSION,
    build_info_from_env,
    gunicorn_config_from_env,
    worker_uptime_seconds,
)
from apps.reservations.reservation_version_event_bus import get_event_bus_status
from apps.reservations.reservation_version_events import get_sse_connection_stats


def probe_database_status() -> dict:
    """Thin connectivity snapshot (not a counter) for component derivation."""
    try:
        start = time.perf_counter()
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        latency_ms = round((time.perf_counter() - start) * 1000.0, 2)
        return {"ok": True, "latency_ms": latency_ms}
    except Exception:
        return {"ok": False, "latency_ms": None}


def _messaging_status_block() -> dict:
    """ADR 0010 messaging inventory + outbox depth (fail soft for status)."""
    try:
        from apps.communications.messaging.health import messaging_health_snapshot

        return messaging_health_snapshot(include_queue=True)
    except Exception as exc:  # noqa: BLE001 — status must stay available
        return {
            "ok": False,
            "error": str(exc)[:200],
        }


def _conversation_status_block() -> dict:
    """ADR 0019 ingest lag per channel — not ADR 0010 engine health."""
    try:
        from apps.communications.conversation_ingest_status import (
            conversation_ingest_snapshot,
        )

        return conversation_ingest_snapshot()
    except Exception as exc:  # noqa: BLE001 — status must stay available
        return {
            "ok": False,
            "error": str(exc)[:200],
        }


def _channex_status_block() -> dict:
    """Outbound guard + verify/repair process counters (ADR 0014)."""
    try:
        from apps.integrations.channex.availability_verify_service import (
            get_channex_repair_skipped_threshold_total,
            get_channex_repair_success_total,
            get_channex_verify_mismatches_total,
        )
        from apps.integrations.channex.outbound_guard import (
            channex_outbound_status_snapshot,
        )

        snap = channex_outbound_status_snapshot()
        return {
            **snap,
            "verify_mismatches_total": get_channex_verify_mismatches_total(),
            "repair_skipped_threshold_total": get_channex_repair_skipped_threshold_total(),
            "repair_success_total": get_channex_repair_success_total(),
        }
    except Exception as exc:  # noqa: BLE001 — status must stay available
        return {"ok": False, "error": str(exc)[:200]}


def build_system_status_payload(*, reporter_process: str | None = None) -> dict:
    gunicorn_config = gunicorn_config_from_env()
    sse = get_sse_connection_stats()
    event_bus = get_event_bus_status()
    database = probe_database_status()
    payload: dict = {
        "schema_version": SYSTEM_STATUS_SCHEMA_VERSION,
        "metrics_scope": "worker_process",
        "build": build_info_from_env(),
        "gunicorn": {
            **gunicorn_config,
            "pid": os.getpid(),
            "uptime_seconds": worker_uptime_seconds(),
            "timeout": int(os.environ.get("GUNICORN_TIMEOUT", "3600")),
        },
        "sse": sse,
        "event_bus": event_bus,
        "database": database,
        "messaging": _messaging_status_block(),
        "conversation": _conversation_status_block(),
        "channex": _channex_status_block(),
        "components": build_components_status(
            event_bus=event_bus,
            sse=sse,
            database=database,
        ),
    }
    if reporter_process:
        payload["reporter_process"] = reporter_process
    return payload
