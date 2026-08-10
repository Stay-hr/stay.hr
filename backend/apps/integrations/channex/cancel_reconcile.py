"""
Narrow Channex cancel-status safety net.

Compares local open Channex reservations to GET /bookings/:id and heals only
when the remote status is cancelled. Never a full booking sync.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from django.utils import timezone

from apps.integrations.channex.booking_service import parse_channex_booking_id
from apps.integrations.channex.cancel_service import heal_channex_cancel_locally
from apps.integrations.channex.client import ChannexClient
from apps.integrations.channex.config import ChannexRuntimeConfig
from apps.integrations.channex.exceptions import ChannexApiError
from apps.integrations.models import IntegrationConfig
from apps.reservations.channel_sync import IMPORT_SOURCE_CHANNEX
from apps.reservations.models import Reservation

logger = logging.getLogger(__name__)

ZAGREB = ZoneInfo("Europe/Zagreb")
DEFAULT_BATCH_LIMIT = 500
LOOKAHEAD_DAYS = 60
# Reserved for a future per-request delay; unused in v1.
REQUEST_DELAY_SECONDS = 0

def empty_cancel_reconcile_stats() -> dict[str, Any]:
    return {
        "candidates": 0,
        "api_checked": 0,
        "healed": 0,
        "would_heal": 0,
        "noop_active": 0,
        "remote_not_found": 0,
        "invalid_remote_payload": 0,
        "skipped_unparseable_id": 0,
        "skipped_no_show": 0,
        "skipped_ineligible": 0,
        "already_canceled": 0,
        "errors": 0,
        "healed_ids": [],
    }


def _normalize_remote_status(payload: dict[str, Any]) -> str | None:
    """
    Return strip().lower() status, or None when payload/status is invalid.

    Missing attributes, missing status, or blank status → None (fail-closed).
    """
    if not isinstance(payload, dict):
        return None
    attrs = payload.get("attributes")
    if not isinstance(attrs, dict):
        return None
    if "status" not in attrs:
        return None
    raw = attrs.get("status")
    if raw is None:
        return None
    normalized = str(raw).strip().lower()
    if not normalized:
        return None
    return normalized


def candidate_queryset(
    tenant_id: int,
    *,
    today=None,
    limit: int = DEFAULT_BATCH_LIMIT,
):
    if today is None:
        today = timezone.now().astimezone(ZAGREB).date()
    yesterday = today - timedelta(days=1)
    lookahead_end = today + timedelta(days=LOOKAHEAD_DAYS)
    return (
        Reservation.objects.filter(
            tenant_id=tenant_id,
            import_source=IMPORT_SOURCE_CHANNEX,
            status__in=[
                Reservation.Status.EXPECTED,
                Reservation.Status.CHECKED_IN,
            ],
            external_id__gt="",
            check_out__gte=yesterday,
            check_in__lte=lookahead_end,
        )
        .order_by("check_out", "id")[:limit]
    )


def reconcile_channex_cancelled_bookings(
    integration_row: IntegrationConfig,
    *,
    client: ChannexClient | None = None,
    dry_run: bool = False,
    limit: int = DEFAULT_BATCH_LIMIT,
) -> dict[str, Any]:
    """
    Heal local open reservations that are cancelled in Channex.

    ``get_booking`` is the only remote call. Dry-run never calls heal and has
    no DB/outbound side effects.
    """
    stats = empty_cancel_reconcile_stats()
    config = ChannexRuntimeConfig.from_integration_dict(integration_row.get_config_dict())
    owns_client = client is None
    if owns_client:
        client = ChannexClient(config)

    try:
        candidates = list(candidate_queryset(integration_row.tenant_id, limit=limit))
        stats["candidates"] = len(candidates)

        for reservation in candidates:
            booking_id = parse_channex_booking_id(reservation.external_id)
            if not booking_id:
                stats["skipped_unparseable_id"] += 1
                continue

            stats["api_checked"] += 1
            try:
                payload = client.get_booking(booking_id)
            except ChannexApiError as exc:
                if exc.status_code == 404:
                    stats["remote_not_found"] += 1
                    logger.info(
                        "channex cancel reconcile remote not found",
                        extra={
                            "reservation_id": reservation.pk,
                            "booking_id": booking_id,
                            "tenant_id": integration_row.tenant_id,
                        },
                    )
                    continue
                stats["errors"] += 1
                logger.exception(
                    "channex cancel reconcile API error",
                    extra={
                        "reservation_id": reservation.pk,
                        "booking_id": booking_id,
                        "tenant_id": integration_row.tenant_id,
                    },
                )
                continue
            except Exception:
                stats["errors"] += 1
                logger.exception(
                    "channex cancel reconcile unexpected error",
                    extra={
                        "reservation_id": reservation.pk,
                        "booking_id": booking_id,
                        "tenant_id": integration_row.tenant_id,
                    },
                )
                continue

            remote_status = _normalize_remote_status(payload)
            if remote_status is None:
                stats["invalid_remote_payload"] += 1
                continue
            if remote_status != "cancelled":
                stats["noop_active"] += 1
                continue

            if dry_run:
                stats["would_heal"] += 1
                continue

            result = heal_channex_cancel_locally(reservation.pk)
            if result == "healed":
                stats["healed"] += 1
                stats["healed_ids"].append(reservation.pk)
            elif result == "already_canceled":
                stats["already_canceled"] += 1
            elif result == "skipped_no_show":
                stats["skipped_no_show"] += 1
            else:
                stats["skipped_ineligible"] += 1
    finally:
        if owns_client and client is not None:
            client.close()

    logger.info(
        "channex cancel status reconcile summary",
        extra={
            "tenant_id": integration_row.tenant_id,
            "dry_run": dry_run,
            "candidates": stats["candidates"],
            "api_checked": stats["api_checked"],
            "healed": stats["healed"],
            "would_heal": stats["would_heal"],
            "noop_active": stats["noop_active"],
            "remote_not_found": stats["remote_not_found"],
            "invalid_remote_payload": stats["invalid_remote_payload"],
            "skipped_unparseable_id": stats["skipped_unparseable_id"],
            "skipped_no_show": stats["skipped_no_show"],
            "skipped_ineligible": stats["skipped_ineligible"],
            "already_canceled": stats["already_canceled"],
            "errors": stats["errors"],
            "healed_ids_sample": stats["healed_ids"][:20],
        },
    )
    return stats
