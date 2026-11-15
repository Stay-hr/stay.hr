"""Replay: dry-run, apply, cursor protection, rollback, duplicates."""

from datetime import datetime, timedelta, timezone
from io import StringIO
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.db import IntegrityError

from apps.communications import guest_email_replay as replay
from apps.communications.guest_email_ingest import ingest_parsed_email, parse_guest_email_bytes
from apps.communications.guest_email_replay import (
    CursorMismatch, ReplayError, ReplayLocked, format_report, replay_tenant_guest_inbox,
)
from apps.communications.models import GuestInboundMessage, GuestMessage, GuestMessageSource
from apps.communications.tests.guest_imap_test_support import (
    CODE_A, CODE_B, CURSOR, UIDVALIDITY, FakeImap, ReplayCase, hold_guest_imap_lock, make_eml,
)
from apps.reservations.models import ReservationVersion, ReservationVersionScope

CONNECT = "apps.communications.guest_email_replay._connect_imap"
TOUCH = "apps.communications.guest_email_replay.touch_reservation_version"
D1 = datetime(2026, 7, 31, 21, 54, 2, tzinfo=timezone.utc)
D2 = D1 + timedelta(days=30)


def three_messages() -> dict:
    return {
        1122: (make_eml(message_id="m1@x", date="Fri, 31 Jul 2026 23:50:00 +0200"), D1),
        1123: (make_eml(message_id="m2@x", date="Tue, 04 Aug 2026 10:00:00 +0200"), D1 + timedelta(days=4)),
        1124: (make_eml(message_id="m3@x", code=CODE_B, date="Mon, 26 Oct 2026 12:53:34 +0100"), D2),
    }


def snapshot(case) -> dict:
    return {
        "inbound": GuestInboundMessage.objects.count(),
        "canon_msgs": GuestMessage.objects.count(),
        "canon_src": GuestMessageSource.objects.count(),
        "versions": list(ReservationVersion.objects.order_by("pk").values_list("reservation_id", "scope", "version")),
        "cursor": case.cursor(),
    }


class DryRunTests(ReplayCase):
    def test_dry_run_is_the_default_and_writes_nothing(self):
        fake = FakeImap(three_messages())
        before = snapshot(self)
        with patch(TOUCH) as touch:
            report = replay_tenant_guest_inbox(self.tenant_fresh(), from_uid=1122, to_uid=1124, connect=lambda s: fake)
        self.assertEqual(snapshot(self), before)
        touch.assert_not_called()
        self.assertEqual((report.mode, report.applied, report.cursor_after), ("dry-run", False, None))
        self.assertEqual([i.outcome for i in report.items], ["would_insert"] * 3)
        self.assertEqual(report.uidvalidity, UIDVALIDITY)
        self.assertEqual(report.uidnext, 1125)

    def test_only_read_only_imap_commands_are_used(self):
        fake = FakeImap(three_messages())
        replay_tenant_guest_inbox(self.tenant_fresh(), from_uid=1122, to_uid=1124, connect=lambda s: fake)
        self.assertEqual(fake.commands(), ["STATUS", "SELECT", "SEARCH", "FETCH", "FETCH", "FETCH"])
        self.assertTrue(fake.logged_out)  # FakeImap asserts readonly select and PEEK fetch itself

    def test_dry_run_takes_no_lock(self):
        fake = FakeImap(three_messages())
        with patch.object(replay, "guest_imap_single_flight", side_effect=AssertionError("lock")):
            replay_tenant_guest_inbox(self.tenant_fresh(), from_uid=1122, to_uid=1124, connect=lambda s: fake)

    def test_dry_run_classifies_every_outcome_and_reports_blocking(self):
        msgs = {
            1122: (make_eml(message_id="ok@x"), D1),
            1123: (make_eml(message_id="own@x", sender="room_reservations@uzorita.hr"), D1),
            1124: (make_eml(message_id="ng@x", sender="someone@example.com"), D1),
            1125: (b"From: x@guest.booking.com\r\nMessage-ID: <nc@x>\r\n\r\nno booking number, no said block\r\n", D1),
            1126: (make_eml(message_id="nores@x", code="9999999999"), D1),
        }
        msgs[1127] = (b"", D1)  # present on the server, but the fetch fails
        fake = FakeImap(msgs, uidnext=1128, fail_fetch=(1127,))
        report = replay_tenant_guest_inbox(self.tenant_fresh(), from_uid=1122, to_uid=1127, connect=lambda s: fake)
        self.assertEqual(
            [i.outcome for i in report.items],
            ["would_insert", "skip_own_outbound", "skip_not_booking_guest", "skip_no_booking_code",
             "no_reservation", "fetch_error"],
        )
        self.assertEqual(len(report.blocking), 2)  # no_reservation + fetch_error
        self.assertEqual(report.items[0].reservation_id, self.res_a.pk)
        self.assertEqual(report.items[0].reservation_status, "expected")
        self.assertEqual(self.cursor(), CURSOR)

    def test_dry_run_flags_duplicates_inside_the_batch_and_in_the_database(self):
        existing = parse_guest_email_bytes(make_eml(message_id="dup-db@x"))
        ingest_parsed_email(self.tenant_fresh(), existing, side_effects=False)
        msgs = {
            1122: (make_eml(message_id="dup-db@x"), D1),
            1123: (make_eml(message_id="same@x"), D1),
            1124: (make_eml(message_id="same@x"), D1),
        }
        report = replay_tenant_guest_inbox(
            self.tenant_fresh(), from_uid=1122, to_uid=1124, connect=lambda s: FakeImap(msgs))
        self.assertEqual([i.outcome for i in report.items], ["duplicate", "would_insert", "duplicate"])

    def test_dry_run_uidvalidity_and_range_problems_are_reported_not_raised(self):
        fake = FakeImap(three_messages(), uidnext=1124)  # to-uid 1124 is beyond UIDNEXT-1
        report = replay_tenant_guest_inbox(
            self.tenant_fresh(), from_uid=1122, to_uid=1124, expect_uidvalidity=1, connect=lambda s: fake)
        self.assertTrue(any("UIDVALIDITY" in b for b in report.blocking))
        self.assertTrue(any("UIDNEXT" in b for b in report.blocking))


