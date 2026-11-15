"""Reliable archive of WhatsApp media (inbound messages and Business App echoes).

Design (docs/operations/whatsapp-media-archive.md):
- the webhook only writes a ``pending`` row (DB-only) and enqueues a Celery task;
- the task claims the row with a lease, streams the file into private storage,
  verifies size/sha256 and records the outcome;
- failed downloads retry with backoff (task countdown + beat sweeper safety net);
- nothing is ever deleted from Meta or from disk here (PR 2 / retention decision).

This module is independent of document intake: it never touches DocumentIntakeImage.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.files import File
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.integrations.models import WhatsAppMediaArchive, WhatsAppMessage
from apps.integrations.whatsapp.config import access_token_from_env
from apps.integrations.whatsapp.media_download import (
    WhatsAppMediaError,
    extract_media_descriptor,
    fetch_whatsapp_media_to_file,
)

logger = logging.getLogger(__name__)

Status = WhatsAppMediaArchive.Status
Sensitivity = WhatsAppMediaArchive.Sensitivity

BACKOFF_SECONDS = (60, 300, 1800, 7200, 21600, 43200, 86400)
LEASE_SECONDS = 600
PENDING_GRACE_SECONDS = 300
QUOTA_DEFER_SECONDS = 3600
_SPOOL_MAX_BYTES = 8 * 1024 * 1024

# Extension comes from this whitelist (never from guest-supplied names).
_MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/heic": ".heic",
    "application/pdf": ".pdf",
    "video/mp4": ".mp4",
    "video/3gpp": ".3gp",
    "video/quicktime": ".mov",
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/amr": ".amr",
}

# Images and documents may be guest IDs: treated as personal-document candidates.
_PERSONAL_KINDS = frozenset({"image", "document"})


def archive_enabled() -> bool:
    return bool(getattr(settings, "WHATSAPP_MEDIA_ARCHIVE_ENABLED", False))


def _extension_for(mime: str) -> str:
    return _MIME_EXTENSIONS.get((mime or "").split(";")[0].strip().lower(), ".bin")


def _retention_until(sensitivity: str, now):
    key = (
        "WHATSAPP_MEDIA_RETENTION_DAYS_PERSONAL"
        if sensitivity == Sensitivity.PERSONAL_DOCUMENT_CANDIDATE
        else "WHATSAPP_MEDIA_RETENTION_DAYS_STANDARD"
    )
    days = int(getattr(settings, key, 0) or 0)
    return now + timedelta(days=days) if days > 0 else None


# ---------------------------------------------------------------- registration


def ensure_media_archive(
    message: WhatsAppMessage,
) -> tuple[WhatsAppMediaArchive | None, bool]:
    """Idempotently create the archive row for a message's media (DB only)."""
    if not archive_enabled():
        return None, False
    descriptor = extract_media_descriptor(message.raw_payload or {})
    if descriptor is None:
        return None, False
    sensitivity = (
        Sensitivity.PERSONAL_DOCUMENT_CANDIDATE
        if descriptor.kind in _PERSONAL_KINDS
        else Sensitivity.STANDARD
    )
    return WhatsAppMediaArchive.objects.get_or_create(
        message=message,
        media_id=descriptor.media_id,
        defaults={
            "tenant_id": message.tenant_id,
            "wamid": message.wamid,
            "kind": descriptor.kind,
            "sensitivity": sensitivity,
            "declared_mime": descriptor.mime_type[:128],
            "original_filename": descriptor.filename[:255],
            "caption": descriptor.caption,
        },
    )


def enqueue_archive(archive_id: int, *, countdown: int | None = None) -> None:
    from apps.integrations.whatsapp.media_archive_tasks import archive_whatsapp_media

    try:
        if countdown:
            archive_whatsapp_media.apply_async(args=[archive_id], countdown=countdown)
        else:
            archive_whatsapp_media.delay(archive_id)
    except Exception:  # broker down: the row stays pending, the sweeper picks it up
        logger.warning("whatsapp media archive enqueue failed archive_id=%s", archive_id, exc_info=True)


