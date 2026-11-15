"""W1: CASCADE deletes must clean files via a durable outbox, never inside the transaction."""

import os
from datetime import timedelta
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.db import transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.integrations.models import (
    IntegrationConfig,
    WhatsAppMediaArchive,
    WhatsAppMediaFileDeletion,
    WhatsAppMessage,
)
from apps.integrations.tests.media_archive_support import ArchiveTestBase
from apps.integrations.whatsapp import media_file_cleanup as mfc
from apps.integrations.whatsapp.media_file_cleanup import cleanup_media_file, sweep_file_deletions
from apps.tenants.models import Tenant

Del = WhatsAppMediaFileDeletion.Status
ENQUEUE = "apps.integrations.whatsapp.media_file_cleanup.enqueue_file_cleanup"


class Boom(Exception):
    pass


def archived(base, *, wamid="w.1", media_id="m1", name=None, message=None) -> WhatsAppMediaArchive:
    """An ARCHIVED row backed by a real file in the (temp) private root."""
    message = message or base.make_message(wamid=wamid, media_id=media_id)
    row = WhatsAppMediaArchive(
        tenant=base.tenant, message=message, wamid=message.wamid, media_id=media_id, kind="image",
        sensitivity="personal_document_candidate", status="pending", sha256="a" * 64, size_bytes=3,
    )
    row.save()  # the CHECK constraint forbids `archived` without a file, so attach it first
    if name is None:
        row.file.save(f"{row.pk}-aaaaaaaaaaaa.jpg", ContentFile(b"abc"), save=False)
    else:
        row.file.name = name
        if not row.file.storage.exists(name):
            row.file.storage.save(name, ContentFile(b"abc"))
    row.status = "archived"
    row.save()
    return row


def on_disk(row_or_name) -> bool:
    name = row_or_name if isinstance(row_or_name, str) else row_or_name.file.name
    return WhatsAppMediaArchive._meta.get_field("file").storage.exists(name)


