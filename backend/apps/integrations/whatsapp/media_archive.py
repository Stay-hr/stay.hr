"""Reliable archive of WhatsApp media (inbound messages and Business App echoes).

Design (docs/operations/whatsapp-media-archive.md):
- the webhook only writes a ``pending`` row (DB-only) and enqueues a Celery task;
- the task claims the row with a lease, streams the file into private storage,
  verifies size/sha256 and records the outcome;
- the beat **sweeper is the only retry scheduler** (no countdown/ETA tasks), so retry
  timing lives in one place: ``next_retry_at`` on the row;
- claiming is gated: a FAILED row is only claimable when due, and a row that has used
  ``WHATSAPP_MEDIA_MAX_ATTEMPTS`` claims (including crashed ones) becomes permanent;
- files are published atomically under deterministic names, so a crash between the
  file write and the DB update is healed by the next attempt instead of orphaning.

Nothing is deleted from Meta here (PR 2). Deleting *files* happens only as a reaction
to a deleted DB row (``media_file_cleanup``) or an explicit operator command.
This module is independent of document intake: it never touches DocumentIntakeImage.
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import tempfile
import uuid
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

# --- Timing contract. tests/test_whatsapp_media_archive_coherence.py pins the ordering:
#     deadline < soft limit < hard limit < lease, and the beat interval == sweep interval.
SWEEP_INTERVAL_SECONDS = 60
PENDING_GRACE_SECONDS = 300  # > sweep interval: lets the on_commit enqueue win first
DOWNLOAD_DEADLINE_SECONDS = 480  # total streaming time, enforced inside the fetch loop
TASK_SOFT_TIME_LIMIT = 540
TASK_TIME_LIMIT = 600
LEASE_SECONDS = 900  # must outlive the hard limit, or a live worker could be reclaimed
DEFER_SECONDS = 3600  # quota / unwritable storage: wait, do not burn attempts
BACKOFF_SECONDS = (60, 300, 1800, 7200, 21600, 43200, 86400)

_SPOOL_MAX_BYTES = 8 * 1024 * 1024
# OS errors that mean "this host is misconfigured / full", not "this media is bad".
_ENVIRONMENT_ERRNOS = frozenset(
    {errno.EACCES, errno.EPERM, errno.ENOSPC, errno.EROFS, errno.EDQUOT}
)

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


def max_attempts() -> int:
    return int(getattr(settings, "WHATSAPP_MEDIA_MAX_ATTEMPTS", 8))


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


def enqueue_archive(archive_id: int) -> None:
    """Queue one immediate attempt. Never schedules retries (the sweeper does)."""
    from apps.integrations.whatsapp.media_archive_tasks import archive_whatsapp_media

    try:
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


def _deferral_reason() -> str:
    """Why a download must wait (storage unusable / quota), without burning an attempt."""
    root = Path(settings.PRIVATE_MEDIA_ROOT)
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        return f"private storage unavailable: {exc}"
    if not os.access(root, os.W_OK | os.X_OK):
        return f"private storage not writable: {root}"

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
        free = shutil.disk_usage(root).free
        if free < min_free:
            return f"free disk space {free} below threshold {min_free} bytes"
    return ""


def _fail(archive_id: int, attempt: int, error: str, *, retryable: bool) -> dict[str, Any]:
    """Record a counted failure. Retry timing = next_retry_at, picked up by the sweeper."""
    now = timezone.now()
    with transaction.atomic():
        row = WhatsAppMediaArchive.objects.select_for_update().get(pk=archive_id)
        if row.attempts != attempt or row.status == Status.ARCHIVED:
            return {"status": "superseded"}
        permanent = (not retryable) or attempt >= max_attempts()
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
    return {"status": "failed", "archive_id": archive_id, "retry_in": delay}


def _defer_after_claim(archive_id: int, attempt: int, reason: str) -> dict[str, Any]:
    """Environment problem hit mid-attempt (EACCES/ENOSPC/...): refund the attempt."""
    with transaction.atomic():
        row = WhatsAppMediaArchive.objects.select_for_update().get(pk=archive_id)
        if row.attempts != attempt or row.status == Status.ARCHIVED:
            return {"status": "superseded"}
        row.attempts = max(attempt - 1, 0)
        row.status = Status.FAILED
        row.last_error = reason[:500]
        row.lease_until = None
        row.next_retry_at = timezone.now() + timedelta(seconds=DEFER_SECONDS)
        row.save(
            update_fields=["attempts", "status", "last_error", "lease_until", "next_retry_at", "updated_at"]
        )
    logger.error("whatsapp media archive deferred id=%s: %s", archive_id, reason[:200])
    return {"status": "deferred", "archive_id": archive_id, "reason": reason}


def _publish_file(row: WhatsAppMediaArchive, tmp, sha256: str, mime: str) -> str:
    """Write ``tmp`` to its deterministic final path atomically; return the stored name.

    The bytes go to an attempt-unique ``.part`` file first and are moved into place with
    ``os.replace``: a reader never sees a partial file, a retry (or a concurrent attempt
    with identical content) rewrites the same final path, and the only file a failed
    attempt can leave behind is its own ``.part``, which it is safe to remove.
    """
    storage = row.file.storage
    final_name = row.file.field.generate_filename(row, f"{row.pk}-{sha256[:12]}{_extension_for(mime)}")
    part_name = storage.save(f"{final_name}.{uuid.uuid4().hex[:8]}.part", File(tmp))
    try:
        os.replace(storage.path(part_name), storage.path(final_name))
    except Exception:
        try:
            storage.delete(part_name)  # unique to this attempt: never shared
        except OSError:
            logger.warning("whatsapp media archive could not remove part file id=%s", row.pk)
        raise
    return final_name


def _finalize_archive(archive_id: int, attempt: int, final_name: str, result, mime: str) -> str:
    done_at = timezone.now()
    with transaction.atomic():
        fresh = WhatsAppMediaArchive.objects.select_for_update().get(pk=archive_id)
        if fresh.attempts != attempt or fresh.status == Status.ARCHIVED:
            return "superseded"
        # Never mark archived unless the published file is really there.
        if not fresh.file.storage.exists(final_name):
            raise FileNotFoundError(final_name)
        fresh.file.name = final_name
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
    return "archived"


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
        # Duplicate/redelivered tasks must not shorten the backoff.
        if row.status == Status.FAILED and row.next_retry_at and row.next_retry_at > now:
            return {"status": "not_due", "archive_id": archive_id}
        # Every claim counts, including claims whose worker died before reporting a
        # failure: this is what bounds a "poison" row that keeps killing workers.
        if row.attempts >= max_attempts():
            row.status = Status.PERMANENT_FAILED
            row.last_error = (
                f"max attempts ({max_attempts()}) reached"
                + (f"; last error: {row.last_error}" if row.last_error else "; worker died mid-download?")
            )[:500]
            row.lease_until = None
            row.next_retry_at = None
            row.save(update_fields=["status", "last_error", "lease_until", "next_retry_at", "updated_at"])
            logger.warning("whatsapp media archive gave up id=%s after %s claims", archive_id, row.attempts)
            return {"status": "permanent_failed", "archive_id": archive_id}

        blocked = _deferral_reason()
        if blocked:
            logger.error("whatsapp media archive deferred id=%s: %s", archive_id, blocked)
            row.status = Status.FAILED
            row.last_error = blocked[:500]
            row.next_retry_at = now + timedelta(seconds=DEFER_SECONDS)
            row.save(update_fields=["status", "last_error", "next_retry_at", "updated_at"])
            return {"status": "deferred", "reason": blocked}

        row.status = Status.DOWNLOADING
        row.attempts += 1
        row.last_attempt_at = now
        row.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        row.save(update_fields=["status", "attempts", "last_attempt_at", "lease_until", "updated_at"])
        attempt = row.attempts

    max_bytes = int(getattr(settings, "WHATSAPP_MEDIA_MAX_BYTES", 0) or 0)
    try:
        with tempfile.SpooledTemporaryFile(max_size=_SPOOL_MAX_BYTES) as tmp:
            result = fetch_whatsapp_media_to_file(
                media_id=row.media_id,
                dest=tmp,
                max_bytes=max_bytes,
                access_token=access_token_from_env(),
                deadline_seconds=DOWNLOAD_DEADLINE_SECONDS,
            )
            tmp.seek(0)
            mime = result.mime_type or row.declared_mime
            final_name = _publish_file(row, tmp, result.sha256, mime)
    except WhatsAppMediaError as exc:
        return _fail(archive_id, attempt, str(exc), retryable=exc.retryable)
    except OSError as exc:
        if exc.errno in _ENVIRONMENT_ERRNOS:
            return _defer_after_claim(archive_id, attempt, f"{type(exc).__name__}: {exc}")
        logger.exception("whatsapp media archive storage error id=%s", archive_id)
        return _fail(archive_id, attempt, f"{type(exc).__name__}: {exc}", retryable=True)
    except Exception as exc:
        logger.exception("whatsapp media archive error id=%s", archive_id)
        return _fail(archive_id, attempt, f"{type(exc).__name__}: {exc}", retryable=True)

    try:
        outcome = _finalize_archive(archive_id, attempt, final_name, result, mime)
    except Exception as exc:
        # The published file is left in place on purpose: the retry rewrites the same
        # deterministic path. Only an explicit operator reconcile removes true orphans.
        logger.exception("whatsapp media archive finalize failed id=%s", archive_id)
        return _fail(archive_id, attempt, f"{type(exc).__name__}: {exc}", retryable=True)
    if outcome == "superseded":
        return {"status": "superseded"}

    logger.info(
        "whatsapp media archived id=%s kind=%s size=%s sensitivity=%s",
        archive_id, row.kind, result.size, row.sensitivity,
    )
    return {"status": "archived", "archive_id": archive_id, "size": result.size}


# ----------------------------------------------------------------------- sweep


def sweep_media_archive(*, limit: int = 200) -> dict[str, int]:
    """The single retry scheduler: due failures, lost enqueues, abandoned claims."""
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