def record_media_for_message(message: WhatsAppMessage) -> None:
    """Webhook hook. Never raises: archiving must not break message ingestion."""
    try:
        with transaction.atomic():  # savepoint: a DB error must not poison the caller
            archive, created = ensure_media_archive(message)
    except Exception:
        logger.exception("whatsapp media archive row failed message_id=%s", message.pk)
        return
    if archive is not None and created:
        archive_id = archive.pk
        transaction.on_commit(lambda: enqueue_archive(archive_id))


# -------------------------------------------------------------------- download


def _quota_block_reason() -> str:
    max_total = int(getattr(settings, "WHATSAPP_MEDIA_MAX_TOTAL_BYTES", 0) or 0)
    if max_total > 0:
        total = (
            WhatsAppMediaArchive.objects.filter(status=Status.ARCHIVED).aggregate(
                total=Sum("size_bytes")
            )["total"]
            or 0
        )
        if total >= max_total:
            return f"archive size limit reached ({total} >= {max_total} bytes)"
    min_free = int(getattr(settings, "WHATSAPP_MEDIA_MIN_FREE_BYTES", 0) or 0)
    if min_free > 0:
        root = Path(settings.PRIVATE_MEDIA_ROOT)
        root.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(root).free
        if free < min_free:
            return f"free disk space {free} below threshold {min_free} bytes"
    return ""


def _fail(archive_id: int, attempt: int, error: str, *, retryable: bool) -> dict[str, Any]:
    now = timezone.now()
    max_attempts = int(getattr(settings, "WHATSAPP_MEDIA_MAX_ATTEMPTS", 8))
    with transaction.atomic():
        row = WhatsAppMediaArchive.objects.select_for_update().get(pk=archive_id)
        if row.attempts != attempt or row.status == Status.ARCHIVED:
            return {"status": "superseded"}
        permanent = (not retryable) or attempt >= max_attempts
        row.last_error = error[:500]
        row.lease_until = None
        if permanent:
            row.status = Status.PERMANENT_FAILED
            row.next_retry_at = None
            delay = None
        else:
            delay = BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)]
            row.status = Status.FAILED
            row.next_retry_at = now + timedelta(seconds=delay)
        row.save(update_fields=["status", "last_error", "lease_until", "next_retry_at", "updated_at"])
    if permanent:
        logger.warning("whatsapp media archive permanent failure id=%s: %s", archive_id, error[:200])
        return {"status": "permanent_failed", "archive_id": archive_id}
    logger.info("whatsapp media archive failed id=%s attempt=%s retry_in=%ss", archive_id, attempt, delay)
    enqueue_archive(archive_id, countdown=delay)
    return {"status": "failed", "archive_id": archive_id, "retry_in": delay}


