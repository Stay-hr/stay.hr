from __future__ import annotations

import logging

from celery import shared_task

from apps.integrations.channex.booking_service import process_channex_booking_revisions_feed
from apps.integrations.channex.cancel_reconcile import (
    DEFAULT_BATCH_LIMIT,
    empty_cancel_reconcile_stats,
    reconcile_channex_cancelled_bookings,
)
from apps.integrations.channex.exceptions import ChannexApiError, ChannexBookingIngestError
from apps.integrations.models import IntegrationConfig

logger = logging.getLogger(__name__)

_SUMMARY_HEALED_IDS_CAP = 20


def _active_channex_integrations(tenant_slug: str):
    return list(
        IntegrationConfig.objects.filter(
            tenant__slug=tenant_slug,
            provider=IntegrationConfig.Provider.CHANNEX,
            is_active=True,
        ).select_related("tenant")
    )


@shared_task
def process_channex_booking_revisions_feed_periodic(
    *,
    tenant_slug: str = "uzorita",
) -> dict:
    """Process non-acknowledged Channex booking revisions (missed webhook fallback)."""
    row = (
        IntegrationConfig.objects.filter(
            tenant__slug=tenant_slug,
            provider=IntegrationConfig.Provider.CHANNEX,
            is_active=True,
        )
        .select_related("tenant")
        .first()
    )
    if row is None:
        return {"processed": 0, "error": "no_integration"}

    try:
        result = process_channex_booking_revisions_feed(row)
    except (ChannexBookingIngestError, ChannexApiError) as exc:
        logger.warning(
            "channex booking revisions feed periodic failed",
            extra={"tenant_slug": tenant_slug, "error": str(exc)},
        )
        return {"processed": 0, "error": str(exc)}

    reservations = result["ingested"]
    ack_only = int(result.get("ack_only") or 0)
    errors = int(result.get("errors") or 0)
    reservation_ids = [r.pk for r in reservations]
    if reservation_ids or ack_only or errors:
        logger.info(
            "channex booking revisions feed periodic processed",
            extra={
                "tenant_slug": tenant_slug,
                "reservation_ids": reservation_ids,
                "ack_only": ack_only,
                "errors": errors,
            },
        )
    return {
        "processed": len(reservation_ids),
        "ack_only": ack_only,
        "errors": errors,
        "reservation_ids": reservation_ids,
    }


@shared_task
def reconcile_channex_cancelled_bookings_daily(
    *,
    tenant_slug: str = "uzorita",
    dry_run: bool = False,
    limit: int = DEFAULT_BATCH_LIMIT,
) -> dict:
    """
    Daily cancel-only safety net: local expected/checked_in vs Channex booking status.
    """
    rows = _active_channex_integrations(tenant_slug)
    if not rows:
        logger.warning(
            "channex cancel reconcile skipped",
            extra={"tenant_slug": tenant_slug, "error": "no_integration"},
        )
        stats = empty_cancel_reconcile_stats()
        stats["error"] = "no_integration"
        return stats
    if len(rows) > 1:
        logger.error(
            "channex cancel reconcile skipped",
            extra={
                "tenant_slug": tenant_slug,
                "error": "ambiguous_integration",
                "integration_count": len(rows),
            },
        )
        stats = empty_cancel_reconcile_stats()
        stats["error"] = "ambiguous_integration"
        stats["integration_count"] = len(rows)
        return stats

    row = rows[0]
    try:
        stats = reconcile_channex_cancelled_bookings(
            row,
            dry_run=dry_run,
            limit=limit,
        )
    except (ChannexBookingIngestError, ChannexApiError) as exc:
        logger.warning(
            "channex cancel reconcile failed",
            extra={"tenant_slug": tenant_slug, "error": str(exc)},
        )
        stats = empty_cancel_reconcile_stats()
        stats["error"] = str(exc)
        return stats

    healed_ids = list(stats.get("healed_ids") or [])
    logger.info(
        "channex cancel reconcile daily processed",
        extra={
            "tenant_slug": tenant_slug,
            "dry_run": dry_run,
            "candidates": stats.get("candidates"),
            "api_checked": stats.get("api_checked"),
            "healed": stats.get("healed"),
            "would_heal": stats.get("would_heal"),
            "noop_active": stats.get("noop_active"),
            "remote_not_found": stats.get("remote_not_found"),
            "invalid_remote_payload": stats.get("invalid_remote_payload"),
            "errors": stats.get("errors"),
            "healed_ids_sample": healed_ids[:_SUMMARY_HEALED_IDS_CAP],
            "healed_ids_total": len(healed_ids),
        },
    )
    return stats
