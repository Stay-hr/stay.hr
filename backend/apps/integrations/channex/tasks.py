from __future__ import annotations

import logging

from celery import shared_task

from apps.integrations.channex.ari_service import flush_channex_ari_outbox, get_active_channex_integration
from apps.integrations.channex.availability_verify_service import DEFAULT_VERIFY_DAYS
from apps.integrations.channex.exceptions import (
    ChannexBookingIngestError,
    PhotoSyncRetryableError,
)
from apps.integrations.channex.outbound_guard import skip_if_channex_write_disabled
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

# Ops default for live Celery runs until more Channex clients are online.
# Beat schedule also passes kwargs tenant_id=2 — keep in sync.
OPS_DEFAULT_TENANT_ID = 2


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def sync_reservation_channex_availability_task(
    self,
    reservation_id: int,
    action: str = "sync",
) -> dict:
    from apps.integrations.channel_manager.tasks import sync_reservation_outbound_task

    return sync_reservation_outbound_task(reservation_id, action)


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def flush_channex_ari_outbox_task(self, tenant_slug: str = "demo") -> list[dict]:
    skipped = skip_if_channex_write_disabled(
        task="flush_channex_ari_outbox_task",
        tenant=tenant_slug,
    )
    if skipped is not None:
        return []
    try:
        integration = get_active_channex_integration(tenant_slug)
        results = flush_channex_ari_outbox(integration)
        logger.info(
            "channex ari outbox flushed",
            extra={"tenant_slug": tenant_slug, "results": results},
        )
        return results
    except ChannexBookingIngestError as exc:
        logger.warning("channex ari flush skipped: %s", exc)
        return []
    except Exception as exc:
        logger.exception("channex ari flush failed")
        raise self.retry(exc=exc) from exc


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def flush_photo_outbox_task(
    self,
    tenant_slug: str = "uzorita",
    limit: int = 100,
) -> dict:
    """Flush pending PhotoOutbox rows to Channex (ADR 0015 Phase B)."""
    from apps.integrations.channex.photo_outbox import flush_photo_outbox

    skipped = skip_if_channex_write_disabled(
        task="flush_photo_outbox_task",
        tenant=tenant_slug,
    )
    if skipped is not None:
        return skipped
    try:
        summary = flush_photo_outbox(tenant_slug=tenant_slug, limit=limit)
        logger.info(
            "photo outbox flushed",
            extra={"tenant_slug": tenant_slug, "summary": summary},
        )
        return summary
    except PhotoSyncRetryableError as exc:
        logger.warning("photo outbox flush retryable: %s", exc)
        raise self.retry(exc=exc) from exc
    except Exception as exc:
        logger.exception("photo outbox flush failed")
        raise self.retry(exc=exc) from exc


@shared_task
def verify_channex_availability_daily(
    tenant_id: int = OPS_DEFAULT_TENANT_ID,
    days: int = DEFAULT_VERIFY_DAYS,
) -> dict:
    """Daily GET /availability verify + notify (no ARI repair).

    Safe on read-only hosts. Repair is CLI-only on an authorized writer
    (incident 2026-08-01 / ADR 0014).
    """
    from apps.integrations.channex.availability_verify_service import verify_availability

    tenant = Tenant.objects.filter(pk=tenant_id).first()
    if tenant is None:
        return {"skipped": True, "reason": "tenant_not_found", "tenant_id": tenant_id}

    slug = (tenant.slug or "").strip()
    if not slug:
        return {"skipped": True, "reason": "tenant_slug_missing", "tenant_id": tenant_id}

    result = verify_availability(
        tenant_slug=slug,
        days=days,
        notify=True,
    )
    return {"tenant_id": tenant_id, **result}
