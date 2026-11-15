"""B1/B2/W2/W4: retry gating, bounded attempts, crash recovery, retry-mechanism coherence."""

import errno
import os
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.test import override_settings
from django.utils import timezone

from apps.integrations.models import WhatsAppMediaArchive
from apps.integrations.tests.media_archive_support import ArchiveTestBase, fake_fetch
from apps.integrations.whatsapp import media_archive as ma
from apps.integrations.whatsapp import media_file_cleanup as mfc
from apps.integrations.whatsapp.media_archive import archive_media, sweep_media_archive
from apps.integrations.whatsapp.media_archive_tasks import archive_whatsapp_media
from apps.integrations.whatsapp.media_download import WhatsAppMediaError

FETCH = "apps.integrations.whatsapp.media_archive.fetch_whatsapp_media_to_file"
Status = WhatsAppMediaArchive.Status


class Crash(BaseException):
    """Simulates the process dying (not an Exception: skips every `except Exception`)."""


def _files(root):
    return sorted(os.path.join(d, f) for d, _, fs in os.walk(root) for f in fs)


def _make_due(archive):
    WhatsAppMediaArchive.objects.filter(pk=archive.pk).update(next_retry_at=timezone.now() - timedelta(seconds=1))


class ClaimGatingTests(ArchiveTestBase):
    def test_duplicate_task_does_not_shorten_backoff_or_burn_attempts(self):
        archive = self.make_archive()
        with patch(FETCH, side_effect=WhatsAppMediaError("503", status_code=503)) as fetch:
            self.assertEqual(archive_media(archive.pk)["status"], "failed")
            # redelivered / sweeper-duplicated copies arriving before the backoff elapsed
            for _ in range(5):
                self.assertEqual(archive_media(archive.pk)["status"], "not_due")
        archive.refresh_from_db()
        self.assertEqual((archive.attempts, fetch.call_count), (1, 1))

    def test_failed_row_is_claimed_again_once_due(self):
        archive = self.make_archive()
        with patch(FETCH, side_effect=WhatsAppMediaError("503", status_code=503)):
            archive_media(archive.pk)
        _make_due(archive)
        with patch(FETCH, side_effect=fake_fetch()):
            self.assertEqual(archive_media(archive.pk)["status"], "archived")

    def test_sweeper_is_the_single_scheduler_end_to_end(self):
        archive = self.make_archive()
        with patch(FETCH, side_effect=WhatsAppMediaError("503", status_code=503)):
            archive_media(archive.pk)
        self.enqueue.assert_not_called()
        self.assertEqual(sweep_media_archive()["enqueued"], 0)  # not due yet
        _make_due(archive)
        self.assertEqual(sweep_media_archive()["enqueued"], 1)
        self.enqueue.assert_called_once_with(archive.pk)
        with patch(FETCH, side_effect=fake_fetch()):
            self.assertEqual(archive_media(archive.pk)["status"], "archived")

    def test_poison_row_that_keeps_killing_workers_becomes_permanent(self):
        archive = self.make_archive()  # MAX_ATTEMPTS=3
        crashes = 0
        with patch(FETCH, side_effect=Crash()) as fetch:
            for _ in range(10):  # far more cycles than MAX_ATTEMPTS
                WhatsAppMediaArchive.objects.filter(pk=archive.pk, status=Status.DOWNLOADING).update(
                    lease_until=timezone.now() - timedelta(seconds=1)
                )
                try:
                    result = archive_media(archive.pk)
                except Crash:
                    crashes += 1
                    continue
                self.assertEqual(result["status"], "permanent_failed")
                break
        archive.refresh_from_db()
        self.assertEqual(archive.status, Status.PERMANENT_FAILED)
        self.assertEqual((crashes, fetch.call_count), (3, 3))  # exactly MAX_ATTEMPTS claims
        self.assertIn("max attempts", archive.last_error)
        self.assertIsNone(archive.next_retry_at)

    def test_permanent_row_is_ignored_by_sweeper(self):
        archive = self.make_archive(status=Status.PERMANENT_FAILED)
        sweep_media_archive()
        self.enqueue.assert_not_called()
        with patch(FETCH) as fetch:
            archive_media(archive.pk)
        fetch.assert_not_called()


