"""PR-2: parallel polls against real connections (committed data, real threads)."""

from unittest.mock import patch

from apps.communications.guest_email_ingest import poll_tenant_guest_inbox
from apps.communications.models import GuestImapIssue, GuestInboundMessage
from apps.communications.tests.guest_imap_test_support import (
    CURSOR, UIDVALIDITY, FakeLiveImap, ReplayTxCase, make_eml, run_in_thread,
)
from apps.tenants.models import TenantReceptionSettings

CONNECT = "apps.communications.guest_email_ingest._connect_imap"


class ParallelPollTests(ReplayTxCase):
    def setUp(self):
        super().setUp()
        TenantReceptionSettings.objects.filter(pk=self.settings_row.pk).update(guest_imap_uidvalidity=UIDVALIDITY)
        for target in ("apps.core.tasks.notify_guest_message_inbound.delay",
                       "apps.communications.guest_arrival_inbound.maybe_handle_guest_arrival_inbound",
                       "apps.communications.guest_invoice_inbound.maybe_handle_guest_invoice_inbound",
                       "apps.communications.guest_parking_inbound.maybe_handle_guest_parking_inbound"):
            p = patch(target, return_value=None)
            p.start()
            self.addCleanup(p.stop)

    def test_two_polls_at_once_process_once_and_count_one_attempt(self):
        msgs = {1122: make_eml(message_id="a@x"), 1123: make_eml(message_id="b@x"), 1124: make_eml(message_id="c@x")}
        outcome = {}

        def during_first_poll(uid):
            if uid == 1123 and "second" not in outcome:
                tenant = self.tenant_fresh()
                outcome["second"] = run_in_thread(lambda: poll_tenant_guest_inbox(tenant))

        first_fake = FakeLiveImap(msgs, fetch_no=(1123,))
        first_fake.on_fetch = during_first_poll
        second_fake = FakeLiveImap(msgs)  # would ingest 1123 if it ever got to run
        with patch(CONNECT, side_effect=[first_fake, second_fake]):
            first = poll_tenant_guest_inbox(self.tenant_fresh())
        second = outcome["second"]["value"]
        self.assertTrue(second.skipped_locked)  # refused by the shared lock
        self.assertEqual(second_fake.log, [])  # never even connected a session
        self.assertEqual((first.ingested, first.issues_open), (2, 1))
        self.assertEqual(GuestInboundMessage.objects.count(), 2)
        self.assertEqual(self.cursor(), 1122)
        self.assertEqual(GuestImapIssue.objects.get().attempts, 1)  # not double-counted

    def test_sequential_polls_after_the_race_converge(self):
        msgs = {1122: make_eml(message_id="a@x"), 1123: make_eml(message_id="b@x")}
        with patch(CONNECT, side_effect=lambda s: FakeLiveImap(msgs)):
            results = [poll_tenant_guest_inbox(self.tenant_fresh()) for _ in range(3)]
        self.assertEqual([r.ingested for r in results], [2, 0, 0])
        self.assertEqual((GuestInboundMessage.objects.count(), self.cursor()), (2, 1123))
