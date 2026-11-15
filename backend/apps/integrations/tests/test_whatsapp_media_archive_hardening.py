"""media_id validation, PRIVATE_MEDIA_ROOT checks, default-off config, logging, reconcile."""

import ast
import importlib.util
import logging
import os
import time
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.checks import Error, Warning
from django.core.management import call_command
from django.test import SimpleTestCase, override_settings

from apps.integrations import checks
from apps.integrations.models import WhatsAppMediaArchive
from apps.integrations.tests.media_archive_support import ArchiveTestBase, fake_fetch
from apps.integrations.whatsapp.media_download import (
    WhatsAppMediaError,
    extract_media_descriptor,
    fetch_whatsapp_media_to_file,
    is_valid_media_id,
)
from apps.integrations.whatsapp.media_reconcile import reconcile_orphans

SETTINGS_DIR = Path(settings.BASE_DIR) / "config" / "settings"


class MediaIdValidationTests(SimpleTestCase):
    def test_accepts_meta_style_ids(self):
        for ok in ("1234567890123456", "abc_DEF-123", "a" * 128):
            self.assertTrue(is_valid_media_id(ok), ok)

    def test_rejects_anything_that_could_alter_the_url(self):
        for bad in ("", "../x", "12/34", "12?x=1", "12#x", "a b", "12\n34", "é1", "a" * 129, "..", "1.2", "%2e%2e"):
            self.assertFalse(is_valid_media_id(bad), repr(bad))

    def test_descriptor_ignores_invalid_ids(self):
        self.assertIsNone(extract_media_descriptor({"type": "image", "image": {"id": "../../etc"}}))
        self.assertIsNotNone(extract_media_descriptor({"type": "image", "image": {"id": "12345"}}))

    @patch("apps.integrations.whatsapp.media_download.httpx.get")
    def test_fetch_rejects_invalid_id_before_any_http_and_is_not_retryable(self, mock_get):
        with self.assertRaises(WhatsAppMediaError) as ctx:
            fetch_whatsapp_media_to_file(media_id="../v1/me", dest=BytesIO(), max_bytes=10, access_token="t")
        self.assertFalse(ctx.exception.retryable)
        mock_get.assert_not_called()

    @patch("apps.integrations.whatsapp.media_download.time.monotonic")
    @patch("apps.integrations.whatsapp.media_download.httpx.stream")
    @patch("apps.integrations.whatsapp.media_download.httpx.get")
    def test_total_deadline_aborts_a_trickling_download(self, mock_get, mock_stream, mono):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"url": "https://x/y", "mime_type": "video/mp4"}

        class Slow:
            status_code = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def iter_bytes(self, chunk_size=None):
                yield b"a"
                yield b"b"

        mock_stream.return_value = Slow()
        mono.side_effect = [0, 1, 999]  # started, first chunk ok, second chunk past deadline
        with self.assertRaisesMessage(WhatsAppMediaError, "deadline"):
            fetch_whatsapp_media_to_file(
                media_id="123", dest=BytesIO(), max_bytes=0, access_token="t", deadline_seconds=60
            )


