"""init_guest_imap_uidvalidity: verified, explicit, never automatic."""

from datetime import datetime, timezone
from io import StringIO
from unittest.mock import patch

from django.core.management import CommandError, call_command

from apps.communications.guest_imap_init import InitError, format_init_report, initialise_uidvalidity
from apps.communications.guest_email_ingest import ingest_parsed_email, parse_guest_email_bytes
from apps.communications.tests.guest_imap_test_support import (
    CURSOR, UIDVALIDITY, FakeImap, ReplayCase, hold_guest_imap_lock, make_eml,
)
from apps.tenants.models import TenantReceptionSettings

CONNECT = "apps.communications.guest_imap_init._connect_imap"
D = datetime(2026, 7, 31, 18, 45, 15, tzinfo=timezone.utc)


def mailbox(**kw) -> FakeImap:
    return FakeImap({1121: (make_eml(message_id="old@x"), D), 1128: (make_eml(message_id="new@x"), D)}, **kw)


class InitCase(ReplayCase):
    def stored(self):
        return TenantReceptionSettings.objects.get(pk=self.settings_row.pk).guest_imap_uidvalidity

    def run_init(self, fake=None, **kw):
        args = dict(expect_uidvalidity=UIDVALIDITY, expect_cursor=CURSOR, connect=lambda s: fake or mailbox())
        args.update(kw)
        return initialise_uidvalidity(self.tenant_fresh(), **args)


class InitTests(InitCase):
    def test_dry_run_verifies_and_writes_nothing(self):
        report = self.run_init()
        self.assertEqual((report.mode, report.applied, report.blocking), ("dry-run", False, []))
        self.assertEqual((report.server_uidvalidity, report.server_uidnext, report.cursor_uid_present),
                         (UIDVALIDITY, 1129, True))
        self.assertEqual(report.cursor_uid_internaldate, D)
        self.assertIsNone(self.stored())

    def test_apply_stores_the_asserted_value_and_nothing_else(self):
        report = self.run_init(apply=True)
        self.assertTrue(report.applied)
        self.assertEqual((self.stored(), self.cursor()), (UIDVALIDITY, CURSOR))

    def test_read_only_mailbox_access(self):
        fake = mailbox()
        self.run_init(fake)
        self.assertEqual(fake.commands(), ["STATUS", "SELECT", "SEARCH", "FETCH"])  # FakeImap asserts readonly/no body

    def test_server_must_confirm_the_asserted_uidvalidity(self):
        with self.assertRaisesMessage(InitError, "operator asserted"):
            self.run_init(mailbox(uidvalidity=UIDVALIDITY + 1), apply=True)
        self.assertIsNone(self.stored())

    def test_cursor_assertion_must_match(self):
        with self.assertRaisesMessage(InitError, "cursor is"):
            self.run_init(expect_cursor=CURSOR - 1, apply=True)
        self.assertIsNone(self.stored())

    def test_cursor_beyond_the_mailbox_looks_like_a_replaced_mailbox_and_is_refused(self):
        with self.assertRaisesMessage(InitError, "replaced"):
            self.run_init(FakeImap({5: (make_eml(message_id="x@x"), D)}, uidnext=600), apply=True)
        self.assertIsNone(self.stored())

    def test_dry_run_reports_refusals_without_raising(self):
        report = self.run_init(mailbox(uidvalidity=1))
        self.assertTrue(report.blocking)
        self.assertIn("apply would be REFUSED", "\n".join(format_init_report(report)))

    def test_already_initialised_same_value_is_a_noop(self):
        TenantReceptionSettings.objects.filter(pk=self.settings_row.pk).update(guest_imap_uidvalidity=UIDVALIDITY)
        report = self.run_init(apply=True)
        self.assertEqual((report.already_initialised, report.applied), (True, False))

    def test_a_different_stored_value_is_never_overwritten_without_replace_from(self):
        TenantReceptionSettings.objects.filter(pk=self.settings_row.pk).update(guest_imap_uidvalidity=111)
        with self.assertRaisesMessage(InitError, "--replace-from 111"):
            self.run_init(apply=True)
        self.assertEqual(self.stored(), 111)

    def test_replace_from_must_match_and_then_replaces(self):
        TenantReceptionSettings.objects.filter(pk=self.settings_row.pk).update(guest_imap_uidvalidity=111)
        with self.assertRaisesMessage(InitError, "does not match"):
            self.run_init(apply=True, replace_from=222)
        self.run_init(apply=True, replace_from=111)
        self.assertEqual(self.stored(), UIDVALIDITY)
        with self.assertRaisesMessage(InitError, "no UIDVALIDITY is stored"):
            TenantReceptionSettings.objects.filter(pk=self.settings_row.pk).update(guest_imap_uidvalidity=None)
            self.run_init(apply=True, replace_from=111)

    def test_cursor_moving_during_the_run_is_caught_under_the_row_lock(self):
        real = FakeImap.status

        def status_then_move(fake, mailbox_, items):
            out = real(fake, mailbox_, items)
            TenantReceptionSettings.objects.filter(pk=self.settings_row.pk).update(guest_imap_last_uid=1125)
            return out

        with patch.object(FakeImap, "status", status_then_move):
            with self.assertRaises(InitError):
                self.run_init(apply=True)
        self.assertIsNone(self.stored())

    def test_refused_while_a_poll_or_replay_holds_the_lock(self):
        with hold_guest_imap_lock(self.tenant.pk):
            with self.assertRaisesMessage(InitError, "lock"):
                self.run_init(apply=True)
        self.assertIsNone(self.stored())

    def test_missing_credentials(self):
        self.settings_row.guest_smtp_password_encrypted = ""
        self.settings_row.save()
        with self.assertRaisesMessage(InitError, "credentials"):
            self.run_init()

    def test_after_initialisation_the_live_poll_is_allowed_to_run(self):
        from apps.communications.guest_email_ingest import poll_tenant_guest_inbox
        from apps.communications.tests.guest_imap_test_support import FakeLiveImap
        before = poll_tenant_guest_inbox  # noqa: F841
        with patch("apps.communications.guest_email_ingest._connect_imap", return_value=FakeLiveImap({}, uidnext=1122)), \
                self.assertLogs("apps.communications.guest_imap_issues", level="ERROR"):
            self.assertEqual(poll_tenant_guest_inbox(self.tenant_fresh()).blocked_reason, "uidvalidity_uninitialized")
        self.run_init(apply=True)
        with patch("apps.communications.guest_email_ingest._connect_imap", return_value=FakeLiveImap({}, uidnext=1122)):
            self.assertEqual(poll_tenant_guest_inbox(self.tenant_fresh()).blocked_reason, "")


