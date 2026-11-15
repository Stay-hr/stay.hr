from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from apps.integrations.models import WhatsAppMediaArchive, WhatsAppMessage
from apps.integrations.tests.media_archive_support import ArchiveTestBase, fake_fetch
from apps.integrations.whatsapp.media_archive import sweep_media_archive

Status = WhatsAppMediaArchive.Status
FETCH = "apps.integrations.whatsapp.media_archive.fetch_whatsapp_media_to_file"


class MediaArchiveSweeperTests(ArchiveTestBase):
    def _row(self, n: int, **fields) -> WhatsAppMediaArchive:
        message = self.make_message(wamid=f"w.{n}", media_id=f"m.{n}")
        return self.make_archive(message=message, **fields)

    def _age(self, row, seconds: int):
        WhatsAppMediaArchive.objects.filter(pk=row.pk).update(
            created_at=timezone.now() - timedelta(seconds=seconds)
        )

    def test_sweeper_picks_only_due_work(self):
        now = timezone.now()
        due_failed = self._row(1, status=Status.FAILED, next_retry_at=now - timedelta(minutes=1))
        later_failed = self._row(2, status=Status.FAILED, next_retry_at=now + timedelta(hours=1))
        lost_pending = self._row(3)
        self._age(lost_pending, 3600)
        fresh_pending = self._row(4)
        stale_claim = self._row(5, status=Status.DOWNLOADING, lease_until=now - timedelta(minutes=1))
        live_claim = self._row(6, status=Status.DOWNLOADING, lease_until=now + timedelta(minutes=5))
        permanent = self._row(7, status=Status.PERMANENT_FAILED)
        done = self._row(8, status=Status.ARCHIVED, file="x", sha256="a" * 64)

        result = sweep_media_archive()
        queued = {c.args[0] for c in self.enqueue.call_args_list}
        self.assertEqual(queued, {due_failed.pk, lost_pending.pk, stale_claim.pk})
        self.assertEqual(result["enqueued"], 3)
        for skipped in (later_failed, fresh_pending, live_claim, permanent, done):
            self.assertNotIn(skipped.pk, queued)

    def test_sweeper_disabled_is_noop(self):
        row = self._row(1, status=Status.FAILED, next_retry_at=timezone.now() - timedelta(minutes=1))
        with override_settings(WHATSAPP_MEDIA_ARCHIVE_ENABLED=False):
            self.assertEqual(sweep_media_archive(), {"enqueued": 0})
        self.enqueue.assert_not_called()
        self.assertEqual(WhatsAppMediaArchive.objects.get(pk=row.pk).status, Status.FAILED)

    def test_command_retry_failed_inline(self):
        row = self._row(1, status=Status.FAILED, next_retry_at=timezone.now() + timedelta(hours=5), attempts=1)
        with patch(FETCH, side_effect=fake_fetch()):
            call_command("whatsapp_media_archive", "--retry-failed", "--inline", stdout=StringIO())
        self.assertEqual(WhatsAppMediaArchive.objects.get(pk=row.pk).status, Status.ARCHIVED)

    def test_command_include_permanent_resets_attempts(self):
        row = self._row(1, status=Status.PERMANENT_FAILED, attempts=3)
        call_command("whatsapp_media_archive", "--retry-failed", "--include-permanent", stdout=StringIO())
        row.refresh_from_db()
        self.assertEqual((row.status, row.attempts), (Status.FAILED, 0))
        self.enqueue.assert_called_once_with(row.pk)

    def test_command_backfill_creates_missing_rows_only(self):
        existing = self.make_message(wamid="w.has", media_id="has")
        self.make_archive(message=existing)
        self.make_message(wamid="w.new", media_id="new")
        WhatsAppMessage.objects.create(
            tenant=self.tenant, wamid="w.text", wa_id="1", direction="inbound", message_type="text",
            raw_payload={"type": "text"},
        )
        out = StringIO()
        call_command("whatsapp_media_archive", "--backfill", stdout=out)
        self.assertIn("1 rows created", out.getvalue())
        self.assertEqual(WhatsAppMediaArchive.objects.count(), 2)

    def test_command_does_nothing_when_disabled(self):
        self.make_message(wamid="w.new", media_id="new")
        out = StringIO()
        with override_settings(WHATSAPP_MEDIA_ARCHIVE_ENABLED=False):
            call_command("whatsapp_media_archive", "--backfill", stdout=out)
        self.assertEqual(WhatsAppMediaArchive.objects.count(), 0)

    def test_retention_report_never_deletes(self):
        row = self._row(
            1, status=Status.ARCHIVED, file="whatsapp_media/x.jpg", sha256="a" * 64,
            size_bytes=10, retention_until=timezone.now() - timedelta(days=1),
        )
        out = StringIO()
        call_command("whatsapp_media_archive", "--retention-report", stdout=out)
        self.assertIn("past retention", out.getvalue())
        self.assertIn("nothing was deleted", out.getvalue())
        self.assertTrue(WhatsAppMediaArchive.objects.filter(pk=row.pk).exists())