class PrivateRootCheckTests(SimpleTestCase):
    def test_rejects_private_root_inside_media_root(self):
        with override_settings(MEDIA_ROOT="/srv/media", PRIVATE_MEDIA_ROOT="/srv/media/private"):
            errors = checks.check_private_media_root(None)
        self.assertEqual([e.id for e in errors], ["integrations.E001"])
        self.assertIsInstance(errors[0], Error)

    def test_rejects_media_root_inside_private_root_and_identical_paths(self):
        for media, private in (("/srv/private/media", "/srv/private"), ("/srv/m", "/srv/m")):
            with override_settings(MEDIA_ROOT=media, PRIVATE_MEDIA_ROOT=private):
                self.assertEqual([e.id for e in checks.check_private_media_root(None)], ["integrations.E001"])

    def test_rejects_private_root_inside_static_root(self):
        with override_settings(MEDIA_ROOT="/srv/media", STATIC_ROOT="/srv/static", PRIVATE_MEDIA_ROOT="/srv/static/p"):
            self.assertIn("integrations.E001", [e.id for e in checks.check_private_media_root(None)])

    def test_sibling_directories_are_fine_and_prefix_names_do_not_confuse_it(self):
        with override_settings(MEDIA_ROOT="/app/backend/media", PRIVATE_MEDIA_ROOT="/app/backend/media_private"):
            self.assertEqual(checks.check_private_media_root(None), [])
        with override_settings(MEDIA_ROOT="/app/backend/media", PRIVATE_MEDIA_ROOT="/app/backend/private_media"):
            self.assertEqual(checks.check_private_media_root(None), [])

    def test_symlink_into_media_root_is_detected(self):
        import shutil
        import tempfile

        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        media = Path(base) / "media"
        media.mkdir()
        link = Path(base) / "link"
        link.symlink_to(media)
        with override_settings(MEDIA_ROOT=str(media), PRIVATE_MEDIA_ROOT=str(link / "sub")):
            self.assertEqual([e.id for e in checks.check_private_media_root(None)], ["integrations.E001"])

    def test_warns_when_enabled_but_root_not_writable(self):
        with override_settings(
            MEDIA_ROOT="/srv/media", PRIVATE_MEDIA_ROOT="/nonexistent/private", WHATSAPP_MEDIA_ARCHIVE_ENABLED=True
        ):
            result = checks.check_private_media_root(None)
        self.assertEqual([r.id for r in result], ["integrations.W001"])
        self.assertIsInstance(result[0], Warning)
        with override_settings(
            MEDIA_ROOT="/srv/media", PRIVATE_MEDIA_ROOT="/nonexistent/private", WHATSAPP_MEDIA_ARCHIVE_ENABLED=False
        ):
            self.assertEqual(checks.check_private_media_root(None), [])

    def test_check_is_registered_and_passes_for_the_real_settings(self):
        from django.core import checks as dj_checks

        found = [m for m in dj_checks.run_checks() if m.id.startswith("integrations.E")]
        self.assertEqual(found, [])


class DefaultOffTests(SimpleTestCase):
    SETTINGS_FILES = ("base.py", "production.py", "test.py", "test_postgis.py")

    def test_base_default_is_false_when_environment_is_empty(self):
        import environ

        spec = importlib.util.spec_from_file_location("isolated_base_settings", SETTINGS_DIR / "base.py")
        module = importlib.util.module_from_spec(spec)
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("WHATSAPP_MEDIA_", "PRIVATE_MEDIA"))}
        with patch.dict(os.environ, clean, clear=True), patch.object(environ.Env, "read_env"):
            spec.loader.exec_module(module)
        self.assertIs(module.WHATSAPP_MEDIA_ARCHIVE_ENABLED, False)
        self.assertEqual(module.WHATSAPP_MEDIA_RETENTION_DAYS_PERSONAL, 0)
        self.assertEqual(module.WHATSAPP_MEDIA_RETENTION_DAYS_STANDARD, 0)

    def test_declaration_is_literally_default_false_and_unique_across_settings(self):
        hits = {}
        for name in self.SETTINGS_FILES:
            tree = ast.parse((SETTINGS_DIR / name).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "WHATSAPP_MEDIA_ARCHIVE_ENABLED" for t in node.targets
                ):
                    hits[name] = ast.unparse(node.value)
        # only base.py defines it, and with default=False: no environment overrides it on
        self.assertEqual(hits, {"base.py": "env.bool('WHATSAPP_MEDIA_ARCHIVE_ENABLED', default=False)"})

    def test_example_env_ships_it_disabled_and_compose_does_not_enable_it(self):
        # Repo files are absent inside the runtime image; assert them when running from a checkout.
        root = Path(settings.BASE_DIR).parent
        example = root / ".env.example"
        if example.exists():
            self.assertIn("# WHATSAPP_MEDIA_ARCHIVE_ENABLED=false", example.read_text())
        compose = root / "docker-compose.yml"
        if compose.exists():
            self.assertNotIn("WHATSAPP_MEDIA_ARCHIVE_ENABLED", compose.read_text())

    def test_dockerignore_excludes_private_media(self):
        ignore = Path(settings.BASE_DIR).parent / ".dockerignore"
        if ignore.exists():  # not present inside the runtime image
            self.assertIn("data/private_media", ignore.read_text().split())


class LoggingHygieneTests(SimpleTestCase):
    def test_httpx_request_lines_are_not_logged_at_info(self):
        for name in ("httpx", "httpcore"):
            self.assertGreaterEqual(logging.getLogger(name).getEffectiveLevel(), logging.WARNING)
        self.assertEqual(settings.LOGGING["loggers"]["httpx"]["level"], "WARNING")