class InitCommandTests(InitCase):
    def cmd(self, *args, fake=None):
        out = StringIO()
        with patch(CONNECT, return_value=fake or mailbox()):
            call_command("init_guest_imap_uidvalidity", "--expect-uidvalidity", str(UIDVALIDITY),
                         "--expect-cursor", str(CURSOR), *args, stdout=out)
        return out.getvalue()

    def test_default_is_dry_run(self):
        out = self.cmd()
        self.assertTrue(out.startswith("DRY-RUN"))
        self.assertIn("nothing was written (dry-run)", out)
        self.assertIsNone(self.stored())

    def test_apply_and_report_contain_no_addresses_or_content(self):
        out = self.cmd("--apply")
        self.assertIn(f"guest_imap_uidvalidity = {UIDVALIDITY}", out)
        self.assertNotIn("@", out)
        self.assertEqual(self.stored(), UIDVALIDITY)

    def test_report_helps_a_human_compare_the_cursor_message_with_the_newest_ingested_mail(self):
        ingest_parsed_email(self.tenant_fresh(), parse_guest_email_bytes(make_eml(
            message_id="i@x", date="Fri, 31 Jul 2026 20:45:00 +0200")), side_effects=False)
        out = self.cmd()
        self.assertIn("2026-07-31T18:45:15+00:00", out)
        self.assertIn("received_at: 2026-07-31T18:45:00+00:00", out)

    def test_errors_become_command_errors(self):
        with self.assertRaises(CommandError):
            self.cmd("--apply", fake=mailbox(uidvalidity=5))
        with self.assertRaisesMessage(CommandError, "Tenant not found"):
            call_command("init_guest_imap_uidvalidity", "--tenant", "nope", "--expect-uidvalidity", "1", "--expect-cursor", "1")