class FileCleanupTests(ArchiveTestBase):
    def test_cascade_from_message_delete_queues_outbox_and_cleans_after_commit(self):
        row = archived(self)
        name = row.file.name
        with patch(ENQUEUE) as enqueue, self.captureOnCommitCallbacks(execute=True) as callbacks:
            row.message.delete()  # CASCADE: message -> archive
        self.assertFalse(WhatsAppMediaArchive.objects.filter(pk=row.pk).exists())
        outbox = WhatsAppMediaFileDeletion.objects.get(file_name=name)
        self.assertEqual((outbox.status, outbox.archive_id, outbox.tenant_id), (Del.PENDING, row.pk, self.tenant.pk))
        self.assertEqual(len(callbacks), 1)
        enqueue.assert_called_once_with(outbox.pk)
        self.assertTrue(on_disk(name))  # signal itself never unlinks

        self.assertEqual(cleanup_media_file(outbox.pk)["status"], Del.DONE)
        outbox.refresh_from_db()
        self.assertEqual(outbox.status, Del.DONE)
        self.assertIsNotNone(outbox.completed_at)
        self.assertFalse(on_disk(name))

    def test_cascade_from_tenant_delete(self):
        other = Tenant.objects.create(slug="doomed", name="Doomed", default_language="hr")
        integration = IntegrationConfig.objects.create(
            tenant=other, provider=IntegrationConfig.Provider.WHATSAPP, routing_key="doomed-pn", is_active=True
        )
        message = WhatsAppMessage.objects.create(
            tenant=other, integration=integration, wamid="w.doomed", wa_id="1", direction="inbound",
            message_type="image", raw_payload={},
        )
        row = WhatsAppMediaArchive(
            tenant=other, message=message, wamid="w.doomed", media_id="d1", kind="image",
            status="pending", sha256="b" * 64,
        )
        row.save()
        row.file.save(f"{row.pk}-bbbbbbbbbbbb.jpg", ContentFile(b"x"), save=False)
        row.status = "archived"
        row.save()
        name = row.file.name
        tenant_pk = other.pk  # Django clears .pk on the instance after delete()
        with patch(ENQUEUE), self.captureOnCommitCallbacks(execute=True):
            other.delete()
        self.assertFalse(Tenant.objects.filter(pk=tenant_pk).exists())
        outbox = WhatsAppMediaFileDeletion.objects.get(file_name=name)
        self.assertEqual(outbox.tenant_id, tenant_pk)  # outbox has no FK: survives the tenant
        cleanup_media_file(outbox.pk)
        self.assertFalse(on_disk(name))

    def test_queryset_bulk_delete_records_every_file(self):
        rows = [archived(self, wamid=f"w.{i}", media_id=f"m{i}") for i in range(3)]
        with patch(ENQUEUE), self.captureOnCommitCallbacks(execute=True):
            WhatsAppMediaArchive.objects.filter(pk__in=[r.pk for r in rows]).delete()
        self.assertEqual(WhatsAppMediaFileDeletion.objects.filter(status=Del.PENDING).count(), 3)

    def test_row_without_file_creates_no_outbox(self):
        pending = self.make_archive()
        with patch(ENQUEUE) as enqueue, self.captureOnCommitCallbacks(execute=True):
            pending.delete()
        self.assertEqual(WhatsAppMediaFileDeletion.objects.count(), 0)
        enqueue.assert_not_called()

    def test_rollback_discards_outbox_callbacks_and_keeps_the_file(self):
        row = archived(self)
        name = row.file.name
        with patch(ENQUEUE) as enqueue, self.captureOnCommitCallbacks(execute=True) as callbacks:
            with self.assertRaises(Boom):
                with transaction.atomic():
                    row.message.delete()
                    raise Boom
        self.assertTrue(WhatsAppMediaArchive.objects.filter(pk=row.pk).exists())
        self.assertEqual(WhatsAppMediaFileDeletion.objects.count(), 0)
        self.assertEqual(callbacks, [])
        enqueue.assert_not_called()
        self.assertTrue(on_disk(name))
        self.assertEqual(sweep_file_deletions(), {"enqueued": 0})

    def test_duplicate_delete_events_and_duplicate_tasks_are_idempotent(self):
        row = archived(self)
        name = row.file.name
        with patch(ENQUEUE) as enqueue, self.captureOnCommitCallbacks(execute=True):
            mfc.record_file_deletion(WhatsAppMediaArchive, row)
            mfc.record_file_deletion(WhatsAppMediaArchive, row)
        self.assertEqual(WhatsAppMediaFileDeletion.objects.filter(file_name=name).count(), 1)
        self.assertEqual(enqueue.call_count, 1)
        row.delete()  # a third event for the same name (real delete)
        outbox = WhatsAppMediaFileDeletion.objects.get(file_name=name)
        self.assertEqual(cleanup_media_file(outbox.pk)["status"], Del.DONE)
        self.assertEqual(cleanup_media_file(outbox.pk)["status"], Del.DONE)  # second run: no-op
        self.assertFalse(on_disk(name))

    def test_already_missing_file_is_not_an_error(self):
        row = archived(self)
        name = row.file.name
        row.file.storage.delete(name)
        with patch(ENQUEUE), self.captureOnCommitCallbacks(execute=True):
            row.delete()
        outbox = WhatsAppMediaFileDeletion.objects.get(file_name=name)
        self.assertEqual(cleanup_media_file(outbox.pk)["status"], Del.DONE)

    def test_file_still_referenced_by_another_row_is_never_deleted(self):
        first = archived(self, wamid="w.a", media_id="ma")
        second = archived(self, wamid="w.b", media_id="mb", name=first.file.name)  # shares the path
        with patch(ENQUEUE), self.captureOnCommitCallbacks(execute=True):
            first.delete()
        outbox = WhatsAppMediaFileDeletion.objects.get()
        self.assertEqual(cleanup_media_file(outbox.pk)["status"], Del.SKIPPED_IN_USE)
        self.assertTrue(on_disk(second))
        self.assertEqual(cleanup_media_file(outbox.pk)["status"], Del.SKIPPED_IN_USE)  # idempotent

    def test_worker_failure_keeps_outbox_pending_with_backoff_then_recovers(self):
        row = archived(self)
        name = row.file.name
        with patch(ENQUEUE), self.captureOnCommitCallbacks(execute=True):
            row.delete()
        outbox = WhatsAppMediaFileDeletion.objects.get()
        storage = WhatsAppMediaArchive._meta.get_field("file").storage
        with patch.object(type(storage), "delete", side_effect=OSError("storage offline")):
            result = cleanup_media_file(outbox.pk)
        outbox.refresh_from_db()
        self.assertEqual(result["status"], "failed")
        self.assertEqual((outbox.status, outbox.attempts), (Del.PENDING, 1))
        self.assertIn("storage offline", outbox.last_error)
        self.assertGreater(outbox.next_attempt_at, timezone.now())
        self.assertTrue(on_disk(name))
        # duplicate task inside the backoff window does nothing; sweeper ignores it too
        self.assertEqual(cleanup_media_file(outbox.pk)["status"], "not_due")
        self.assertEqual(sweep_file_deletions()["enqueued"], 0)
        # time passes -> sweeper re-enqueues -> worker succeeds
        WhatsAppMediaFileDeletion.objects.filter(pk=outbox.pk).update(next_attempt_at=timezone.now())
        with patch(ENQUEUE) as enqueue:
            self.assertEqual(sweep_file_deletions()["enqueued"], 1)
        enqueue.assert_called_once_with(outbox.pk)
        self.assertEqual(cleanup_media_file(outbox.pk)["status"], Del.DONE)
        self.assertFalse(on_disk(name))

    def test_persistent_failure_ends_dead_not_infinite(self):
        row = archived(self)
        with patch(ENQUEUE), self.captureOnCommitCallbacks(execute=True):
            row.delete()
        outbox = WhatsAppMediaFileDeletion.objects.get()
        storage = WhatsAppMediaArchive._meta.get_field("file").storage
        with patch.object(type(storage), "delete", side_effect=OSError("nope")):
            for _ in range(mfc.MAX_ATTEMPTS + 2):
                WhatsAppMediaFileDeletion.objects.filter(pk=outbox.pk, status=Del.PENDING).update(
                    next_attempt_at=timezone.now()
                )
                cleanup_media_file(outbox.pk)
        outbox.refresh_from_db()
        self.assertEqual((outbox.status, outbox.attempts), (Del.DEAD, mfc.MAX_ATTEMPTS))
        self.assertEqual(sweep_file_deletions()["enqueued"], 0)

    def test_broker_outage_at_commit_leaves_durable_outbox_for_the_sweeper(self):
        row = archived(self)
        name = row.file.name
        with patch(
            "apps.integrations.whatsapp.media_file_cleanup.cleanup_whatsapp_media_file.delay",
            side_effect=RuntimeError("redis down"),
        ), self.captureOnCommitCallbacks(execute=True):
            row.delete()  # must not raise
        outbox = WhatsAppMediaFileDeletion.objects.get()
        self.assertEqual(outbox.status, Del.PENDING)
        self.assertTrue(on_disk(name))
        with patch(
            "apps.integrations.whatsapp.media_file_cleanup.cleanup_whatsapp_media_file.delay",
            side_effect=lambda pk: cleanup_media_file(pk),
        ):
            self.assertEqual(sweep_file_deletions()["enqueued"], 1)
        self.assertFalse(on_disk(name))

    def test_path_outside_the_archive_is_refused(self):
        outside = os.path.join(self.private_root, "outside.txt")
        with open(outside, "w") as fh:
            fh.write("keep me")
        for bad in ("whatsapp_media/../outside.txt", "outside.txt", "/etc/passwd", "whatsapp_media\\..\\x"):
            outbox = WhatsAppMediaFileDeletion.objects.create(file_name=bad)
            self.assertEqual(cleanup_media_file(outbox.pk)["status"], Del.DEAD)
        self.assertTrue(os.path.exists(outside))

    def test_cleanup_works_with_archive_flag_off(self):
        row = archived(self)
        name = row.file.name
        with override_settings(WHATSAPP_MEDIA_ARCHIVE_ENABLED=False), patch(ENQUEUE), self.captureOnCommitCallbacks(
            execute=True
        ):
            row.delete()
            outbox = WhatsAppMediaFileDeletion.objects.get()
            self.assertEqual(cleanup_media_file(outbox.pk)["status"], Del.DONE)
        self.assertFalse(on_disk(name))

    def test_missing_outbox_row_is_harmless(self):
        self.assertEqual(cleanup_media_file(10**9)["status"], "missing")

    def test_outbox_write_failure_aborts_the_deletion(self):
        row = archived(self)
        with patch.object(WhatsAppMediaFileDeletion.objects, "get_or_create", side_effect=RuntimeError("db")):
            with self.assertRaises(RuntimeError):
                with transaction.atomic():  # savepoint: Collector.delete itself uses savepoint=False
                    row.delete()
        self.assertTrue(WhatsAppMediaArchive.objects.filter(pk=row.pk).exists())
        self.assertTrue(on_disk(row))


