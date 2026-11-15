"""retry_guest_imap_issues: late processing of ledger entries, without automation by default."""

from datetime import datetime, timezone
from io import StringIO
from unittest.mock import patch

from django.core.management import CommandError, call_command

from apps.communications.guest_imap_issue_retry import RetryError, retry_issues
from apps.communications.models import GuestImapIssue, GuestInboundMessage
from apps.communications.tests.guest_imap_test_support import (
    CODE_B, CURSOR, UIDVALIDITY, FakeImap, ReplayCase, hold_guest_imap_lock, make_eml,
)
from apps.reservations.models import Reservation, ReservationVersion
from apps.tenants.models import TenantReceptionSettings

CONNECT = "apps.communications.guest_imap_issue_retry._connect_imap"
TOUCH = "apps.communications.guest_imap_issue_retry.touch_reservation_version"
D = datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc)
AUTOMATION = (
    "apps.communications.guest_language_inbound.on_guest_inbound_message",
    "apps.communications.guest_arrival_inbound.maybe_handle_guest_arrival_inbound",
    "apps.communications.guest_invoice_inbound.maybe_handle_guest_invoice_inbound",
    "apps.communications.guest_parking_inbound.maybe_handle_guest_parking_inbound",
    "apps.communications.guest_message_send.send_guest_message",
    "apps.core.tasks.notify_guest_message_inbound.delay",
)


class RetryCase(ReplayCase):
    def setUp(self):
        super().setUp()
        TenantReceptionSettings.objects.filter(pk=self.settings_row.pk).update(
            guest_imap_uidvalidity=UIDVALIDITY, guest_imap_last_uid=1128)
        self.guards = {}
        for target in AUTOMATION:
            p = patch(target, side_effect=AssertionError(f"automation reached: {target}"))
            self.guards[target] = p.start()
            self.addCleanup(p.stop)

    def issue(self, uid, status="quarantined", kind="fetch_failed", uidvalidity=UIDVALIDITY, attempts=10):
        return GuestImapIssue.objects.create(
            tenant=self.tenant, uid=uid, uidvalidity=uidvalidity, kind=kind, status=status, attempts=attempts)

    def mailbox(self, msgs, **kw):
        return FakeImap({u: (m, D) for u, m in msgs.items()}, uidnext=1129, **kw)

    def run_retry(self, msgs, **kw):
        fake = kw.pop("fake", None) or self.mailbox(msgs)
        return retry_issues(self.tenant_fresh(), connect=lambda s: fake, **kw)