class ApplyTests(ReplayCase):
    def apply(self, msgs=None, **kw):
        fake = FakeImap(msgs if msgs is not None else three_messages(), **kw.pop("fake_kw", {}))
        args = dict(from_uid=1122, to_uid=1124, apply=True, expect_cursor=CURSOR, connect=lambda s: fake)
        args.update(kw)
        return replay_tenant_guest_inbox(self.tenant_fresh(), **args), fake

    def test_apply_stores_with_original_dates_moves_cursor_and_touches_once_per_reservation(self):
        with patch(TOUCH) as touch:
            report, fake = self.apply()
        self.assertTrue(report.applied)
        self.assertEqual((report.cursor_before, report.cursor_after, self.cursor()), (CURSOR, 1124, 1124))
        self.assertEqual([i.outcome for i in report.items], ["inserted"] * 3)

        rows = {r.message_id: r for r in GuestInboundMessage.objects.all()}
        self.assertEqual(rows["m1@x"].received_at, datetime(2026, 7, 31, 21, 50, tzinfo=timezone.utc))
        self.assertEqual(rows["m3@x"].received_at, datetime(2026, 10, 26, 11, 53, 34, tzinfo=timezone.utc))
        self.assertEqual(rows["m1@x"].reservation_id, self.res_a.pk)
        self.assertEqual(rows["m3@x"].reservation_id, self.res_b.pk)
        # canonical dual-write carries the ORIGINAL date, not "now"
        for mid in ("m1@x", "m2@x", "m3@x"):
            canon = GuestMessage.objects.get(sources__provider_message_id__contains=mid.split("@")[0])
            self.assertEqual(canon.occurred_at, rows[mid].received_at)

        calls = sorted((c.args[0], c.args[1], c.kwargs["reason"]) for c in touch.call_args_list)
        self.assertEqual(calls, sorted([
            (self.res_a.pk, ReservationVersionScope.MESSAGES, "guest_email_replay"),
            (self.res_b.pk, ReservationVersionScope.MESSAGES, "guest_email_replay"),
        ]))  # reservation A has two messages but is bumped ONCE
        self.assertEqual(set(report.touched_reservations), {self.res_a.pk, self.res_b.pk})

    def test_real_version_bump_is_messages_scope_only_and_single(self):
        report, _ = self.apply()
        rows = list(ReservationVersion.objects.order_by("reservation_id").values_list("reservation_id", "scope", "version"))
        self.assertEqual(rows, [(self.res_a.pk, "messages", 1), (self.res_b.pk, "messages", 1)])
        self.assertTrue(report.applied)

    def test_missing_date_header_falls_back_to_internaldate_never_now(self):
        msgs = {1122: (make_eml(message_id="nodate@x", date=None), D1)}
        self.apply(msgs, to_uid=1122)
        self.assertEqual(GuestInboundMessage.objects.get().received_at, D1)

    def test_whole_range_is_verified_and_gaps_from_expunged_uids_are_fine(self):
        msgs = {1122: (make_eml(message_id="a@x"), D1), 1126: (make_eml(message_id="b@x"), D1)}  # 1123-1125 expunged
        report, _ = self.apply(msgs, to_uid=1127, fake_kw={"uidnext": 1128})
        self.assertEqual((len(report.items), self.cursor()), (2, 1127))

    # ---- cursor protection: nothing is read or written when these fail
    def assert_untouched(self, connect_calls):
        self.assertEqual(connect_calls, [])
        self.assertEqual(snapshot(self)["inbound"], 0)
        self.assertEqual(self.cursor(), CURSOR)

    def test_apply_requires_expect_cursor(self):
        calls = []
        with self.assertRaisesMessage(ReplayError, "--expect-cursor"):
            replay_tenant_guest_inbox(self.tenant_fresh(), from_uid=1122, to_uid=1124, apply=True,
                                      connect=lambda s: calls.append(1))
        self.assert_untouched(calls)

    def test_expect_cursor_mismatch_is_refused_before_any_imap_access(self):
        calls = []
        with self.assertRaises(CursorMismatch):
            replay_tenant_guest_inbox(self.tenant_fresh(), from_uid=1101, to_uid=1124, apply=True,
                                      expect_cursor=1100, connect=lambda s: calls.append(1))
        self.assert_untouched(calls)

    def test_range_must_start_right_after_the_cursor(self):
        calls = []
        with self.assertRaisesMessage(ReplayError, "right after the cursor"):
            replay_tenant_guest_inbox(self.tenant_fresh(), from_uid=1125, to_uid=1126, apply=True,
                                      expect_cursor=CURSOR, connect=lambda s: calls.append(1))
        self.assert_untouched(calls)

    def test_cursor_cannot_pass_uidnext_minus_one(self):
        with self.assertRaisesMessage(ReplayError, "UIDNEXT"):
            self.apply(fake_kw={"uidnext": 1124})  # message 1124 would not exist yet
        self.assertEqual((GuestInboundMessage.objects.count(), self.cursor()), (0, CURSOR))

    def test_uidvalidity_expectation_is_enforced(self):
        with self.assertRaisesMessage(ReplayError, "UIDVALIDITY"):
            self.apply(expect_uidvalidity=UIDVALIDITY + 1)
        self.assertEqual(GuestInboundMessage.objects.count(), 0)
        report, _ = self.apply(expect_uidvalidity=UIDVALIDITY)
        self.assertTrue(report.applied)

    def test_range_size_limit(self):
        calls = []
        with self.assertRaisesMessage(ReplayError, "larger than"):
            replay_tenant_guest_inbox(self.tenant_fresh(), from_uid=1122, to_uid=1400, apply=True,
                                      expect_cursor=CURSOR, connect=lambda s: calls.append(1))
        self.assertEqual(calls, [])
        report, _ = self.apply({}, to_uid=1400, allow_large_range=True, fake_kw={"uidnext": 1401})
        self.assertEqual(report.cursor_after, 1400)

    def test_bad_arguments_and_missing_credentials(self):
        for kw in ({"from_uid": 0, "to_uid": 5}, {"from_uid": 9, "to_uid": 5}):
            with self.assertRaises(ReplayError):
                replay_tenant_guest_inbox(self.tenant_fresh(), connect=lambda s: None, **kw)
        self.settings_row.guest_smtp_password_encrypted = ""
        self.settings_row.save()
        with self.assertRaisesMessage(ReplayError, "credentials"):
            replay_tenant_guest_inbox(self.tenant_fresh(), from_uid=1122, to_uid=1124, connect=lambda s: None)

    # ---- blocking outcomes: refused, nothing written
    def test_unmatched_booking_blocks_the_cursor_unless_explicitly_accepted(self):
        msgs = {1122: (make_eml(message_id="ok@x"), D1), 1123: (make_eml(message_id="nores@x", code="9999999999"), D1)}
        with self.assertRaisesMessage(ReplayError, "no_reservation"):
            self.apply(msgs, to_uid=1123)
        self.assertEqual((GuestInboundMessage.objects.count(), self.cursor()), (0, CURSOR))
        report, _ = self.apply(msgs, to_uid=1123, accept_unmatched=True)
        self.assertEqual([i.outcome for i in report.items], ["inserted", "no_reservation"])
        self.assertEqual((GuestInboundMessage.objects.count(), self.cursor()), (1, 1123))

    def test_fetch_error_and_parse_error_refuse_the_whole_batch(self):
        with self.assertRaisesMessage(ReplayError, "fetch_error"):
            self.apply(fake_kw={"fail_fetch": (1123,)})
        with patch.object(replay, "parse_guest_email_bytes", side_effect=ValueError("boom")):
            with self.assertRaisesMessage(ReplayError, "parse_error"):
                self.apply()
        self.assertEqual((GuestInboundMessage.objects.count(), self.cursor()), (0, CURSOR))

    # ---- rollback
    def test_failure_in_the_middle_rolls_back_rows_canonical_cursor_and_versions(self):
        calls = {"n": 0}

        def flaky(tenant, parsed, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise IntegrityError("simulated failure on the second message")
            return ingest_parsed_email(tenant, parsed, **kw)

        with patch.object(replay, "ingest_parsed_email", side_effect=flaky), patch(TOUCH) as touch:
            with self.assertRaises(IntegrityError):
                self.apply()
        self.assertEqual(calls["n"], 2)
        self.assertEqual(snapshot(self), {"inbound": 0, "canon_msgs": 0, "canon_src": 0, "versions": [], "cursor": CURSOR})
        touch.assert_not_called()

    def test_failure_saving_the_cursor_rolls_back_the_inserted_messages(self):
        from apps.tenants.models import TenantReceptionSettings
        with patch.object(TenantReceptionSettings, "save", side_effect=RuntimeError("cursor save failed")), patch(TOUCH) as touch:
            with self.assertRaises(RuntimeError):
                self.apply()
        self.assertEqual(snapshot(self)["inbound"], 0)
        self.assertEqual(snapshot(self)["canon_src"], 0)
        touch.assert_not_called()

    def test_cursor_changed_by_someone_else_during_the_run_rolls_everything_back(self):
        from apps.tenants.models import TenantReceptionSettings
        real = replay._read_range

        def read_then_move_cursor(*a, **kw):
            out = real(*a, **kw)
            TenantReceptionSettings.objects.filter(pk=self.settings_row.pk).update(guest_imap_last_uid=1125)
            return out

        with patch.object(replay, "_read_range", side_effect=read_then_move_cursor), patch(TOUCH) as touch:
            with self.assertRaises(CursorMismatch):
                self.apply()
        self.assertEqual((GuestInboundMessage.objects.count(), self.cursor()), (0, 1125))
        touch.assert_not_called()

    def test_version_bump_failure_after_commit_keeps_the_data_and_is_reported(self):
        def bump(rid, scope, **kw):
            if rid == self.res_a.pk:
                raise RuntimeError("event bus down")

        with patch(TOUCH, side_effect=bump):
            report, _ = self.apply()
        self.assertTrue(report.applied)
        self.assertEqual((GuestInboundMessage.objects.count(), self.cursor()), (3, 1124))
        self.assertEqual(report.touch_failures, [self.res_a.pk])
        self.assertEqual(report.touched_reservations, [self.res_b.pk])

    # ---- duplicates
    def test_duplicates_are_not_inserted_twice_and_do_not_bump_versions(self):
        ingest_parsed_email(self.tenant_fresh(), parse_guest_email_bytes(make_eml(message_id="m1@x")), side_effects=False)
        msgs = {1122: (make_eml(message_id="m1@x"), D1), 1123: (make_eml(message_id="new@x", code=CODE_B), D1)}
        with patch(TOUCH) as touch:
            report, _ = self.apply(msgs, to_uid=1123)
        self.assertEqual([i.outcome for i in report.items], ["duplicate", "inserted"])
        self.assertEqual(GuestInboundMessage.objects.count(), 2)
        self.assertEqual([c.args[0] for c in touch.call_args_list], [self.res_b.pk])  # not reservation A

    def test_a_duplicate_detected_only_at_insert_time_is_reported_as_duplicate(self):
        with patch.object(replay, "ingest_parsed_email", return_value=None), patch(TOUCH) as touch:
            report, _ = self.apply()
        self.assertEqual([i.outcome for i in report.items], ["duplicate"] * 3)
        touch.assert_not_called()
        self.assertEqual(self.cursor(), 1124)  # everything was already stored: the range is resolved

    def test_second_apply_of_the_same_range_is_refused_by_the_cursor_check(self):
        self.apply()
        with self.assertRaises(CursorMismatch):
            self.apply()
        self.assertEqual(GuestInboundMessage.objects.count(), 3)

    def test_same_message_id_twice_in_one_batch_is_stored_once(self):
        msgs = {1122: (make_eml(message_id="twin@x"), D1), 1123: (make_eml(message_id="twin@x"), D1)}
        report, _ = self.apply(msgs, to_uid=1123)
        self.assertEqual([i.outcome for i in report.items], ["inserted", "duplicate"])
        self.assertEqual(GuestInboundMessage.objects.count(), 1)

    # ---- single-flight
    def test_apply_is_refused_while_another_process_holds_the_lock(self):
        calls = []
        with hold_guest_imap_lock(self.tenant.pk):
            with self.assertRaises(ReplayLocked):
                replay_tenant_guest_inbox(self.tenant_fresh(), from_uid=1122, to_uid=1124, apply=True,
                                          expect_cursor=CURSOR, connect=lambda s: calls.append(1))
        self.assertEqual(calls, [])
        self.assertEqual((GuestInboundMessage.objects.count(), self.cursor()), (0, CURSOR))

    def test_lock_is_released_after_success_and_after_failure(self):
        from apps.communications.tests.guest_imap_test_support import try_lock_from_thread
        self.apply()
        self.assertTrue(try_lock_from_thread(self.tenant.pk))
        with self.assertRaises(CursorMismatch):
            self.apply()
        self.assertTrue(try_lock_from_thread(self.tenant.pk))


class CommandTests(ReplayCase):
    def run_cmd(self, *args):
        out = StringIO()
        fake = FakeImap(three_messages())
        with patch(CONNECT, return_value=fake):
            call_command("replay_guest_email", "--from-uid", "1122", "--to-uid", "1124", *args, stdout=out)
        return out.getvalue(), fake

    def test_default_is_dry_run(self):
        before = snapshot(self)
        out, _ = self.run_cmd()
        self.assertTrue(out.startswith("DRY-RUN"))
        self.assertIn("nothing was written (dry-run)", out)
        self.assertEqual(snapshot(self), before)

    def test_apply_needs_expect_cursor_then_reports(self):
        with self.assertRaisesMessage(CommandError, "--expect-cursor"):
            self.run_cmd("--apply")
        out, _ = self.run_cmd("--apply", "--expect-cursor", str(CURSOR))
        self.assertTrue(out.startswith("APPLY"))
        self.assertIn(f"applied: cursor {CURSOR} -> 1124", out)
        self.assertEqual(self.cursor(), 1124)

    def test_unknown_tenant_and_locked_are_command_errors(self):
        with self.assertRaisesMessage(CommandError, "Tenant not found"):
            call_command("replay_guest_email", "--tenant", "nope", "--from-uid", "1", "--to-uid", "2")
        with hold_guest_imap_lock(self.tenant.pk):
            with self.assertRaisesMessage(CommandError, "lock"):
                self.run_cmd("--apply", "--expect-cursor", str(CURSOR))

    def test_report_lines_are_ids_dates_and_counts_only(self):
        report, = [replay_tenant_guest_inbox(self.tenant_fresh(), from_uid=1122, to_uid=1124,
                                              connect=lambda s: FakeImap(three_messages()))]
        text = "\n".join(format_report(report))
        self.assertIn("uid=1122 internaldate=2026-07-31T21:54:02+00:00 outcome=would_insert", text)
        self.assertNotIn("@", text)

class StoredUidValidityTests(ReplayCase):
    def test_replay_refuses_when_the_mailbox_generation_differs_from_the_stored_one(self):
        from apps.tenants.models import TenantReceptionSettings
        TenantReceptionSettings.objects.filter(pk=self.settings_row.pk).update(guest_imap_uidvalidity=UIDVALIDITY + 1)
        fake = FakeImap(three_messages())
        report = replay_tenant_guest_inbox(self.tenant_fresh(), from_uid=1122, to_uid=1124, connect=lambda s: fake)
        self.assertTrue(any("stored value" in b for b in report.blocking))
        with self.assertRaisesMessage(ReplayError, "stored value"):
            replay_tenant_guest_inbox(self.tenant_fresh(), from_uid=1122, to_uid=1124, apply=True,
                                      expect_cursor=CURSOR, connect=lambda s: FakeImap(three_messages()))
        self.assertEqual((GuestInboundMessage.objects.count(), self.cursor()), (0, CURSOR))

    def test_replay_accepts_a_matching_or_missing_stored_value(self):
        from apps.tenants.models import TenantReceptionSettings
        TenantReceptionSettings.objects.filter(pk=self.settings_row.pk).update(guest_imap_uidvalidity=UIDVALIDITY)
        report = replay_tenant_guest_inbox(self.tenant_fresh(), from_uid=1122, to_uid=1124,
                                           connect=lambda s: FakeImap(three_messages()))
        self.assertEqual(report.blocking, [])
