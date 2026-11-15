import os
from datetime import timedelta
from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone

from apps.integrations.models import WhatsAppMediaArchive
from apps.integrations.tests.media_archive_support import ArchiveTestBase, fake_fetch
from apps.integrations.whatsapp.media_archive import BACKOFF_SECONDS, archive_media
from apps.integrations.whatsapp.media_download import WhatsAppMediaError, WhatsAppMediaTooLarge

FETCH = "apps.integrations.whatsapp.media_archive.fetch_whatsapp_media_to_file"
Status = WhatsAppMediaArchive.Status


class MediaArchiveTaskTests(ArchiveTestBase):
    def test_success_archives_to_private_storage(self):
        archive = self.make_archive()
        with patch(FETCH, side_effect=fake_fetch(b"hello-bytes", "image/jpeg")):
            result = archive_media(archive.pk)
        archive.refresh_from_db()
        self.assertEqual(result["status"], "archived")
        self.assertEqual(archive.status, Status.ARCHIVED)
        self.assertEqual(archive.size_bytes, len(b"hello-bytes"))
        self.assertEqual(len(archive.sha256), 64)
        self.assertEqual(archive.mime_type, "image/jpeg")
        self.assertIsNotNone(archive.archived_at)
        self.assertEqual(archive.attempts, 1)
        with archive.file.open("rb") as fh:
            self.assertEqual(fh.read(), b"hello-bytes")
        self.assertTrue(archive.file.path.startswith(self.private_root))
        self.assertIn("/personal/", archive.file.name)  # image => personal candidate

    def test_second_run_is_noop(self):
        archive = self.make_archive()
        with patch(FETCH, side_effect=fake_fetch()) as fetch:
            archive_media(archive.pk)
            self.assertEqual(archive_media(archive.pk)["status"], "already_archived")
        self.assertEqual(fetch.call_count, 1)

    def test_file_has_no_public_url_and_is_outside_media_root(self):
        from django.conf import settings

        archive = self.make_archive()
        with patch(FETCH, side_effect=fake_fetch()):
            archive_media(archive.pk)
        archive.refresh_from_db()
        with self.assertRaises(ValueError):
            archive.file.url
        self.assertFalse(archive.file.path.startswith(str(settings.MEDIA_ROOT)))

    def test_guest_filename_never_reaches_disk_path(self):
        message = self.make_message(kind="document", wamid="w.doc", media_id="doc-1", filename="../../etc/passwd")
        archive = self.make_archive(message=message)
        with patch(FETCH, side_effect=fake_fetch(b"%PDF", "application/pdf")):
            archive_media(archive.pk)
        archive.refresh_from_db()
        self.assertNotIn("..", archive.file.name)
        self.assertNotIn("passwd", archive.file.name)
        self.assertTrue(archive.file.name.endswith(".pdf"))
        self.assertEqual(archive.original_filename, "../../etc/passwd")  # DB only
        self.assertEqual(oct(os.stat(archive.file.path).st_mode & 0o777), "0o600")

    def test_standard_kind_goes_to_standard_bucket(self):
        message = self.make_message(kind="video", wamid="w.v", media_id="v-1")
        archive = self.make_archive(message=message)
        with patch(FETCH, side_effect=fake_fetch(b"vid", "video/mp4")):
            archive_media(archive.pk)
        archive.refresh_from_db()
        self.assertIn("/standard/", archive.file.name)
        self.assertEqual(archive.sensitivity, "standard")

    def test_transient_error_records_backoff_and_leaves_scheduling_to_the_sweeper(self):
        archive = self.make_archive()
        with patch(FETCH, side_effect=WhatsAppMediaError("boom", status_code=503)):
            result = archive_media(archive.pk)
        archive.refresh_from_db()
        self.assertEqual(result["status"], "failed")
        self.assertEqual((archive.status, archive.attempts), (Status.FAILED, 1))
        self.assertIn("boom", archive.last_error)
        delta = (archive.next_retry_at - timezone.now()).total_seconds()
        self.assertTrue(BACKOFF_SECONDS[0] - 5 <= delta <= BACKOFF_SECONDS[0] + 5)
        self.enqueue.assert_not_called()  # no countdown/ETA task: the sweeper is the only scheduler

    def test_404_is_permanent(self):
        archive = self.make_archive()
        with patch(FETCH, side_effect=WhatsAppMediaError("gone", status_code=404)):
            result = archive_media(archive.pk)
        archive.refresh_from_db()
        self.assertEqual(result["status"], "permanent_failed")
        self.assertEqual(archive.status, Status.PERMANENT_FAILED)
        self.assertIsNone(archive.next_retry_at)
        self.enqueue.assert_not_called()

    def test_too_large_is_permanent(self):
        archive = self.make_archive()
        with patch(FETCH, side_effect=WhatsAppMediaTooLarge("too big")):
            archive_media(archive.pk)
        archive.refresh_from_db()
        self.assertEqual(archive.status, Status.PERMANENT_FAILED)

    def test_missing_token_is_retryable(self):
        archive = self.make_archive()
        with patch(FETCH, side_effect=WhatsAppMediaError("WHATSAPP_ACCESS_TOKEN missing")):
            archive_media(archive.pk)
        archive.refresh_from_db()
        self.assertEqual(archive.status, Status.FAILED)

    def test_attempts_exhausted_becomes_permanent(self):
        archive = self.make_archive()  # MAX_ATTEMPTS=3
        with patch(FETCH, side_effect=WhatsAppMediaError("x", status_code=500)):
            for _ in range(3):
                WhatsAppMediaArchive.objects.filter(pk=archive.pk, status=Status.FAILED).update(
                    next_retry_at=timezone.now()
                )
                archive_media(archive.pk)
        archive.refresh_from_db()
        self.assertEqual((archive.status, archive.attempts), (Status.PERMANENT_FAILED, 3))

    def test_fresh_lease_blocks_concurrent_claim(self):
        archive = self.make_archive(
            status=Status.DOWNLOADING, lease_until=timezone.now() + timedelta(minutes=5), attempts=1
        )
        with patch(FETCH) as fetch:
            self.assertEqual(archive_media(archive.pk)["status"], "in_progress")
        fetch.assert_not_called()

    def test_expired_lease_is_reclaimed(self):
        archive = self.make_archive(
            status=Status.DOWNLOADING, lease_until=timezone.now() - timedelta(minutes=1), attempts=1
        )
        with patch(FETCH, side_effect=fake_fetch()):
            self.assertEqual(archive_media(archive.pk)["status"], "archived")
        archive.refresh_from_db()
        self.assertEqual(archive.attempts, 2)

    def test_storage_failure_leaves_no_partial_file_and_retries(self):
        archive = self.make_archive()
        with patch(FETCH, side_effect=fake_fetch()), patch(
            "django.core.files.storage.FileSystemStorage._save", side_effect=OSError("disk full")
        ):
            archive_media(archive.pk)
        archive.refresh_from_db()
        self.assertEqual(archive.status, Status.FAILED)
        self.assertEqual(archive.file.name, "")
        self.assertIn("disk full", archive.last_error)

    def test_superseded_attempt_never_deletes_the_shared_final_file(self):
        archive = self.make_archive()

        def steal_claim(*, media_id, dest, max_bytes, access_token=None, api_version=None, deadline_seconds=None):
            WhatsAppMediaArchive.objects.filter(pk=archive.pk).update(attempts=99)
            return fake_fetch()(media_id=media_id, dest=dest, max_bytes=max_bytes)

        with patch(FETCH, side_effect=steal_claim):
            self.assertEqual(archive_media(archive.pk)["status"], "superseded")
        archive.refresh_from_db()
        self.assertEqual(archive.file.name, "")
        stored = [f for _, _, fs in os.walk(self.private_root) for f in fs]
        # The deterministic final file stays (the winning attempt would use the very same
        # path); no .part leftovers. True orphans are an explicit operator reconcile.
        self.assertEqual(len(stored), 1)
        self.assertFalse(stored[0].endswith(".part"))

    def test_total_size_quota_defers_without_burning_attempt(self):
        WhatsAppMediaArchive.objects.create(
            tenant=self.tenant, message=self.make_message(wamid="w.old", media_id="old"),
            wamid="w.old", media_id="old", kind="image", status=Status.ARCHIVED,
            file="x", sha256="a" * 64, size_bytes=500,
        )
        archive = self.make_archive()
        with override_settings(WHATSAPP_MEDIA_MAX_TOTAL_BYTES=400), patch(FETCH) as fetch:
            result = archive_media(archive.pk)
        archive.refresh_from_db()
        self.assertEqual(result["status"], "deferred")
        self.assertEqual((archive.status, archive.attempts), (Status.FAILED, 0))
        fetch.assert_not_called()

    def test_low_free_disk_defers(self):
        archive = self.make_archive()
        with override_settings(WHATSAPP_MEDIA_MIN_FREE_BYTES=10**18), patch(FETCH) as fetch:
            self.assertEqual(archive_media(archive.pk)["status"], "deferred")
        fetch.assert_not_called()

    def test_retention_is_recorded_but_nothing_is_deleted(self):
        archive = self.make_archive()
        with override_settings(WHATSAPP_MEDIA_RETENTION_DAYS_PERSONAL=30), patch(FETCH, side_effect=fake_fetch()):
            archive_media(archive.pk)
        archive.refresh_from_db()
        self.assertAlmostEqual(
            (archive.retention_until - archive.archived_at).days, 30, delta=1
        )
        self.assertTrue(archive.file.storage.exists(archive.file.name))

    def test_default_retention_is_unset(self):
        archive = self.make_archive()
        with patch(FETCH, side_effect=fake_fetch()):
            archive_media(archive.pk)
        archive.refresh_from_db()
        self.assertIsNone(archive.retention_until)

    def test_disabled_flag_is_noop(self):
        archive = self.make_archive()
        with override_settings(WHATSAPP_MEDIA_ARCHIVE_ENABLED=False), patch(FETCH) as fetch:
            self.assertEqual(archive_media(archive.pk)["status"], "disabled")
        fetch.assert_not_called()

    def test_intake_models_untouched(self):
        from apps.reservations.models import DocumentIntakeImage

        archive = self.make_archive()
        with patch(FETCH, side_effect=fake_fetch()):
            archive_media(archive.pk)
        self.assertEqual(DocumentIntakeImage.objects.count(), 0)