class RealTransactionTests(TransactionTestCase):
    """Real COMMIT / ROLLBACK semantics (TestCase wraps everything in one transaction)."""

    def setUp(self):
        import shutil
        import tempfile

        root = tempfile.mkdtemp(prefix="stay-private-media-tx-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        ov = override_settings(PRIVATE_MEDIA_ROOT=root)
        ov.enable()
        self.addCleanup(ov.disable)
        self.tenant = Tenant.objects.create(slug="tx-hotel", name="Tx", default_language="hr")
        msg = WhatsAppMessage.objects.create(
            tenant=self.tenant, wamid="w.tx", wa_id="1", direction="inbound", message_type="image", raw_payload={}
        )
        self.row = WhatsAppMediaArchive(
            tenant=self.tenant, message=msg, wamid="w.tx", media_id="tx1", kind="image",
            status="pending", sha256="c" * 64,
        )
        self.row.save()
        self.row.file.save(f"{self.row.pk}-cccccccccccc.jpg", ContentFile(b"x"), save=False)
        self.row.status = "archived"
        self.row.save()
        self.name = self.row.file.name

    def test_real_rollback_never_removes_the_file(self):
        with patch(ENQUEUE) as enqueue:
            with self.assertRaises(Boom):
                with transaction.atomic():
                    WhatsAppMediaArchive.objects.get(pk=self.row.pk).delete()
                    raise Boom
        enqueue.assert_not_called()
        self.assertTrue(WhatsAppMediaArchive.objects.filter(pk=self.row.pk).exists())
        self.assertEqual(WhatsAppMediaFileDeletion.objects.count(), 0)
        self.assertTrue(on_disk(self.name))

    def test_real_commit_enqueues_only_after_commit_and_then_cleans(self):
        calls = []

        def record(outbox_id):
            # at on_commit time the deletion and its outbox row are durably visible
            calls.append((outbox_id, WhatsAppMediaFileDeletion.objects.filter(pk=outbox_id).exists(),
                          WhatsAppMediaArchive.objects.filter(pk=self.row.pk).exists(), on_disk(self.name)))

        with patch(ENQUEUE, side_effect=record):
            WhatsAppMediaArchive.objects.get(pk=self.row.pk).delete()
        self.assertEqual(len(calls), 1)
        outbox_id, outbox_exists, archive_exists, file_exists = calls[0]
        self.assertTrue(outbox_exists)
        self.assertFalse(archive_exists)
        self.assertTrue(file_exists)  # still on disk when the task is merely queued
        self.assertEqual(cleanup_media_file(outbox_id)["status"], Del.DONE)
        self.assertFalse(on_disk(self.name))
