"""PR-2: UIDVALIDITY guard, safe UID search, cursor safety + issue ledger for the LIVE poll."""

import logging
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.communications import guest_email_ingest as ingest
from apps.communications.guest_email_ingest import ingest_parsed_email, poll_tenant_guest_inbox
from apps.communications.guest_imap_protocol import parse_status_numbers, search_new_uids
from apps.communications.models import GuestImapIssue, GuestInboundMessage
from apps.communications.tests.guest_imap_test_support import (
    CODE_A, CODE_B, CURSOR, UIDVALIDITY, FakeLiveImap, ReplayCase, make_eml,
)
from apps.tenants.models import TenantReceptionSettings

CONNECT = "apps.communications.guest_email_ingest._connect_imap"
STAMP = "apps.communications.guest_email_ingest.mark_conversation_ingest"
NOTIFY = "apps.core.tasks.notify_guest_message_inbound.delay"
HANDLERS = (
    "apps.communications.guest_arrival_inbound.maybe_handle_guest_arrival_inbound",
    "apps.communications.guest_invoice_inbound.maybe_handle_guest_invoice_inbound",
    "apps.communications.guest_parking_inbound.maybe_handle_guest_parking_inbound",
)


class Crash(BaseException):
    """The process dies (not an Exception)."""


class PollCase(ReplayCase):
    """Uzorita, cursor 1121, UIDVALIDITY stored, benign texts, push and handlers mocked."""

    def setUp(self):
        super().setUp()
        TenantReceptionSettings.objects.filter(pk=self.settings_row.pk).update(guest_imap_uidvalidity=UIDVALIDITY)
        self.notify = self.start(patch(NOTIFY))
        self.handlers = [self.start(patch(h, return_value=None)) for h in HANDLERS]
        self.stamp = self.start(patch(STAMP))

    def start(self, patcher):
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def poll(self, fake, **kw):
        with patch(CONNECT, return_value=fake):
            return poll_tenant_guest_inbox(self.tenant_fresh(), **kw)

    def msgs(self, *uids, code=CODE_A) -> dict:
        return {u: make_eml(message_id=f"m{u}@x", code=code, text=f"hello {u}") for u in uids}

    def issues(self):
        return {(i.uid, i.kind): i for i in GuestImapIssue.objects.all()}

    def stored_validity(self):
        return TenantReceptionSettings.objects.get(pk=self.settings_row.pk).guest_imap_uidvalidity


# --------------------------------------------------------------------------- UIDVALIDITY


