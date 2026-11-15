"""Operate the WhatsApp media archive: stats, retry, backfill, retention/orphan reports.

Never deletes archived media. The only deletion is ``--reconcile-orphans --delete``,
which removes files (and ``.part`` leftovers) that NO archive row references.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Count, Sum
from django.utils import timezone

from apps.integrations.models import WhatsAppMediaArchive, WhatsAppMessage
from apps.integrations.whatsapp import media_archive
from apps.integrations.whatsapp.media_archive import archive_enabled, archive_media, ensure_media_archive
from apps.integrations.whatsapp.media_reconcile import reconcile_orphans

Status = WhatsAppMediaArchive.Status
_MEDIA_TYPES = ("image", "document", "video", "audio", "sticker")


class Command(BaseCommand):
    help = "WhatsApp media archive: --stats, --retry-failed, --backfill, --retention-report."

    def add_arguments(self, parser):
        parser.add_argument("--stats", action="store_true")
        parser.add_argument("--retry-failed", action="store_true", help="Retry failed rows now.")
        parser.add_argument(
            "--include-permanent",
            action="store_true",
            help="With --retry-failed: also reset permanent_failed rows (e.g. after fixing the token).",
        )
        parser.add_argument("--backfill", action="store_true", help="Create rows for media messages without one.")
        parser.add_argument("--since", help="YYYY-MM-DD lower bound for --backfill (Meta keeps media ~30 days).")
        parser.add_argument("--id", type=int, help="Archive a single row now.")
        parser.add_argument("--inline", action="store_true", help="Run downloads in this process, not via Celery.")
        parser.add_argument("--retention-report", action="store_true", help="List rows past retention_until (report only).")
        parser.add_argument("--reconcile-orphans", action="store_true", help="Report files no row references, and rows missing their file.")
        parser.add_argument("--delete", action="store_true", help="With --reconcile-orphans: also remove unreferenced files.")
        parser.add_argument("--min-age-hours", type=float, default=24.0, help="Ignore files younger than this (default 24).")

    def handle(self, *args, **opts):
        if not archive_enabled() and (opts["retry_failed"] or opts["backfill"] or opts["id"]):
            self.stdout.write(self.style.WARNING("WHATSAPP_MEDIA_ARCHIVE_ENABLED is false: nothing done."))
            return
        if opts["stats"] or not any(
            opts[k] for k in ("retry_failed", "backfill", "id", "retention_report", "reconcile_orphans")
        ):
            self._stats()
        if opts["backfill"]:
            self._backfill(opts["since"], opts["inline"])
        if opts["retry_failed"]:
            self._retry(opts["include_permanent"], opts["inline"])
        if opts["id"]:
            # An operator asking for one row means "now": make a failed row due first.
            WhatsAppMediaArchive.objects.filter(pk=opts["id"], status=Status.FAILED).update(
                next_retry_at=timezone.now()
            )
            self.stdout.write(str(archive_media(opts["id"])))
        if opts["retention_report"]:
            self._retention_report()
        if opts["reconcile_orphans"]:
            self._reconcile(opts["min_age_hours"], opts["delete"])

    def _run(self, archive_id: int, inline: bool) -> None:
        if inline:
            self.stdout.write(f"{archive_id}: {archive_media(archive_id)['status']}")
        else:
            media_archive.enqueue_archive(archive_id)

    def _stats(self) -> None:
        self.stdout.write(f"enabled={archive_enabled()}")
        root = Path(settings.PRIVATE_MEDIA_ROOT)
        writable = root.is_dir() and os.access(root, os.W_OK | os.X_OK)
        self.stdout.write(f"private_root={root} exists={root.is_dir()} writable={writable}")
        if archive_enabled() and not writable:
            self.stdout.write(self.style.ERROR(
                "private_root is not writable: downloads are deferred. "
                "chown 1000:1000 and chmod 700 the host directory."
            ))
        rows = (
            WhatsAppMediaArchive.objects.values("status", "sensitivity")
            .annotate(n=Count("id"), size=Sum("size_bytes"))
            .order_by("status", "sensitivity")
        )
        for r in rows:
            self.stdout.write(f"{r['status']:18} {r['sensitivity']:28} n={r['n']} bytes={r['size'] or 0}")

    def _backfill(self, since: str | None, inline: bool) -> None:
        qs = WhatsAppMessage.objects.filter(message_type__in=_MEDIA_TYPES, media_archives__isnull=True)
        if since:
            start = timezone.make_aware(datetime.strptime(since, "%Y-%m-%d"))
            qs = qs.filter(created_at__gte=start)
        created = 0
        for message in qs.order_by("created_at").iterator():
            archive, was_created = ensure_media_archive(message)
            if archive is not None and was_created:
                created += 1
                self._run(archive.pk, inline)
        self.stdout.write(f"backfill: {created} rows created")

    def _retry(self, include_permanent: bool, inline: bool) -> None:
        now = timezone.now()
        if include_permanent:
            WhatsAppMediaArchive.objects.filter(status=Status.PERMANENT_FAILED).update(
                status=Status.FAILED, attempts=0, next_retry_at=now
            )
        # Backoff gates claiming, so "retry now" must make the rows due.
        WhatsAppMediaArchive.objects.filter(status=Status.FAILED).update(next_retry_at=now)
        ids = list(WhatsAppMediaArchive.objects.filter(status=Status.FAILED).values_list("pk", flat=True))
        for archive_id in ids:
            self._run(archive_id, inline)
        self.stdout.write(f"retry: {len(ids)} rows")

    def _retention_report(self) -> None:
        now = timezone.now()
        due = WhatsAppMediaArchive.objects.filter(status=Status.ARCHIVED, retention_until__lte=now)
        for r in due.values("sensitivity").annotate(n=Count("id"), size=Sum("size_bytes")):
            self.stdout.write(f"past retention: {r['sensitivity']} n={r['n']} bytes={r['size'] or 0}")
        self.stdout.write("report only: nothing was deleted")

    def _reconcile(self, min_age_hours: float, delete: bool) -> None:
        report = reconcile_orphans(min_age_hours=min_age_hours, delete=delete)
        self.stdout.write(f"orphan files (no row references them): {len(report.orphan_files)}")
        for name in report.orphan_files:
            self.stdout.write(f"  {name}")
        self.stdout.write(f"leftover .part files: {len(report.part_files)}")
        for name in report.part_files:
            self.stdout.write(f"  {name}")
        self.stdout.write(f"archived rows whose file is missing: {len(report.missing_files)} {report.missing_files[:20]}")
        if delete:
            self.stdout.write(f"deleted: {len(report.deleted)}")
        else:
            self.stdout.write("report only: pass --delete to remove unreferenced files")