class EnvironmentErrorTests(ArchiveTestBase):
    def _run_with_save_error(self, exc):
        archive = self.make_archive()
        with patch(FETCH, side_effect=fake_fetch()), patch(
            "django.core.files.storage.FileSystemStorage._save", side_effect=exc
        ):
            result = archive_media(archive.pk)
        archive.refresh_from_db()
        return result, archive

    def test_permission_denied_defers_without_burning_an_attempt(self):
        result, archive = self._run_with_save_error(PermissionError(errno.EACCES, "denied"))
        self.assertEqual(result["status"], "deferred")
        self.assertEqual((archive.status, archive.attempts), (Status.FAILED, 0))
        self.assertGreater(archive.next_retry_at, timezone.now() + timedelta(minutes=50))
        self.assertIn("denied", archive.last_error)
        # a duplicate task inside the deferral window is a no-op
        with patch(FETCH) as fetch:
            self.assertEqual(archive_media(archive.pk)["status"], "not_due")
        fetch.assert_not_called()

    def test_disk_full_defers_without_burning_an_attempt(self):
        result, archive = self._run_with_save_error(OSError(errno.ENOSPC, "no space"))
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(archive.attempts, 0)

    def test_other_os_errors_do_count(self):
        result, archive = self._run_with_save_error(OSError(errno.EIO, "io error"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(archive.attempts, 1)

    def test_unwritable_storage_is_caught_before_claiming(self):
        archive = self.make_archive()
        with patch("apps.integrations.whatsapp.media_archive.os.access", return_value=False), patch(FETCH) as fetch:
            result = archive_media(archive.pk)
        archive.refresh_from_db()
        self.assertEqual(result["status"], "deferred")
        self.assertIn("not writable", archive.last_error)
        self.assertEqual((archive.status, archive.attempts), (Status.FAILED, 0))
        fetch.assert_not_called()

    def test_fetch_receives_total_download_deadline(self):
        archive = self.make_archive()
        with patch(FETCH, side_effect=fake_fetch()) as fetch:
            archive_media(archive.pk)
        self.assertEqual(fetch.call_args.kwargs["deadline_seconds"], ma.DOWNLOAD_DEADLINE_SECONDS)


class CrashRecoveryTests(ArchiveTestBase):
    def test_crash_between_file_write_and_db_update_leaves_no_orphan_after_retry(self):
        archive = self.make_archive()
        with patch(FETCH, side_effect=fake_fetch(b"payload")), patch(
            "apps.integrations.whatsapp.media_archive._finalize_archive", side_effect=Crash()
        ):
            with self.assertRaises(Crash):
                archive_media(archive.pk)
        archive.refresh_from_db()
        self.assertEqual(archive.status, Status.DOWNLOADING)
        self.assertEqual(archive.file.name, "")
        self.assertEqual(len(_files(self.private_root)), 1)  # published, but not yet referenced

        WhatsAppMediaArchive.objects.filter(pk=archive.pk).update(
            lease_until=timezone.now() - timedelta(seconds=1)
        )
        self.assertEqual(sweep_media_archive()["enqueued"], 1)
        with patch(FETCH, side_effect=fake_fetch(b"payload")):
            self.assertEqual(archive_media(archive.pk)["status"], "archived")
        archive.refresh_from_db()
        files = _files(self.private_root)
        self.assertEqual(len(files), 1)  # same deterministic path rewritten, no "_AbC123" twin
        self.assertTrue(files[0].endswith(archive.file.name))
        with archive.file.open("rb") as fh:
            self.assertEqual(fh.read(), b"payload")

    def test_failed_publish_removes_its_own_part_file(self):
        archive = self.make_archive()
        with patch(FETCH, side_effect=fake_fetch()), patch(
            "apps.integrations.whatsapp.media_archive.os.replace", side_effect=OSError(errno.EIO, "io")
        ):
            result = archive_media(archive.pk)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(_files(self.private_root), [])

    def test_published_file_is_complete_or_absent_never_partial(self):
        archive = self.make_archive()
        seen = {}

        real_replace = os.replace

        def spy(src, dst):
            seen["part_exists_before"] = os.path.exists(src)
            seen["final_exists_before"] = os.path.exists(dst)
            return real_replace(src, dst)

        with patch(FETCH, side_effect=fake_fetch()), patch(
            "apps.integrations.whatsapp.media_archive.os.replace", side_effect=spy
        ):
            archive_media(archive.pk)
        self.assertTrue(seen["part_exists_before"])
        self.assertFalse(seen["final_exists_before"])  # the final path only ever appears whole

    def test_archived_is_never_recorded_when_published_file_vanished(self):
        archive = self.make_archive()
        with patch(FETCH, side_effect=fake_fetch()), patch(
            "apps.integrations.whatsapp.media_archive._publish_file", return_value="whatsapp_media/personal/x/none.jpg"
        ):
            result = archive_media(archive.pk)
        archive.refresh_from_db()
        self.assertEqual(result["status"], "failed")
        self.assertNotEqual(archive.status, Status.ARCHIVED)
        self.assertIn("FileNotFoundError", archive.last_error)


class RetryMechanismCoherenceTests(ArchiveTestBase):
    """Pins the relationships between every timer/limit that drives retries."""

    def test_timing_contract_ordering(self):
        self.assertLess(ma.DOWNLOAD_DEADLINE_SECONDS, ma.TASK_SOFT_TIME_LIMIT)
        self.assertLess(ma.TASK_SOFT_TIME_LIMIT, ma.TASK_TIME_LIMIT)
        # lease must outlive the hard limit, or a live worker's claim could be reclaimed
        self.assertLess(ma.TASK_TIME_LIMIT, ma.LEASE_SECONDS)
        # the on_commit enqueue must win against the sweeper's lost-enqueue recovery
        self.assertGreater(ma.PENDING_GRACE_SECONDS, ma.SWEEP_INTERVAL_SECONDS)
        # backoff can never be finer than the scheduler that executes it
        self.assertGreaterEqual(ma.BACKOFF_SECONDS[0], ma.SWEEP_INTERVAL_SECONDS)
        self.assertGreaterEqual(ma.DEFER_SECONDS, ma.SWEEP_INTERVAL_SECONDS)

    def test_celery_task_limits_match_contract(self):
        self.assertEqual(archive_whatsapp_media.soft_time_limit, ma.TASK_SOFT_TIME_LIMIT)
        self.assertEqual(archive_whatsapp_media.time_limit, ma.TASK_TIME_LIMIT)

    def test_beat_schedule_matches_sweep_intervals(self):
        beat = settings.CELERY_BEAT_SCHEDULE
        self.assertEqual(beat["whatsapp-media-archive-sweep"]["schedule"], ma.SWEEP_INTERVAL_SECONDS)
        self.assertEqual(beat["whatsapp-media-file-cleanup-sweep"]["schedule"], mfc.SWEEP_INTERVAL_SECONDS)
        for entry in ("whatsapp-media-archive-sweep", "whatsapp-media-file-cleanup-sweep"):
            self.assertIn(beat[entry]["task"], __import__("celery").current_app.tasks.keys())

    def test_no_countdown_or_eta_tasks_remain(self):
        # Redis visibility_timeout (1h default) < our longest backoff (24h): ETA tasks would
        # be redelivered. Retries therefore only exist as DB state + sweeper.
        import inspect

        source = inspect.getsource(ma)
        self.assertNotIn("countdown", source.replace("countdown/ETA", ""))
        self.assertNotIn("apply_async", source)

    def test_only_a_claim_increments_attempts(self):
        archive = self.make_archive(attempts=2, status=Status.FAILED, next_retry_at=timezone.now() + timedelta(hours=1))
        sweep_media_archive()
        archive_media(archive.pk)  # not due
        archive.refresh_from_db()
        self.assertEqual(archive.attempts, 2)

    def test_retry_failed_command_overrides_backoff_but_not_the_attempt_cap(self):
        from io import StringIO

        from django.core.management import call_command

        archive = self.make_archive(attempts=1, status=Status.FAILED, next_retry_at=timezone.now() + timedelta(hours=5))
        with patch(FETCH, side_effect=fake_fetch()):
            call_command("whatsapp_media_archive", "--retry-failed", "--inline", stdout=StringIO())
        self.assertEqual(WhatsAppMediaArchive.objects.get(pk=archive.pk).status, Status.ARCHIVED)

        spent = self.make_archive(
            message=self.make_message(wamid="w.spent", media_id="spent"),
            attempts=3, status=Status.FAILED, next_retry_at=timezone.now() + timedelta(hours=5),
        )
        with patch(FETCH) as fetch:
            call_command("whatsapp_media_archive", "--retry-failed", "--inline", stdout=StringIO())
        fetch.assert_not_called()
        self.assertEqual(WhatsAppMediaArchive.objects.get(pk=spent.pk).status, Status.PERMANENT_FAILED)

    def test_id_option_makes_a_backed_off_row_due(self):
        from io import StringIO

        from django.core.management import call_command

        archive = self.make_archive(attempts=1, status=Status.FAILED, next_retry_at=timezone.now() + timedelta(hours=5))
        with patch(FETCH, side_effect=fake_fetch()):
            call_command("whatsapp_media_archive", "--id", str(archive.pk), stdout=StringIO())
        self.assertEqual(WhatsAppMediaArchive.objects.get(pk=archive.pk).status, Status.ARCHIVED)