class UidValidityTests(PollCase):
    def test_matching_uidvalidity_polls_normally(self):
        result = self.poll(FakeLiveImap(self.msgs(1122, 1123)))
        self.assertEqual((result.ingested, result.errors, result.blocked_reason), (2, 0, ""))
        self.assertEqual((self.cursor(), result.max_uid), (1123, 1123))
        self.stamp.assert_called_once()
        self.assertEqual(GuestImapIssue.objects.count(), 0)

    def test_changed_uidvalidity_aborts_and_touches_nothing(self):
        fake = FakeLiveImap(self.msgs(1122, 1123), uidvalidity=UIDVALIDITY + 7)
        with self.assertLogs("apps.communications.guest_imap_issues", level="ERROR") as logs:
            result = self.poll(fake)
        self.assertEqual(result.blocked_reason, "uidvalidity_changed")
        self.assertEqual((result.ingested, result.errors), (0, 1))
        self.assertEqual((self.cursor(), self.stored_validity()), (CURSOR, UIDVALIDITY))  # never reset
        self.assertEqual(GuestInboundMessage.objects.count(), 0)
        self.assertEqual([e[0] for e in fake.log], ["STATUS", "LOGOUT"])  # no SELECT/SEARCH/FETCH
        self.stamp.assert_not_called()  # a refused poll must not look like a fresh one in system status
        self.assertIn("REFUSED", logs.output[0])
        self.assertIn("uidvalidity_changed", logs.output[0])

    def test_refusal_is_recorded_once_and_counted_on_every_poll(self):
        fake = FakeLiveImap(self.msgs(1122), uidvalidity=UIDVALIDITY + 7)
        for _ in range(3):
            with self.assertLogs("apps.communications.guest_imap_issues", level="ERROR"):
                self.poll(fake)
        issue = GuestImapIssue.objects.get()
        self.assertEqual((issue.uid, issue.kind, issue.status, issue.attempts, issue.uidvalidity),
                         (0, "uidvalidity_changed", "open", 3, UIDVALIDITY + 7))

    def test_polling_resumes_and_the_issue_resolves_when_the_stored_value_matches_again(self):
        with self.assertLogs("apps.communications.guest_imap_issues", level="ERROR"):
            self.poll(FakeLiveImap(self.msgs(1122), uidvalidity=UIDVALIDITY + 7))
        result = self.poll(FakeLiveImap(self.msgs(1122)))
        self.assertEqual((result.blocked_reason, result.ingested), ("", 1))
        self.assertEqual(GuestImapIssue.objects.get().status, "resolved")

    def test_unavailable_uidvalidity_aborts(self):
        with self.assertLogs("apps.communications.guest_imap_issues", level="ERROR"):
            result = self.poll(FakeLiveImap(self.msgs(1122), status_ok=False))
        self.assertEqual(result.blocked_reason, "uidvalidity_unavailable")
        self.assertEqual((self.cursor(), GuestInboundMessage.objects.count()), (CURSOR, 0))

    def test_existing_cursor_without_stored_uidvalidity_is_refused_never_adopted(self):
        TenantReceptionSettings.objects.filter(pk=self.settings_row.pk).update(guest_imap_uidvalidity=None)
        with self.assertLogs("apps.communications.guest_imap_issues", level="ERROR"):
            result = self.poll(FakeLiveImap(self.msgs(1122)))
        self.assertEqual(result.blocked_reason, "uidvalidity_uninitialized")
        self.assertIsNone(self.stored_validity())  # not auto-accepted: it could be a replaced mailbox
        self.assertEqual((self.cursor(), GuestInboundMessage.objects.count()), (CURSOR, 0))

    def test_tenant_without_cursor_adopts_the_uidvalidity_on_first_poll(self):
        TenantReceptionSettings.objects.filter(pk=self.settings_row.pk).update(
            guest_imap_uidvalidity=None, guest_imap_last_uid=0)
        result = self.poll(FakeLiveImap(self.msgs(1, 2), uidnext=3))
        self.assertEqual((result.blocked_reason, result.ingested), ("", 2))
        self.assertEqual(self.stored_validity(), UIDVALIDITY)

    def test_cursor_beyond_the_mailbox_is_refused_even_if_uidvalidity_matches(self):
        with self.assertLogs("apps.communications.guest_imap_issues", level="ERROR"):
            result = self.poll(FakeLiveImap(self.msgs(1, 2), uidnext=600))
        self.assertEqual(result.blocked_reason, "cursor_ahead_of_mailbox")
        self.assertEqual((self.cursor(), GuestInboundMessage.objects.count()), (CURSOR, 0))

    def test_since_uid_override_is_guarded_too(self):
        with self.assertLogs("apps.communications.guest_imap_issues", level="ERROR"):
            result = self.poll(FakeLiveImap(self.msgs(1122), uidvalidity=UIDVALIDITY + 1), since_uid=0)
        self.assertEqual(result.blocked_reason, "uidvalidity_changed")
        self.assertEqual(GuestInboundMessage.objects.count(), 0)

    def test_blocked_poll_releases_the_lock(self):
        from apps.communications.tests.guest_imap_test_support import try_lock_from_thread
        with self.assertLogs("apps.communications.guest_imap_issues", level="ERROR"):
            self.poll(FakeLiveImap(self.msgs(1122), uidvalidity=UIDVALIDITY + 1))
        self.assertTrue(try_lock_from_thread(self.tenant.pk))

    def test_ledger_failure_while_refusing_does_not_make_the_poll_proceed(self):
        with patch("apps.communications.guest_imap_issues.record_mailbox_issue", side_effect=RuntimeError("db")), \
                self.assertLogs("apps.communications.guest_imap_issues", level="ERROR"):
            result = self.poll(FakeLiveImap(self.msgs(1122), uidvalidity=UIDVALIDITY + 1))
        self.assertEqual(result.blocked_reason, "uidvalidity_changed")
        self.assertEqual(GuestInboundMessage.objects.count(), 0)


