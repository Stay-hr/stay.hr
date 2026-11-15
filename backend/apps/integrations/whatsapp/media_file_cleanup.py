"""Reliable on-disk cleanup for deleted WhatsAppMediaArchive rows.

Why not a plain ``post_delete`` that unlinks the file: the signal fires *inside* the
deleting transaction, so a later rollback would leave rows restored but files gone.

Instead:
1. ``post_delete`` only writes a ``WhatsAppMediaFileDeletion`` outbox row — in the same
   transaction, so it commits or rolls back together with the deletion — and registers
   ``transaction.on_commit`` to enqueue the cleanup task.
2. The task removes the file (idempotent; refuses paths outside the archive; refuses
   files that another row still references).
3. A beat sweeper re-enqueues pending outbox rows, so a broker outage at commit time
   only delays the cleanup.

Independent of WHATSAPP_MEDIA_ARCHIVE_ENABLED: files must be cleaned even if the
archive is later switched off. CASCADE from message/tenant deletion is covered because
the signal fires for every collected instance (this receiver also disables Django's
signal-less fast-delete path for the model).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import PurePosixPath

from celery import shared_task
from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.utils import timezone

from apps.integrations.models import WhatsAppMediaArchive, WhatsAppMediaFileDeletion

logger = logging.getLogger(__name__)

Status = WhatsAppMediaFileDeletion.Status

ARCHIVE_PREFIX = "whatsapp_media/"
MAX_ATTEMPTS = 12
BACKOFF_SECONDS = (60, 300, 1800, 7200, 21600, 86400)
SWEEP_INTERVAL_SECONDS = 300


@receiver(post_delete, sender=WhatsAppMediaArchive, dispatch_uid="wa_media_archive_file_outbox")
def record_file_deletion(sender, instance: WhatsAppMediaArchive, **kwargs) -> None:
    name = instance.file.name if instance.file else ""
    if not name:
        return
    # No try/except on purpose: if the outbox cannot be written the whole deletion
    # must fail rather than silently strand a personal document on disk.
    outbox, created = WhatsAppMediaFileDeletion.objects.get_or_create(
        file_name=name,
        defaults={"archive_id": instance.pk, "tenant_id": instance.tenant_id},
    )
    if created:
        outbox_id = outbox.pk
        transaction.on_commit(lambda: enqueue_file_cleanup(outbox_id))


def enqueue_file_cleanup(outbox_id: int) -> None:
    try:
        cleanup_whatsapp_media_file.delay(outbox_id)
    except Exception:  # broker down: the outbox row is durable, the sweeper retries
        logger.warning("whatsapp media cleanup enqueue failed outbox_id=%s", outbox_id, exc_info=True)


def _safe_archive_name(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        name.startswith(ARCHIVE_PREFIX)
        and not path.is_absolute()
        and ".." not in path.parts
        and "\\" not in name
    )


def cleanup_media_file(outbox_id: int) -> dict:
    now = timezone.now()
    with transaction.atomic():
        row = WhatsAppMediaFileDeletion.objects.select_for_update().filter(pk=outbox_id).first()
        if row is None:
            return {"status": "missing"}
        if row.status != Status.PENDING:
            return {"status": row.status}  # duplicate task: already handled
        if row.next_attempt_at > now:
            return {"status": "not_due"}

        if not _safe_archive_name(row.file_name):
            row.status = Status.DEAD
            row.last_error = "refused: path is outside the archive prefix"
            row.completed_at = now
            row.save(update_fields=["status", "last_error", "completed_at"])
            logger.error("whatsapp media cleanup refused unsafe path outbox_id=%s", outbox_id)
            return {"status": Status.DEAD}

        # Protection against removing a file another record still uses.
        if WhatsAppMediaArchive.objects.filter(file=row.file_name).exists():
            row.status = Status.SKIPPED_IN_USE
            row.completed_at = now
            row.save(update_fields=["status", "completed_at"])
            return {"status": Status.SKIPPED_IN_USE}

        storage = WhatsAppMediaArchive._meta.get_field("file").storage
        try:
            storage.delete(row.file_name)  # missing file is not an error: idempotent
        except Exception as exc:
            row.attempts += 1
            row.last_error = f"{type(exc).__name__}: {exc}"[:500]
            if row.attempts >= MAX_ATTEMPTS:
                row.status = Status.DEAD
                row.completed_at = now
                logger.error("whatsapp media cleanup gave up outbox_id=%s", outbox_id)
            else:
                delay = BACKOFF_SECONDS[min(row.attempts - 1, len(BACKOFF_SECONDS) - 1)]
                row.next_attempt_at = now + timedelta(seconds=delay)
            row.save(update_fields=["attempts", "last_error", "status", "next_attempt_at", "completed_at"])
            return {"status": "failed", "attempts": row.attempts}

        row.status = Status.DONE
        row.completed_at = now
        row.save(update_fields=["status", "completed_at"])
    return {"status": Status.DONE}


def sweep_file_deletions(*, limit: int = 200) -> dict:
    now = timezone.now()
    ids = list(
        WhatsAppMediaFileDeletion.objects.filter(status=Status.PENDING, next_attempt_at__lte=now)
        .order_by("created_at")
        .values_list("pk", flat=True)[:limit]
    )
    for outbox_id in ids:
        enqueue_file_cleanup(outbox_id)
    return {"enqueued": len(ids)}


@shared_task
def cleanup_whatsapp_media_file(outbox_id: int) -> dict:
    return cleanup_media_file(outbox_id)


@shared_task
def sweep_whatsapp_media_file_deletions() -> dict:
    return sweep_file_deletions()
