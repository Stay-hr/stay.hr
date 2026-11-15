"""Operate the WhatsApp media archive: stats, retry, backfill, retention report.

Never deletes anything (retention is report-only until a legal basis is defined).
"""

from __future__ import annotations

from datetime import datetime

from django.core.management.base import BaseCommand
from django.db.models import Count, Sum
from django.utils import timezone

from apps.integrations.models import WhatsAppMediaArchive, WhatsAppMessage
from apps.integrations.whatsapp import media_archive
from apps.integrations.whatsapp.media_archive import archive_enabled, archive_media, ensure_media_archive

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

    def handle(self, *args, **opts):
        if not archive_enabled() and (opts["retry_failed"] or opts["backfill"] or opts["id"]):
            self.stdout.write(self.style.WARNING("WHATSAPP_MEDIA_ARCHIVE_ENABLED is false: nothing done."))
            return
        if opts["stats"] or not any(
            opts[k] for k in ("retry_failed", "backfill", "id", "retention_report")
        ):
            self._stats()
        if opts["backfill"]:
            self._backfill(opts["since"], opts["inline"])
        if opts["retry_failed"]:
            self._retry(opts["include_permanent"], opts["inline"])
        if opts["id"]:
            self.stdout.write(str(archive_media(opts["id"])))
        if opts["retention_report"]:
            self._retention_report()

    def _run(self, archive_id: int, inline: bool) -> None:
        if inline:
            self.stdout.write(f"{archive_id}: {archive_media(archive_id)['status']}")
        else:
            media_archive.enqueue_archive(archive_id)

    def _stats(self) -> None:
        self.stdout.write(f"enabled={archive_enabled()}")
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
        statuses = [Status.FAILED] + ([Status.PERMANENT_FAILED] if include_permanent else [])
        ids = list(WhatsAppMediaArchive.objects.filter(status__in=statuses).values_list("pk", flat=True))
        if include_permanent:
            WhatsAppMediaArchive.objects.filter(pk__in=ids, status=Status.PERMANENT_FAILED).update(
                status=Status.FAILED, attempts=0
            )
        for archive_id in ids:
            self._run(archive_id, inline)
        self.stdout.write(f"retry: {len(ids)} rows")

    def _retention_report(self) -> None:
        now = timezone.now()
        due = WhatsAppMediaArchive.objects.filter(status=Status.ARCHIVED, retention_until__lte=now)
        for r in due.values("sensitivity").annotate(n=Count("id"), size=Sum("size_bytes")):
            self.stdout.write(f"past retention: {r['sensitivity']} n={r['n']} bytes={r['size'] or 0}")
        self.stdout.write("report only: nothing was deleted")
