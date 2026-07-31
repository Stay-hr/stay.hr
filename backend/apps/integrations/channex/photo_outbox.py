"""Flush PhotoOutbox through ChannexPhotoProvider (ADR 0015 Phase B).

Per-unit ``select_for_update`` lock serializes gallery ops for the same Unit.
Order within a unit: DELETE → UPLOAD → coalesced REORDER/SET_PRIMARY.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.integrations.channex.ari_service import get_active_channex_integration
from apps.integrations.channex.client import ChannexClient
from apps.integrations.channex.config import ChannexRuntimeConfig
from apps.integrations.channex.exceptions import (
    ChannexWriteDisabled,
    PhotoSyncPermanentError,
    PhotoSyncRetryableError,
)
from apps.integrations.channex import photo_metrics
from apps.integrations.channex.photo_provider import ChannexPhotoProvider
from apps.integrations.channex.outbound_guard import skip_if_channex_write_disabled
from apps.properties.models import Unit
from apps.properties.unit_photos.audit import audit_unit_photo
from apps.properties.unit_photos.models import PhotoOutbox, UnitPhoto
from apps.properties.unit_photos.storage import MediaStorage, default_media_storage

logger = logging.getLogger(__name__)


def _mark_failed(entry: PhotoOutbox, message: str) -> None:
    entry.status = PhotoOutbox.Status.FAILED
    entry.error_message = (message or "")[:2000]
    entry.save(update_fields=["status", "error_message", "updated_at"])
    photo = entry.unit_photo
    if entry.kind == PhotoOutbox.Kind.UPLOAD and photo.status not in (
        UnitPhoto.Status.DELETED,
        UnitPhoto.Status.DELETE_PENDING,
    ):
        photo.status = UnitPhoto.Status.FAILED
        photo.save(update_fields=["status", "updated_at"])


def flush_photo_outbox(
    *,
    tenant_slug: str,
    limit: int = 100,
    client: ChannexClient | None = None,
    storage: MediaStorage | None = None,
    provider: ChannexPhotoProvider | None = None,
) -> dict[str, Any]:
    """Drain pending photo outbox for a tenant. Raises PhotoSyncRetryableError if any retryable fail."""
    skipped = skip_if_channex_write_disabled(
        task="flush_photo_outbox",
        tenant=tenant_slug,
    )
    if skipped is not None:
        return skipped

    integration = get_active_channex_integration(tenant_slug)
    config = ChannexRuntimeConfig.from_integration_dict(integration.get_config_dict())
    tenant = integration.tenant

    pending_count = PhotoOutbox.objects.filter(
        tenant=tenant,
        status=PhotoOutbox.Status.PENDING,
    ).count()
    photo_metrics.incr(
        "photo_outbox_pending",
        value=pending_count,
        tenant=tenant_slug,
    )

    owns_client = client is None and provider is None
    if provider is None:
        if client is None:
            client = ChannexClient(config)
        provider = ChannexPhotoProvider(
            client=client,
            config=config,
            storage=storage or default_media_storage(),
        )

    results: list[dict[str, Any]] = []
    retryable_errors: list[str] = []

    try:
        pending = list(
            PhotoOutbox.objects.filter(
                tenant=tenant,
                status=PhotoOutbox.Status.PENDING,
            )
            .select_related("unit_photo", "unit_photo__unit")
            .order_by("id")[:limit]
        )
        by_unit: dict[int, list[PhotoOutbox]] = defaultdict(list)
        for entry in pending:
            by_unit[entry.unit_photo.unit_id].append(entry)

        for unit_id, entries in by_unit.items():
            with transaction.atomic():
                Unit.objects.select_for_update().filter(pk=unit_id).first()
                # Re-fetch pending under lock (skip rows claimed/sent by another flusher)
                locked_ids = [e.pk for e in entries]
                locked_entries = list(
                    PhotoOutbox.objects.filter(
                        pk__in=locked_ids,
                        status=PhotoOutbox.Status.PENDING,
                    )
                    .select_related("unit_photo", "unit_photo__unit")
                    .order_by("id")
                )
                deletes = [
                    e for e in locked_entries if e.kind == PhotoOutbox.Kind.DELETE
                ]
                uploads = [
                    e for e in locked_entries if e.kind == PhotoOutbox.Kind.UPLOAD
                ]
                positions = [
                    e
                    for e in locked_entries
                    if e.kind
                    in (PhotoOutbox.Kind.REORDER, PhotoOutbox.Kind.SET_PRIMARY)
                ]

                for entry in deletes + uploads:
                    try:
                        provider.apply(entry)
                        results.append(
                            {
                                "id": entry.pk,
                                "kind": entry.kind,
                                "status": "sent",
                                "unit_id": unit_id,
                            }
                        )
                        audit_unit_photo(
                            "outbox_flushed",
                            unit_id=unit_id,
                            photo_id=entry.unit_photo_id,
                            extra={"kind": entry.kind},
                        )
                    except PhotoSyncPermanentError as exc:
                        logger.error(
                            "photo outbox permanent fail id=%s kind=%s: %s",
                            entry.pk,
                            entry.kind,
                            exc,
                            extra={
                                "event": "photo_outbox_permanent_fail",
                                "outbox_id": entry.pk,
                                "kind": entry.kind,
                                "unit_id": unit_id,
                            },
                        )
                        if "401" in str(exc) or "403" in str(exc):
                            logger.error(
                                "photo outbox auth failure — alert ops outbox_id=%s",
                                entry.pk,
                                extra={
                                    "event": "photo_outbox_auth_alert",
                                    "outbox_id": entry.pk,
                                },
                            )
                        _mark_failed(entry, str(exc))
                        photo_metrics.incr(
                            "photo_upload_failed_total"
                            if entry.kind == PhotoOutbox.Kind.UPLOAD
                            else "photo_delete_total",
                            photo_id=entry.unit_photo_id,
                            outcome="permanent_fail",
                        )
                        results.append(
                            {
                                "id": entry.pk,
                                "kind": entry.kind,
                                "status": "failed",
                                "error": str(exc),
                            }
                        )
                    except (PhotoSyncRetryableError, ChannexWriteDisabled) as exc:
                        logger.warning(
                            "photo outbox retryable fail id=%s: %s",
                            entry.pk,
                            exc,
                        )
                        retryable_errors.append(str(exc))
                        results.append(
                            {
                                "id": entry.pk,
                                "kind": entry.kind,
                                "status": "retry",
                                "error": str(exc),
                            }
                        )
                    except Exception as exc:
                        logger.exception("photo outbox unexpected fail id=%s", entry.pk)
                        retryable_errors.append(str(exc))
                        results.append(
                            {
                                "id": entry.pk,
                                "kind": entry.kind,
                                "status": "retry",
                                "error": str(exc),
                            }
                        )

                if positions:
                    try:
                        provider.apply_positions_batch(
                            unit_id=unit_id, entries=positions
                        )
                        for entry in positions:
                            results.append(
                                {
                                    "id": entry.pk,
                                    "kind": entry.kind,
                                    "status": "sent",
                                    "unit_id": unit_id,
                                }
                            )
                    except PhotoSyncPermanentError as exc:
                        logger.error(
                            "photo reorder permanent fail unit=%s: %s",
                            unit_id,
                            exc,
                        )
                        for entry in positions:
                            _mark_failed(entry, str(exc))
                            results.append(
                                {
                                    "id": entry.pk,
                                    "kind": entry.kind,
                                    "status": "failed",
                                    "error": str(exc),
                                }
                            )
                    except Exception as exc:
                        logger.warning(
                            "photo reorder retryable fail unit=%s: %s", unit_id, exc
                        )
                        retryable_errors.append(str(exc))
                        for entry in positions:
                            results.append(
                                {
                                    "id": entry.pk,
                                    "kind": entry.kind,
                                    "status": "retry",
                                    "error": str(exc),
                                }
                            )
    finally:
        if owns_client and client is not None:
            client.close()

    summary = {
        "tenant": tenant_slug,
        "processed": len(results),
        "sent": sum(1 for r in results if r.get("status") == "sent"),
        "failed": sum(1 for r in results if r.get("status") == "failed"),
        "retry": sum(1 for r in results if r.get("status") == "retry"),
        "results": results,
        "flushed_at": timezone.now().isoformat(),
    }
    if retryable_errors:
        raise PhotoSyncRetryableError(
            f"photo outbox retryable failures ({len(retryable_errors)}): "
            + "; ".join(retryable_errors[:3])
        )
    return summary