class RetryTests(RetryCase):
    def test_dry_run_writes_nothing_and_reports_per_uid(self):
        self.issue(1123); self.issue(1124, kind="no_reservation")
        msgs = {1123: make_eml(message_id="r1@x"), 1124: make_eml(message_id="r2@x", code="9999999999")}
        report = self.run_retry(msgs)
        self.assertEqual([(i.uid, i.outcome) for i in report.items], [(1123, "would_insert"), (1124, "still_failing:no_reservation")])
        self.assertEqual((GuestInboundMessage.objects.count(), GuestImapIssue.objects.filter(status="resolved").count()), (0, 0))

    def test_apply_stores_without_any_automation_resolves_and_bumps_once_per_reservation(self):
        self.issue(1123); self.issue(1124); self.issue(1125, kind="no_reservation")
        msgs = {1123: make_eml(message_id="r1@x"), 1124: make_eml(message_id="r2@x"),
                1125: make_eml(message_id="r3@x", code=CODE_B)}
        with patch(TOUCH) as touch:
            report = self.run_retry(msgs, apply=True)
        self.assertTrue(report.applied)
        self.assertEqual([i.outcome for i in report.items], ["inserted"] * 3)
        self.assertEqual(GuestInboundMessage.objects.count(), 3)
        self.assertEqual(set(GuestImapIssue.objects.values_list("status", flat=True)), {"resolved"})
        self.assertEqual(sorted(c.args[0] for c in touch.call_args_list), sorted([self.res_a.pk, self.res_b.pk]))
        for mock in self.guards.values():
            mock.assert_not_called()
        self.assertEqual(self.cursor(), 1128)  # the cursor is not touched

    def test_with_automation_is_an_explicit_opt_in(self):
        self.issue(1123)
        for target in AUTOMATION:
            self.guards[target].side_effect = None
        self.guards["apps.communications.guest_arrival_inbound.maybe_handle_guest_arrival_inbound"].return_value = None
        self.guards["apps.communications.guest_invoice_inbound.maybe_handle_guest_invoice_inbound"].return_value = None
        self.guards["apps.communications.guest_parking_inbound.maybe_handle_guest_parking_inbound"].return_value = None
        report = self.run_retry({1123: make_eml(message_id="r1@x")}, apply=True, with_automation=True)
        self.assertEqual(report.items[0].outcome, "inserted")
        self.guards["apps.core.tasks.notify_guest_message_inbound.delay"].assert_called_once()

    def test_still_failing_messages_stay_in_the_ledger(self):
        self.issue(1123, kind="no_reservation", status="open", attempts=3)
        self.run_retry({1123: make_eml(message_id="r@x", code="9999999999")}, apply=True)
        issue = GuestImapIssue.objects.get()
        self.assertEqual((issue.status, issue.attempts), ("open", 3))

    def test_fetch_failure_keeps_the_issue(self):
        self.issue(1123)
        report = self.run_retry({1123: make_eml(message_id="r@x")}, apply=True,
                                fake=self.mailbox({1123: make_eml(message_id="r@x")}, fail_fetch=(1123,)))
        self.assertEqual(report.items[0].outcome, "still_failing:fetch_error")
        self.assertEqual(GuestImapIssue.objects.get().status, "quarantined")

    def test_duplicates_and_legitimate_skips_resolve_the_issue(self):
        from apps.communications.guest_email_ingest import ingest_parsed_email, parse_guest_email_bytes
        ingest_parsed_email(self.tenant_fresh(), parse_guest_email_bytes(make_eml(message_id="dup@x")), side_effects=False)
        self.issue(1123); self.issue(1124)
        msgs = {1123: make_eml(message_id="dup@x"), 1124: make_eml(message_id="own@x", sender="room_reservations@uzorita.hr")}
        report = self.run_retry(msgs, apply=True)
        self.assertEqual([i.outcome for i in report.items], ["duplicate", "skip_own_outbound"])
        self.assertEqual(set(GuestImapIssue.objects.values_list("status", flat=True)), {"resolved"})
        self.assertEqual(GuestInboundMessage.objects.count(), 1)

    def test_issues_from_another_uidvalidity_are_reported_stale_and_left_alone(self):
        self.issue(1123, uidvalidity=UIDVALIDITY - 1)
        report = self.run_retry({1123: make_eml(message_id="r@x")}, apply=True)
        self.assertEqual(report.items[0].outcome, "stale_uidvalidity")
        self.assertEqual((GuestInboundMessage.objects.count(), GuestImapIssue.objects.get().status), (0, "quarantined"))

    def test_server_uidvalidity_mismatch_refuses_everything(self):
        self.issue(1123)
        with self.assertRaisesMessage(RetryError, "cannot be compared"):
            self.run_retry({1123: make_eml(message_id="r@x")}, apply=True,
                           fake=self.mailbox({1123: make_eml(message_id="r@x")}, uidvalidity=UIDVALIDITY + 1))
        self.assertEqual(GuestInboundMessage.objects.count(), 0)

    def test_uninitialised_uidvalidity_refuses(self):
        TenantReceptionSettings.objects.filter(pk=self.settings_row.pk).update(guest_imap_uidvalidity=None)
        self.issue(1123)
        with self.assertRaisesMessage(RetryError, "not initialised"):
            self.run_retry({1123: make_eml(message_id="r@x")})

    def test_mailbox_level_issues_are_not_message_retries(self):
        self.issue(0, kind="uidvalidity_changed", status="open", attempts=3)
        self.assertEqual(self.run_retry({}).items, [])

    def test_uid_filter(self):
        self.issue(1123); self.issue(1124)
        msgs = {1123: make_eml(message_id="a@x"), 1124: make_eml(message_id="b@x")}
        report = self.run_retry(msgs, apply=True, uids=[1124])
        self.assertEqual([i.uid for i in report.items], [1124])
        self.assertEqual(GuestImapIssue.objects.get(uid=1123).status, "quarantined")

    def test_apply_is_refused_while_polling_or_replay_holds_the_lock(self):
        self.issue(1123)
        with hold_guest_imap_lock(self.tenant.pk):
            with self.assertRaisesMessage(RetryError, "lock"):
                self.run_retry({1123: make_eml(message_id="a@x")}, apply=True)
        self.assertEqual(GuestInboundMessage.objects.count(), 0)

    def test_only_read_only_imap_commands(self):
        self.issue(1123)
        fake = self.mailbox({1123: make_eml(message_id="a@x")})
        self.run_retry({}, fake=fake, apply=True)
        self.assertEqual(fake.commands(), ["STATUS", "SELECT", "FETCH"])  # FakeImap enforces EXAMINE + PEEK


class RetryCommandTests(RetryCase):
    def cmd(self, *args, msgs=None):
        out = StringIO()
        fake = self.mailbox(msgs or {1123: make_eml(message_id="SENTINEL-MID@x", text="SENTINEL-BODY")})
        with patch(CONNECT, return_value=fake):
            call_command("retry_guest_imap_issues", *args, stdout=out)
        return out.getvalue()

    def test_default_is_dry_run_and_output_has_no_content(self):
        self.issue(1123)
        out = self.cmd()
        self.assertTrue(out.startswith("DRY-RUN"))
        self.assertIn("uid=1123 kind=fetch_failed status=quarantined attempts=10 -> would_insert", out)
        self.assertNotIn("SENTINEL", out)
        self.assertEqual(GuestInboundMessage.objects.count(), 0)

    def test_apply(self):
        self.issue(1123)
        with patch(TOUCH):
            out = self.cmd("--apply")
        self.assertIn("inserted", out)
        self.assertEqual(GuestImapIssue.objects.get().status, "resolved")

    def test_errors_are_command_errors(self):
        with self.assertRaisesMessage(CommandError, "Tenant not found"):
            call_command("retry_guest_imap_issues", "--tenant", "nope")
