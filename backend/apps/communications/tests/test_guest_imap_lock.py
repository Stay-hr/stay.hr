"""Single-flight: one worker per tenant mailbox, shared by Celery poll, CLI poll and replay."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase

from apps.communications import guest_imap_lock
from apps.communications.email_ingest_tasks import poll_guest_email_inbox
from apps.communications.guest_email_ingest import poll_tenant_guest_inbox
from apps.communications.guest_email_replay import ReplayLocked, replay_tenant_guest_inbox
from apps.communications.guest_imap_lock import guest_imap_single_flight
from apps.communications.models import GuestInboundMessage
from apps.communications.tests.guest_imap_test_support import (
    CURSOR, FakeImap, ReplayTxCase, hold_guest_imap_lock, make_eml, run_in_thread, try_lock_from_thread,
)

INGEST_CONNECT = "apps.communications.guest_email_ingest._connect_imap"
INGEST_STAMP = "apps.communications.guest_email_ingest.mark_conversation_ingest"
D = datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc)


def live_client():
    client = MagicMock()
    client.uid.return_value = ("OK", [b""])
    return client


class LockPrimitiveTests(TestCase):
    def test_second_connection_cannot_take_a_held_lock_and_can_after_release(self):
        with guest_imap_single_flight(41) as ok:
            self.assertTrue(ok)
            self.assertFalse(try_lock_from_thread(41))
        self.assertTrue(try_lock_from_thread(41))

    def test_lock_is_released_when_the_guarded_block_raises(self):
        with self.assertRaises(RuntimeError):
            with guest_imap_single_flight(42):
                raise RuntimeError("poll blew up")
        self.assertTrue(try_lock_from_thread(42))

    def test_tenants_do_not_block_each_other(self):
        with guest_imap_single_flight(43):
            self.assertTrue(try_lock_from_thread(44))

    def test_lock_disappears_with_the_connection_so_a_crash_cannot_wedge_it(self):
        import threading
        from django.db import connection
        done = threading.Event()

        def crashed_worker():
            with connection.cursor() as c:
                c.execute("SELECT pg_try_advisory_lock(%s, %s)", (guest_imap_lock.LOCK_NAMESPACE, 45))
            connection.close()  # process "dies" without unlocking
            done.set()

        threading.Thread(target=crashed_worker, daemon=True).start()
        self.assertTrue(done.wait(15))
        self.assertTrue(try_lock_from_thread(45))

    def test_unlock_failure_is_swallowed(self):
        """If the connection is already gone at release time, Postgres freed the lock: no raise."""
        locker = MagicMock()
        locker.__enter__.return_value.fetchone.return_value = (True,)
        conn = MagicMock(vendor="postgresql")
        conn.cursor.side_effect = [locker, RuntimeError("connection already closed")]
        with patch.object(guest_imap_lock, "connection", conn):
            with guest_imap_single_flight(46) as ok:
                self.assertTrue(ok)
        self.assertEqual(conn.cursor.call_count, 2)  # lock + (failed) unlock were both attempted


class NonPostgresTests(SimpleTestCase):
    def test_is_a_noop_that_always_grants_on_other_backends(self):
        fake_conn = MagicMock(vendor="sqlite")
        with patch.object(guest_imap_lock, "connection", fake_conn):
            with guest_imap_single_flight(1) as ok:
                self.assertTrue(ok)
        fake_conn.cursor.assert_not_called()


class SingleFlightIntegrationTests(ReplayTxCase):
    def replay_apply(self, tenant, imap):
        return replay_tenant_guest_inbox(
            tenant, from_uid=1122, to_uid=1122, apply=True, expect_cursor=CURSOR, connect=lambda s: imap)

    def one_msg(self):
        return FakeImap({1122: (make_eml(message_id="p1@x"), D)})

    # -- live polling yields to a running replay / other poll
    def test_poll_skips_when_the_lock_is_held_and_touches_neither_imap_nor_stamps(self):
        with hold_guest_imap_lock(self.tenant.pk), patch(INGEST_CONNECT) as connect, patch(INGEST_STAMP) as stamp:
            result = poll_tenant_guest_inbox(self.tenant_fresh())
        self.assertTrue(result.skipped_locked)
        self.assertEqual((result.ingested, result.errors), (0, 0))
        connect.assert_not_called()
        stamp.assert_not_called()  # a skipped poll must not look like a successful poll in system status

    def test_poll_runs_normally_when_free_and_releases_the_lock(self):
        with patch(INGEST_CONNECT, return_value=live_client()) as connect:
            result = poll_tenant_guest_inbox(self.tenant_fresh())
        self.assertFalse(result.skipped_locked)
        connect.assert_called_once()
        self.assertTrue(try_lock_from_thread(self.tenant.pk))

    def test_celery_task_reports_locked_tenants(self):
        with hold_guest_imap_lock(self.tenant.pk), patch(INGEST_CONNECT) as connect:
            summary = poll_guest_email_inbox(tenant_slug="uzorita")
        self.assertEqual(summary, {"tenants": 1, "ingested": 0, "skipped": 0, "errors": 0, "locked": 1})
        connect.assert_not_called()
        with patch(INGEST_CONNECT, return_value=live_client()):
            self.assertEqual(poll_guest_email_inbox(tenant_slug="uzorita")["locked"], 0)

    def test_disabled_tenant_does_not_take_the_lock_at_all(self):
        self.settings_row.guest_imap_enabled = False
        self.settings_row.save()
        with patch.object(guest_imap_lock, "guest_imap_single_flight", side_effect=AssertionError), \
                patch("apps.communications.guest_email_ingest.guest_imap_single_flight", side_effect=AssertionError):
            self.assertFalse(poll_tenant_guest_inbox(self.tenant_fresh()).skipped_locked)

    # -- parallel execution, both directions
    def test_a_running_poll_blocks_replay_a_second_poll_and_the_celery_task(self):
        outcomes = {}

        def during_poll(settings_obj):
            tenant = self.tenant_fresh()
            outcomes["replay"] = run_in_thread(lambda: self.replay_apply(tenant, self.one_msg()))
            outcomes["poll"] = run_in_thread(lambda: poll_tenant_guest_inbox(tenant))
            outcomes["celery"] = run_in_thread(lambda: poll_guest_email_inbox(tenant_slug="uzorita"))
            return live_client()

        with patch(INGEST_CONNECT, side_effect=during_poll):
            first = poll_tenant_guest_inbox(self.tenant_fresh())
        self.assertFalse(first.skipped_locked)
        self.assertIsInstance(outcomes["replay"].get("error"), ReplayLocked)
        self.assertTrue(outcomes["poll"]["value"].skipped_locked)
        self.assertEqual(outcomes["celery"]["value"]["locked"], 1)
        self.assertEqual((GuestInboundMessage.objects.count(), self.cursor()), (0, CURSOR))
        # once the poll is over, the replay can run
        report = self.replay_apply(self.tenant_fresh(), self.one_msg())
        self.assertTrue(report.applied)

    def test_a_running_replay_blocks_polls_the_celery_task_and_a_second_replay(self):
        outcomes = {}
        imap = self.one_msg()

        def probe(cmd):
            if "done" in outcomes:
                return
            outcomes["done"] = True
            tenant = self.tenant_fresh()
            outcomes["lockable"] = try_lock_from_thread(self.tenant.pk)
            outcomes["poll"] = run_in_thread(lambda: poll_tenant_guest_inbox(tenant))
            outcomes["celery"] = run_in_thread(lambda: poll_guest_email_inbox(tenant_slug="uzorita"))
            outcomes["replay2"] = run_in_thread(lambda: self.replay_apply(tenant, self.one_msg()))

        imap.on_uid = probe
        with patch(INGEST_CONNECT) as live_connect:
            report = self.replay_apply(self.tenant_fresh(), imap)
        self.assertTrue(report.applied)
        self.assertFalse(outcomes["lockable"])
        self.assertTrue(outcomes["poll"]["value"].skipped_locked)
        self.assertEqual(outcomes["celery"]["value"]["locked"], 1)
        self.assertIsInstance(outcomes["replay2"].get("error"), ReplayLocked)
        live_connect.assert_not_called()  # no live IMAP session ran beside the replay
        # exactly one replay won: one row, cursor moved once
        self.assertEqual((GuestInboundMessage.objects.count(), self.cursor()), (1, 1122))

    def test_lock_is_released_when_the_replay_fails_midway(self):
        def boom(settings_obj):
            raise OSError("imap unreachable")

        with self.assertRaises(OSError):
            replay_tenant_guest_inbox(self.tenant_fresh(), from_uid=1122, to_uid=1122, apply=True,
                                      expect_cursor=CURSOR, connect=boom)
        self.assertTrue(try_lock_from_thread(self.tenant.pk))
        self.assertEqual(self.cursor(), CURSOR)