class ReconcileTests(ArchiveTestBase):
    def _stray(self, rel: str, age_hours: float = 48):
        path = Path(self.private_root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        old = time.time() - age_hours * 3600
        os.utime(path, (old, old))
        return path

    def test_reports_orphans_and_part_files_but_not_referenced_or_recent_ones(self):
        archive = self.make_archive()
        with patch("apps.integrations.whatsapp.media_archive.fetch_whatsapp_media_to_file", side_effect=fake_fetch()):
            from apps.integrations.whatsapp.media_archive import archive_media

            archive_media(archive.pk)
        archive.refresh_from_db()
        ref = Path(self.private_root) / archive.file.name
        os.utime(ref, (time.time() - 99999, time.time() - 99999))  # old, but referenced
        orphan = self._stray("whatsapp_media/personal/2/2026/11/9-deadbeef0000.jpg")
        part = self._stray("whatsapp_media/personal/2/2026/11/9-deadbeef0000.jpg.ab12cd34.part")
        self._stray("whatsapp_media/personal/2/2026/11/fresh.jpg", age_hours=0)
        report = reconcile_orphans(min_age_hours=24)
        self.assertEqual(report.orphan_files, [orphan.relative_to(self.private_root).as_posix()])
        self.assertEqual(report.part_files, [part.relative_to(self.private_root).as_posix()])
        self.assertTrue(orphan.exists() and part.exists() and ref.exists())  # report only
        self.assertEqual(report.deleted, [])

    def test_delete_only_removes_unreferenced_files(self):
        archive = self.make_archive()
        with patch("apps.integrations.whatsapp.media_archive.fetch_whatsapp_media_to_file", side_effect=fake_fetch()):
            from apps.integrations.whatsapp.media_archive import archive_media

            archive_media(archive.pk)
        archive.refresh_from_db()
        ref = Path(self.private_root) / archive.file.name
        os.utime(ref, (time.time() - 99999, time.time() - 99999))
        orphan = self._stray("whatsapp_media/standard/2/2026/11/7-aaaa.mp4")
        report = reconcile_orphans(min_age_hours=24, delete=True)
        self.assertEqual(len(report.deleted), 1)
        self.assertFalse(orphan.exists())
        self.assertTrue(ref.exists())

    def test_pending_outbox_files_are_in_flight_not_orphans(self):
        from apps.integrations.models import WhatsAppMediaFileDeletion

        stray = self._stray("whatsapp_media/personal/2/2026/11/5-bbbb.jpg")
        WhatsAppMediaFileDeletion.objects.create(file_name="whatsapp_media/personal/2/2026/11/5-bbbb.jpg")
        self.assertEqual(reconcile_orphans(min_age_hours=1).orphan_files, [])
        self.assertTrue(stray.exists())

    def test_reports_archived_rows_whose_file_is_gone(self):
        archive = self.make_archive()
        with patch("apps.integrations.whatsapp.media_archive.fetch_whatsapp_media_to_file", side_effect=fake_fetch()):
            from apps.integrations.whatsapp.media_archive import archive_media

            archive_media(archive.pk)
        archive.refresh_from_db()
        (Path(self.private_root) / archive.file.name).unlink()
        self.assertEqual(reconcile_orphans().missing_files, [archive.pk])

    def test_command_is_report_only_by_default(self):
        orphan = self._stray("whatsapp_media/standard/2/2026/11/3-cccc.mp4")
        out = StringIO()
        call_command("whatsapp_media_archive", "--reconcile-orphans", stdout=out)
        self.assertIn("orphan files (no row references them): 1", out.getvalue())
        self.assertIn("report only", out.getvalue())
        self.assertTrue(orphan.exists())
        call_command("whatsapp_media_archive", "--reconcile-orphans", "--delete", stdout=StringIO())
        self.assertFalse(orphan.exists())

    def test_stats_reports_storage_writability(self):
        out = StringIO()
        call_command("whatsapp_media_archive", "--stats", stdout=out)
        self.assertIn("writable=True", out.getvalue())
        with patch("apps.integrations.management.commands.whatsapp_media_archive.os.access", return_value=False):
            out = StringIO()
            call_command("whatsapp_media_archive", "--stats", stdout=out)
        self.assertIn("writable=False", out.getvalue())
        self.assertIn("chown 1000:1000", out.getvalue())