# ------------------------------------------------------------------------------ UID search


class SearchUnitTests(SimpleTestCase):
    class C:
        def __init__(self, reply, typ="OK"):
            self.reply, self.typ, self.queries = reply, typ, []

        def uid(self, cmd, charset, query):
            self.queries.append(query)
            return self.typ, [self.reply]

    def test_status_parsing(self):
        self.assertEqual(parse_status_numbers([b"INBOX (UIDVALIDITY 1775254578 UIDNEXT 1129)"]),
                         {"UIDVALIDITY": 1775254578, "UIDNEXT": 1129})
        self.assertEqual(parse_status_numbers([None, b"INBOX (UIDNEXT 5)"]), {"UIDNEXT": 5})
        self.assertEqual(parse_status_numbers(None), {})

    def test_never_asks_for_an_open_ended_range(self):
        c = self.C(b"1122 1123")
        self.assertEqual(search_new_uids(c, after_uid=1121, uidnext=1124), [1122, 1123])
        self.assertEqual(c.queries, ["UID 1122:1123"])
        self.assertNotIn("*", c.queries[0])

    def test_nothing_newer_means_no_search_at_all(self):
        c = self.C(b"")
        self.assertEqual(search_new_uids(c, after_uid=1128, uidnext=1129), [])
        self.assertEqual(search_new_uids(c, after_uid=1500, uidnext=1129), [])  # cursor beyond the mailbox
        self.assertEqual(c.queries, [])

    def test_a_server_that_returns_old_or_future_uids_cannot_leak_them_in(self):
        c = self.C(b"5 1121 1122 1123 9999")
        self.assertEqual(search_new_uids(c, after_uid=1121, uidnext=1124), [1122, 1123])

    def test_rejected_search_raises(self):
        with self.assertRaises(RuntimeError):
            search_new_uids(self.C(b"", typ="NO"), after_uid=1, uidnext=10)


class SafeSearchPollTests(PollCase):
    def test_caught_up_cursor_fetches_nothing_and_searches_nothing(self):
        TenantReceptionSettings.objects.filter(pk=self.settings_row.pk).update(guest_imap_last_uid=1128)
        fake = FakeLiveImap(self.msgs(1127, 1128))
        result = self.poll(fake)
        self.assertEqual((result.ingested, result.errors), (0, 0))
        self.assertEqual((fake.searches(), fake.fetched_uids()), ([], []))  # the old code re-fetched UID 1128 every poll

    def test_poll_never_uses_star_and_only_fetches_uids_above_the_cursor(self):
        fake = FakeLiveImap(self.msgs(1119, 1120, 1121, 1122, 1123), ignore_search_bounds=True)
        self.poll(fake)
        self.assertTrue(all("*" not in q for q in fake.searches()))
        self.assertEqual(fake.fetched_uids(), [1122, 1123])

    def test_server_semantics_of_n_star_would_redeliver_the_last_message(self):
        """Documents why: with real IMAP semantics ``UID 1129:*`` returns the highest UID."""
        fake = FakeLiveImap(self.msgs(1127, 1128))
        self.assertEqual(fake.uid("SEARCH", None, "UID 1129:*")[1], [b"1128"])
        self.assertEqual(fake.uid("SEARCH", None, "UID 5000:*")[1], [b"1128"])


# ---------------------------------------------------------------------------- cursor safety


