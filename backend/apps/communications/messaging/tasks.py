"""Celery entry for Messaging Orchestration Engine (ADR 0010 Phase 6–7).

Gated by ``MESSAGE_ORCHESTRATION_*`` flags:

- disabled → no-op
- shadow → materialize planned rows only (no provider send)
- live → materialize + ``process_due_dispatches`` for allowlisted scopes

Phase 7 live cutover: ``suppress_legacy_automated_outbound`` gates
``GuestReminderService`` (CHECKIN_* / D0) and legacy WhatsApp welcome /
intro paths for allowlisted scope in live mode.
"""

from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="communications.run_message_orchestration")
def run_message_orchestration(claim_limit: int = 50) -> dict:
    from apps.communications.messaging.bootstrap import bootstrap_messaging_engine
    from apps.communications.messaging.dispatcher import process_due_dispatches
    from apps.communications.messaging.flags import (
        orchestration_runtime,
        resolve_allowlisted_scopes,
    )
    from apps.communications.messaging.scheduler import run_scheduler_cycle

    runtime = orchestration_runtime()
    if not runtime.enabled:
        logger.info(
            "message_orchestration_skipped reason=%s",
            runtime.block_reason or "orchestration_disabled",
        )
        return {
            "enabled": False,
            "shadow": runtime.shadow,
            "mode": runtime.mode,
            "reason": runtime.block_reason or "orchestration_disabled",
            "scopes": 0,
            "materialized": 0,
            "dispatched": 0,
            "delivered": 0,
            "failed": 0,
            "skipped": 0,
        }

    if runtime.block_reason == "allowlist_empty":
        logger.warning("message_orchestration_skipped reason=allowlist_empty")
        return {
            "enabled": True,
            "shadow": runtime.shadow,
            "mode": "disabled",
            "reason": "allowlist_empty",
            "scopes": 0,
            "materialized": 0,
            "dispatched": 0,
            "delivered": 0,
            "failed": 0,
            "skipped": 0,
        }

    bootstrap_messaging_engine(validate=True)
    scopes = resolve_allowlisted_scopes(runtime=runtime)
    if not scopes:
        logger.warning(
            "message_orchestration_skipped reason=allowlist_unresolved "
            "tenants=%s properties=%s",
            sorted(runtime.tenant_slugs),
            sorted(runtime.property_tokens),
        )
        return {
            "enabled": True,
            "shadow": runtime.shadow,
            "mode": runtime.mode,
            "reason": "allowlist_unresolved",
            "scopes": 0,
            "materialized": 0,
            "dispatched": 0,
            "delivered": 0,
            "failed": 0,
            "skipped": 0,
        }

    # Shadow: plan only. Live: claim+send via process_due (do not claim in cycle).
    claim_in_cycle = False
    totals = {
        "enabled": True,
        "shadow": runtime.shadow,
        "mode": runtime.mode,
        "reason": None,
        "scopes": len(scopes),
        "expired": 0,
        "cancelled": 0,
        "materialized": 0,
        "dispatched": 0,
        "delivered": 0,
        "failed": 0,
        "skipped": 0,
    }

    for scope in scopes:
        summary = run_scheduler_cycle(
            tenant_id=scope.tenant_id,
            property_id=scope.property_id,
            claim_limit=claim_limit,
            claim=claim_in_cycle,
        )
        totals["expired"] += summary["expired"]
        totals["cancelled"] += summary["cancelled"]
        totals["materialized"] += summary["materialized"]

        if runtime.shadow:
            logger.info(
                "message_orchestration_shadow_scope tenant=%s property_id=%s "
                "materialized=%s",
                scope.tenant_slug,
                scope.property_id,
                summary["materialized"],
            )
            continue

        outcomes = process_due_dispatches(
            limit=claim_limit,
            tenant_id=scope.tenant_id,
            property_id=scope.property_id,
        )
        totals["dispatched"] += len(outcomes)
        for outcome in outcomes:
            if outcome.status == "delivered":
                totals["delivered"] += 1
            elif outcome.status == "failed":
                totals["failed"] += 1
            elif outcome.status == "skipped":
                totals["skipped"] += 1

    logger.info(
        "message_orchestration_done mode=%s scopes=%s materialized=%s "
        "dispatched=%s delivered=%s failed=%s skipped=%s",
        totals["mode"],
        totals["scopes"],
        totals["materialized"],
        totals["dispatched"],
        totals["delivered"],
        totals["failed"],
        totals["skipped"],
    )
    return totals