def archive_media(archive_id: int) -> dict[str, Any]:
    """Download one media object into private storage. Safe to call repeatedly."""
    if not archive_enabled():
        return {"status": "disabled"}

    now = timezone.now()
    with transaction.atomic():
        row = WhatsAppMediaArchive.objects.select_for_update().filter(pk=archive_id).first()
        if row is None:
            return {"status": "missing"}
        if row.status == Status.PERMANENT_FAILED:
            return {"status": "permanent_failed", "archive_id": archive_id}
        if row.status == Status.ARCHIVED and row.file and row.file.storage.exists(row.file.name):
            return {"status": "already_archived", "archive_id": archive_id}
        if row.status == Status.DOWNLOADING and row.lease_until and row.lease_until > now:
            return {"status": "in_progress", "archive_id": archive_id}

        try:
            blocked = _quota_block_reason()
        except OSError as exc:
            blocked = f"private storage unavailable: {exc}"
        if blocked:
            logger.error("whatsapp media archive deferred id=%s: %s", archive_id, blocked)
            row.status = Status.FAILED
            row.last_error = blocked[:500]
            row.next_retry_at = now + timedelta(seconds=QUOTA_DEFER_SECONDS)
            row.save(update_fields=["status", "last_error", "next_retry_at", "updated_at"])
            return {"status": "deferred", "reason": blocked}

        row.status = Status.DOWNLOADING
        row.attempts += 1
        row.last_attempt_at = now
        row.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        row.save(update_fields=["status", "attempts", "last_attempt_at", "lease_until", "updated_at"])
        attempt = row.attempts

    max_bytes = int(getattr(settings, "WHATSAPP_MEDIA_MAX_BYTES", 0) or 0)
    saved_name = ""
    try:
        with tempfile.SpooledTemporaryFile(max_size=_SPOOL_MAX_BYTES) as tmp:
            result = fetch_whatsapp_media_to_file(
                media_id=row.media_id,
                dest=tmp,
                max_bytes=max_bytes,
                access_token=access_token_from_env(),
            )
            tmp.seek(0)
            mime = result.mime_type or row.declared_mime
            filename = f"{row.pk}-{result.sha256[:12]}{_extension_for(mime)}"
            row.file.save(filename, File(tmp), save=False)
            saved_name = row.file.name
    except WhatsAppMediaError as exc:
        return _fail(archive_id, attempt, str(exc), retryable=exc.retryable)
    except Exception as exc:  # storage/OS errors: retryable, never leave a half file
        logger.exception("whatsapp media archive storage error id=%s", archive_id)
        return _fail(archive_id, attempt, f"{type(exc).__name__}: {exc}", retryable=True)

    done_at = timezone.now()
    try:
        with transaction.atomic():
            fresh = WhatsAppMediaArchive.objects.select_for_update().get(pk=archive_id)
            if fresh.attempts != attempt or fresh.status == Status.ARCHIVED:
                raise _Superseded
            fresh.file.name = saved_name
            fresh.status = Status.ARCHIVED
            fresh.mime_type = mime[:128]
            fresh.size_bytes = result.size
            fresh.sha256 = result.sha256
            fresh.archived_at = done_at
            fresh.retention_until = _retention_until(fresh.sensitivity, done_at)
            fresh.last_error = ""
            fresh.next_retry_at = None
            fresh.lease_until = None
            fresh.save()
    except _Superseded:
        row.file.storage.delete(saved_name)
        return {"status": "superseded"}
    except Exception as exc:
        row.file.storage.delete(saved_name)
        logger.exception("whatsapp media archive finalize failed id=%s", archive_id)
        return _fail(archive_id, attempt, f"{type(exc).__name__}: {exc}", retryable=True)

    logger.info(
        "whatsapp media archived id=%s kind=%s size=%s sensitivity=%s",
        archive_id, row.kind, result.size, row.sensitivity,
    )
    return {"status": "archived", "archive_id": archive_id, "size": result.size}


class _Superseded(Exception):
    pass


# ----------------------------------------------------------------------- sweep


def sweep_media_archive(*, limit: int = 200) -> dict[str, int]:
    """Re-enqueue due failures, lost enqueues and abandoned (expired-lease) claims."""
    if not archive_enabled():
        return {"enqueued": 0}
    now = timezone.now()
    due = (
        Q(status=Status.FAILED, next_retry_at__lte=now)
        | Q(status=Status.PENDING, created_at__lte=now - timedelta(seconds=PENDING_GRACE_SECONDS))
        | Q(status=Status.DOWNLOADING, lease_until__lt=now)
    )
    ids = list(
        WhatsAppMediaArchive.objects.filter(due).order_by("created_at").values_list("pk", flat=True)[:limit]
    )
    for archive_id in ids:
        enqueue_archive(archive_id)
    return {"enqueued": len(ids)}