class CursorSafetyTests(PollCase):
    def test_failed_fetch_holds_the_cursor_but_never_blocks_later_messages(self):
        fake = FakeLiveImap(self.msgs(1122, 1123, 1124), fetch_no=(1123,))
        r1 = self.poll(fake)
        self.assertEqual((r1.ingested, r1.errors, r1.issues_open), (2, 1, 1))
        self.assertEqual((self.cursor(), r1.max_uid), (1122, 1122))  # NOT moved over 1123
        self.assertEqual(set(GuestInboundMessage.objects.values_list("message_id", flat=True)), {"m1122@x", "m1124@x"})
        issue = GuestImapIssue.objects.get()
        self.assertEqual((issue.uid, issue.kind, issue.status, issue.attempts, issue.error_type),
                         (1123, "fetch_failed", "open", 1, "EmptyResponse"))

        r2 = self.poll(FakeLiveImap(self.msgs(1122, 1123, 1124)))  # the problem went away
        self.assertEqual((r2.ingested, r2.errors), (1, 0))
        self.assertEqual((self.cursor(), GuestInboundMessage.objects.count()), (1124, 3))
        self.assertEqual(GuestImapIssue.objects.get().status, "resolved")
        # dedupe: automation ran exactly once per message although 1124 was fetched twice
        self.assertEqual(self.notify.call_count, 3)
        for h in self.handlers:
            self.assertEqual(h.call_count, 3)

    def test_transport_exception_on_fetch_is_a_ledger_entry_not_a_crash(self):
        r = self.poll(FakeLiveImap(self.msgs(1122, 1123), fetch_raise=(1122,)))
        self.assertEqual((r.ingested, self.cursor()), (1, CURSOR))
        self.assertEqual(self.issues()[(1122, "fetch_failed")].error_type, "abort")

    def test_empty_fetch_reply_is_handled(self):
        r = self.poll(FakeLiveImap(self.msgs(1122, 1123), fetch_empty=(1122,)))
        self.assertEqual((r.ingested, r.issues_open), (1, 1))

    def test_persistent_failure_is_quarantined_after_the_limit_and_releases_the_cursor(self):
        limit = 10
        for n in range(1, limit + 1):
            r = self.poll(FakeLiveImap(self.msgs(1122, 1123, 1124), fetch_no=(1123,)))
            expected = CURSOR + 1 if n < limit else 1124  # held until the last attempt
            self.assertEqual(self.cursor(), expected, f"attempt {n}")
        issue = GuestImapIssue.objects.get()
        self.assertEqual((issue.status, issue.attempts), ("quarantined", limit))
        self.assertEqual(r.quarantined, 1)
        self.assertEqual(GuestInboundMessage.objects.count(), 2)  # 1122 and 1124 stored exactly once
        self.assertEqual(self.notify.call_count, 2)
        before = GuestImapIssue.objects.get().attempts
        self.poll(FakeLiveImap(self.msgs(1122, 1123, 1124), fetch_no=(1123,)))  # nothing new: no more attempts
        self.assertEqual(GuestImapIssue.objects.get().attempts, before)

    def test_parse_failure_is_recorded_without_content_and_quarantined_quickly(self):
        real = ingest.parse_guest_email_bytes
        records = []

        class H(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = H(level=logging.DEBUG)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)

        def boom(raw):
            if b"SECRET-BODY-123" in raw:
                raise ValueError("SECRET-BODY-123 exploded")
            return real(raw)

        msgs = {1122: make_eml(message_id="bad@x", text="SECRET-BODY-123"), 1123: make_eml(message_id="ok@x")}
        with patch.object(ingest, "parse_guest_email_bytes", side_effect=boom):
            self.poll(FakeLiveImap(msgs))
            self.assertEqual((self.cursor(), GuestImapIssue.objects.get().status), (CURSOR, "open"))
            self.poll(FakeLiveImap(msgs))
        issue = GuestImapIssue.objects.get()
        self.assertEqual((issue.kind, issue.status, issue.attempts, issue.error_type), ("parse_failed", "quarantined", 2, "ValueError"))
        self.assertEqual(self.cursor(), 1123)
        self.assertEqual(GuestInboundMessage.objects.count(), 1)  # the good one, stored once
        blob = " ".join(f"{r.getMessage()} {r.__dict__}" for r in records) + repr(list(GuestImapIssue.objects.values()))
        self.assertNotIn("SECRET-BODY-123", blob)

    def test_malformed_mime_never_crashes_the_poll(self):
        junk = b"From: x@guest.booking.com\r\nContent-Type: multipart/mixed; boundary=\r\n\r\n\x00\xff\xfe--garbage--\r\n"
        weird = (b"From: x@guest.booking.com\r\nMessage-ID: <w@x>\r\nContent-Type: text/plain; charset=no-such-charset\r\n"
                 b"Content-Transfer-Encoding: base64\r\n\r\n!!!not base64!!!\r\n")
        r = self.poll(FakeLiveImap({1122: junk, 1123: weird, 1124: make_eml(message_id="ok@x")}))
        self.assertEqual((r.ingested, r.blocked_reason), (1, ""))
        self.assertEqual(self.cursor(), 1124)  # unparseable-but-harmless mail is a legitimate skip

    def test_missing_reservation_holds_the_cursor_until_the_reservation_exists(self):
        msgs = {1122: make_eml(message_id="late@x", code="9999999999"), 1123: make_eml(message_id="ok@x")}
        r1 = self.poll(FakeLiveImap(msgs))
        self.assertEqual((r1.ingested, r1.skipped, r1.errors, r1.issues_open), (1, 1, 0, 1))  # skipped as before
        self.assertEqual(self.cursor(), CURSOR)
        self.assertEqual(self.issues()[(1122, "no_reservation")].status, "open")
        from apps.reservations.models import Reservation
        Reservation.objects.filter(pk=self.res_b.pk).update(booking_code="9999999999")  # the booking is imported
        r2 = self.poll(FakeLiveImap(msgs))
        self.assertEqual((r2.ingested, self.cursor()), (1, 1123))
        self.assertEqual(GuestInboundMessage.objects.filter(message_id="late@x").count(), 1)
        self.assertEqual(self.issues()[(1122, "no_reservation")].status, "resolved")

    def test_missing_reservation_is_quarantined_after_the_limit_and_not_lost(self):
        msgs = {1122: make_eml(message_id="never@x", code="9999999999"), 1123: make_eml(message_id="ok@x")}
        for _ in range(10):
            self.poll(FakeLiveImap(msgs))
        self.assertEqual(self.cursor(), 1123)
        issue = GuestImapIssue.objects.get()
        self.assertEqual((issue.uid, issue.kind, issue.status), (1122, "no_reservation", "quarantined"))

    def test_ledger_write_failure_stops_the_poll_without_moving_the_cursor_further(self):
        fake = FakeLiveImap(self.msgs(1122, 1123, 1124), fetch_no=(1123,))
        with patch.object(ingest, "record_failure", side_effect=RuntimeError("ledger down")):
            r = self.poll(fake)
        self.assertEqual(self.cursor(), 1122)  # only the verified prefix; 1123 was not recorded, so never passed
        self.assertGreaterEqual(r.errors, 1)
        self.assertEqual(GuestImapIssue.objects.count(), 0)

    def test_store_failure_of_one_message_does_not_stop_the_others(self):
        calls = {"n": 0}

        def flaky(tenant, parsed, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("db hiccup SECRET")
            return ingest_parsed_email(tenant, parsed, **kw)

        with patch.object(ingest, "ingest_parsed_email", side_effect=flaky):
            r = self.poll(FakeLiveImap(self.msgs(1122, 1123, 1124)))
        self.assertEqual((r.ingested, r.errors, r.issues_open), (2, 1, 1))
        self.assertEqual(self.cursor(), 1122)
        self.assertEqual(self.issues()[(1123, "ingest_failed")].error_type, "RuntimeError")
        r2 = self.poll(FakeLiveImap(self.msgs(1122, 1123, 1124)))
        self.assertEqual((r2.ingested, self.cursor(), GuestInboundMessage.objects.count()), (1, 1124, 3))

    def test_process_killed_mid_batch_resumes_without_duplicates_or_gaps(self):
        calls = {"n": 0}

        def die_on_third(tenant, parsed, **kw):
            calls["n"] += 1
            if calls["n"] == 3:
                raise Crash()
            return ingest_parsed_email(tenant, parsed, **kw)

        with patch.object(ingest, "ingest_parsed_email", side_effect=die_on_third):
            with self.assertRaises(Crash):
                self.poll(FakeLiveImap(self.msgs(1122, 1123, 1124, 1125)))
        self.assertEqual(self.cursor(), 1123)  # durable per message: the first two are safe
        self.assertEqual(GuestInboundMessage.objects.count(), 2)
        r = self.poll(FakeLiveImap(self.msgs(1122, 1123, 1124, 1125)))
        self.assertEqual((r.ingested, self.cursor(), GuestInboundMessage.objects.count()), (2, 1125, 4))
        self.assertEqual(self.notify.call_count, 4)  # once per message

    def test_repolling_is_idempotent(self):
        fake_args = self.msgs(1122, 1123)
        self.poll(FakeLiveImap(fake_args))
        again = self.poll(FakeLiveImap(fake_args), since_uid=CURSOR)  # forced re-read of the same UIDs
        self.assertEqual((again.ingested, again.skipped), (0, 2))
        self.assertEqual((GuestInboundMessage.objects.count(), self.notify.call_count, self.cursor()), (2, 2, 1123))

    def test_a_large_backlog_is_worked_off_in_bounded_batches(self):
        n = ingest.POLL_BATCH_LIMIT + 30
        msgs = {1121 + i: make_eml(message_id=f"b{i}@x") for i in range(1, n + 1)}
        fake = FakeLiveImap(msgs)
        r1 = self.poll(fake)
        self.assertEqual((r1.ingested, self.cursor()), (ingest.POLL_BATCH_LIMIT, 1121 + ingest.POLL_BATCH_LIMIT))
        r2 = self.poll(FakeLiveImap(msgs))
        self.assertEqual((r2.ingested, self.cursor()), (30, 1121 + n))


# ------------------------------------------------------- existing business logic unchanged


class BusinessLogicUnchangedTests(PollCase):
    def test_counters_skips_and_cursor_for_a_mixed_batch_match_the_previous_behaviour(self):
        msgs = {
            1122: make_eml(message_id="g1@x"),
            1123: make_eml(message_id="own@x", sender="room_reservations@uzorita.hr"),
            1124: make_eml(message_id="other@x", sender="someone@example.com"),
            1125: b"From: x@guest.booking.com\r\nMessage-ID: <nc@x>\r\n\r\nno booking number\r\n",
            1126: make_eml(message_id="g2@x", code=CODE_B),
        }
        r = self.poll(FakeLiveImap(msgs))
        self.assertEqual((r.ingested, r.skipped, r.errors), (2, 3, 0))
        self.assertEqual((self.cursor(), r.max_uid), (1126, 1126))
        self.assertEqual(GuestImapIssue.objects.count(), 0)  # legitimate skips are not problems

    def test_stored_rows_and_automation_are_exactly_what_ingest_parsed_email_does(self):
        self.poll(FakeLiveImap({1122: make_eml(message_id="g1@x", text="Hello", date="Thu, 04 Jun 2026 17:00:49 +0200")}))
        row = GuestInboundMessage.objects.get()
        self.assertEqual((row.channel, row.reservation_id, row.message_id, row.body_text), ("email", self.res_a.pk, "g1@x", "Hello"))
        self.assertEqual(row.received_at.isoformat(), "2026-06-04T15:00:49+00:00")
        self.notify.assert_called_once()  # live automation still runs for new mail (default side_effects=True)
        for h in self.handlers:
            h.assert_called_once()

    def test_no_notify_still_only_silences_push(self):
        self.poll(FakeLiveImap(self.msgs(1122)), notify=False)
        self.notify.assert_not_called()
        for h in self.handlers:
            h.assert_called_once()

    def test_poll_still_opens_the_mailbox_read_write_with_rfc822_like_before(self):
        fake = FakeLiveImap(self.msgs(1122))
        self.poll(fake)
        self.assertIn(("SELECT", False), fake.log)
