"""PR-3: ``poll_guest_email_inbox`` is registered but gated by GUEST_IMAP_CELERY_POLL_ENABLED (default off).

No test opens a real IMAP connection: ``_connect_imap`` is always faked or asserted not to be called.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase, override_settings

from apps.communications.email_ingest_tasks import poll_guest_email_inbox
from apps.communications.models import GuestImapIssue, GuestInboundMessage
from apps.communications.tests.guest_imap_test_support import (
    CURSOR, UIDVALIDITY, FakeLiveImap, ReplayCase, ReplayTxCase, hold_guest_imap_lock, make_eml,
)
from apps.tenants.models import Tenant, TenantReceptionSettings

TASK_NAME = "apps.communications.email_ingest_tasks.poll_guest_email_inbox"
CONNECT = "apps.communications.guest_email_ingest._connect_imap"
LOCK = "apps.communications.guest_email_ingest.guest_imap_single_flight"
STAMP = "apps.communications.guest_email_ingest.mark_conversation_ingest"
NOTIFY = "apps.core.tasks.notify_guest_message_inbound.delay"
HANDLERS = (
    "apps.communications.guest_arrival_inbound.maybe_handle_guest_arrival_inbound",
    "apps.communications.guest_invoice_inbound.maybe_handle_guest_invoice_inbound",
    "apps.communications.guest_parking_inbound.maybe_handle_guest_parking_inbound",
)
BACKEND_DIR = Path(__file__).resolve().parents[3]

# Beat entries whose task is NOT registered in the worker today. Out of scope for PR-3: this pins the
# current state so that registering the IMAP poll cannot silently drag any of them along.
OTHER_UNREGISTERED_BEAT_TASKS = frozenset({
    "apps.integrations.channex.message_tasks.sync_channex_messages_for_upcoming_checkins",
    "apps.integrations.channex.booking_tasks.process_channex_booking_revisions_feed_periodic",
    "apps.integrations.channex.review_tasks.sync_channex_reviews_periodic",
    "apps.reservations.overbooking_tasks.detect_overbooking_daily",
    "apps.reservations.overbooking_tasks.detect_multi_room_gaps_daily",
    "apps.integrations.channex.booking_tasks.reconcile_channex_cancelled_bookings_daily",
    "reservations.send_document_intake_quality_report",
    "core.send_daily_ops_report",
})

# config/settings/base.py calls ``environ.Env.read_env(<repo>/.env)``, which fills os.environ for every key the
# process does not already have. Dropping the variable from the subprocess env is therefore not enough: a local
# .env with GUEST_IMAP_CELERY_POLL_ENABLED=true would flip the "default" under test. The probe redirects
# ``read_env`` to a temporary dotenv file owned by the test (empty = no .env at all) before Django starts.
# Production configuration code is untouched; only the probe interpreter is rewired.
_PRELUDE = """
import os
import environ

_real_read_env = environ.Env.read_env


def _redirected_read_env(cls, env_file=None, **overrides):
    return _real_read_env(os.environ["PROBE_DOTENV"], **overrides)


environ.Env.read_env = classmethod(_redirected_read_env)
import django
django.setup()
"""

_PROBE = """
import json
from django.conf import settings
from config.celery import app
app.loader.import_default_modules()
print("PROBE" + json.dumps({
    "flag": settings.GUEST_IMAP_CELERY_POLL_ENABLED,
    "unregistered": sorted(v["task"] for v in settings.CELERY_BEAT_SCHEDULE.values() if v["task"] not in app.tasks),
}))
"""


def fresh_process_probe(dotenv: str | None = None) -> dict:
    """Import the Celery app in a NEW interpreter, like a worker does, and report what it registered.

    ``dotenv`` is the content of the ``.env`` the settings module will see (None/empty = no entries). The real
    repository ``.env`` is never read, and the variable is removed from the inherited environment.
    """
    env = {k: v for k, v in os.environ.items() if k != "GUEST_IMAP_CELERY_POLL_ENABLED"}
    with tempfile.TemporaryDirectory() as tmp:
        dotenv_path = Path(tmp) / ".env"
        dotenv_path.write_text(dotenv or "")
        env["PROBE_DOTENV"] = str(dotenv_path)
        proc = subprocess.run(
            [sys.executable, "-c", _PRELUDE + _PROBE],
            cwd=BACKEND_DIR, env=env, capture_output=True, text=True, timeout=120,
        )
    assert proc.returncode == 0, proc.stderr[-2000:]
    line = next(x for x in proc.stdout.splitlines() if x.startswith("PROBE"))
    return json.loads(line[len("PROBE"):])


class RegistrationTests(TestCase):
    def test_task_is_registered_in_a_fresh_celery_process(self):
        probe = fresh_process_probe()
        self.assertNotIn(TASK_NAME, probe["unregistered"])

    def test_flag_defaults_to_false_in_a_fresh_process(self):
        self.assertIs(fresh_process_probe()["flag"], False)  # no variable, no .env entry

    def test_dotenv_true_is_honoured_by_settings(self):
        """Regression: production wiring is unchanged — a .env entry still switches the flag on."""
        self.assertIs(fresh_process_probe("GUEST_IMAP_CELERY_POLL_ENABLED=true\n")["flag"], True)

    def test_dotenv_false_and_unrelated_entries_keep_the_flag_off(self):
        self.assertIs(fresh_process_probe("GUEST_IMAP_CELERY_POLL_ENABLED=false\n")["flag"], False)
        self.assertIs(fresh_process_probe("SOME_OTHER_KEY=true\n")["flag"], False)

    def test_the_other_eight_unregistered_beat_tasks_stay_out_of_scope(self):
        self.assertEqual(set(fresh_process_probe()["unregistered"]), OTHER_UNREGISTERED_BEAT_TASKS)
        self.assertEqual(len(OTHER_UNREGISTERED_BEAT_TASKS), 8)

    def test_beat_entry_is_unchanged(self):
        entry = settings.CELERY_BEAT_SCHEDULE["guest-email-imap-poll"]
        self.assertEqual((entry["task"], entry["schedule"], entry["kwargs"]), (TASK_NAME, 120.0, {"tenant_slug": "uzorita"}))


class _PollBase:
    def setUp(self):
        super().setUp()
        TenantReceptionSettings.objects.filter(pk=self.settings_row.pk).update(guest_imap_uidvalidity=UIDVALIDITY)
        for target in (NOTIFY, STAMP, *HANDLERS):
            p = patch(target, return_value=None)
            p.start()
            self.addCleanup(p.stop)

    def other_tenant(self, slug, *, enabled=True, password="x", status=Tenant.Status.ACTIVE):
        tenant = Tenant.objects.create(slug=slug, name=slug, status=status)
        TenantReceptionSettings.objects.create(
            tenant=tenant, guest_contact_email=f"r@{slug}.hr", guest_smtp_password_encrypted=password,
            guest_imap_enabled=enabled, guest_imap_last_uid=CURSOR, guest_imap_uidvalidity=UIDVALIDITY,
        )
        return tenant


# Pinned to False so the suite does not depend on the host .env / environment of the test process; the real
# default is asserted in a fresh, .env-isolated interpreter (RegistrationTests.test_flag_defaults_to_false_...).
@override_settings(GUEST_IMAP_CELERY_POLL_ENABLED=False)
class FlagOffTests(_PollBase, ReplayCase):
    def test_flag_off_does_nothing(self):
        before = (self.cursor(), GuestInboundMessage.objects.count(), GuestImapIssue.objects.count())
        with patch(CONNECT, side_effect=AssertionError("IMAP must not be opened")) as connect, \
                patch(LOCK, side_effect=AssertionError("lock must not be taken")), \
                self.assertNumQueries(0):  # not even a tenant lookup
            self.assertEqual(poll_guest_email_inbox(tenant_slug="uzorita"), {"disabled": True})
            self.assertEqual(poll_guest_email_inbox(), {"disabled": True})
            self.assertEqual(poll_guest_email_inbox(self.tenant.pk), {"disabled": True})
        connect.assert_not_called()
        self.assertEqual((self.cursor(), GuestInboundMessage.objects.count(), GuestImapIssue.objects.count()), before)

    def test_flag_off_does_nothing_even_with_new_mail_waiting(self):
        fake = FakeLiveImap({1122: make_eml(message_id="a@x")})
        with patch(CONNECT, return_value=fake) as connect:
            self.assertEqual(poll_guest_email_inbox(tenant_slug="uzorita"), {"disabled": True})
        connect.assert_not_called()
        self.assertEqual(fake.log, [])
        self.assertEqual((self.cursor(), GuestInboundMessage.objects.count()), (CURSOR, 0))


@override_settings(GUEST_IMAP_CELERY_POLL_ENABLED=True)
class FlagOnTenantScopeTests(_PollBase, ReplayCase):
    def run_task(self, **kwargs):
        fakes = []

        def connect(settings_obj):
            fake = FakeLiveImap({}, uidnext=1129)
            fake.settings_email = settings_obj.guest_contact_email
            fakes.append(fake)
            return fake

        with patch(CONNECT, side_effect=connect):
            summary = poll_guest_email_inbox(**kwargs)
        return summary, fakes

    def test_beat_kwargs_poll_only_the_named_tenant(self):
        self.other_tenant("other")  # enabled too, but not addressed by the beat entry
        summary, fakes = self.run_task(**settings.CELERY_BEAT_SCHEDULE["guest-email-imap-poll"]["kwargs"])
        self.assertEqual(summary["tenants"], 1)
        self.assertEqual([f.settings_email for f in fakes], ["room_reservations@uzorita.hr"])

    def test_only_permitted_tenants_are_polled(self):
        self.other_tenant("off", enabled=False)
        self.other_tenant("nopass", password="")
        self.other_tenant("suspended", status=Tenant.Status.SUSPENDED)
        self.other_tenant("on")
        summary, fakes = self.run_task()  # no filter: every tenant is considered, three must be skipped
        self.assertEqual(summary["tenants"], 2)
        self.assertEqual(sorted(f.settings_email for f in fakes), ["r@on.hr", "room_reservations@uzorita.hr"])

    def test_guest_imap_enabled_remains_the_tenant_level_control(self):
        TenantReceptionSettings.objects.filter(pk=self.settings_row.pk).update(guest_imap_enabled=False)
        summary, fakes = self.run_task(tenant_slug="uzorita")
        self.assertEqual(summary["tenants"], 0)
        self.assertEqual(fakes, [])

    def test_inactive_or_unknown_tenant_is_not_polled(self):
        self.other_tenant("suspended", status=Tenant.Status.SUSPENDED)
        for slug in ("suspended", "does-not-exist"):
            summary, fakes = self.run_task(tenant_slug=slug)
            self.assertEqual((summary["tenants"], fakes), (0, []))


@override_settings(GUEST_IMAP_CELERY_POLL_ENABLED=True)
class FlagOnGuardTests(_PollBase, ReplayCase):
    def test_uidvalidity_mismatch_refuses_without_touching_cursor_or_stored_value(self):
        fake = FakeLiveImap({1122: make_eml(message_id="a@x")}, uidvalidity=UIDVALIDITY + 1)
        with patch(CONNECT, return_value=fake):
            summary = poll_guest_email_inbox(tenant_slug="uzorita")
        self.assertEqual((summary["blocked"], summary["ingested"]), (1, 0))
        self.assertEqual(fake.fetched_uids(), [])
        self.assertEqual(self.cursor(), CURSOR)
        self.settings_row.refresh_from_db()
        self.assertEqual(self.settings_row.guest_imap_uidvalidity, UIDVALIDITY)
        self.assertEqual(GuestInboundMessage.objects.count(), 0)

    def test_cursor_without_stored_uidvalidity_is_refused_not_adopted(self):
        TenantReceptionSettings.objects.filter(pk=self.settings_row.pk).update(guest_imap_uidvalidity=None)
        fake = FakeLiveImap({1122: make_eml(message_id="a@x")})
        with patch(CONNECT, return_value=fake):
            summary = poll_guest_email_inbox(tenant_slug="uzorita")
        self.assertEqual((summary["blocked"], summary["ingested"]), (1, 0))
        self.settings_row.refresh_from_db()
        self.assertIsNone(self.settings_row.guest_imap_uidvalidity)
        self.assertEqual(self.cursor(), CURSOR)

    def test_cursor_beyond_mailbox_is_refused(self):
        fake = FakeLiveImap({}, uidnext=CURSOR)  # UIDNEXT-1 < cursor: the mailbox looks replaced
        with patch(CONNECT, return_value=fake):
            summary = poll_guest_email_inbox(tenant_slug="uzorita")
        self.assertEqual(summary["blocked"], 1)
        self.assertEqual(fake.fetched_uids(), [])
        self.assertEqual(self.cursor(), CURSOR)

    def test_caught_up_cursor_does_no_search_or_fetch(self):
        fake = FakeLiveImap({}, uidnext=CURSOR + 1)
        with patch(CONNECT, return_value=fake):
            summary = poll_guest_email_inbox(tenant_slug="uzorita")
        self.assertEqual((summary["tenants"], summary["blocked"], summary["ingested"]), (1, 0, 0))
        self.assertEqual((fake.searches(), fake.fetched_uids()), ([], []))


@override_settings(GUEST_IMAP_CELERY_POLL_ENABLED=True)
class FlagOnLockTests(_PollBase, ReplayTxCase):
    def test_advisory_lock_blocks_the_task_without_an_imap_session(self):
        with hold_guest_imap_lock(self.tenant.pk), patch(CONNECT) as connect:
            summary = poll_guest_email_inbox(tenant_slug="uzorita")
        self.assertEqual((summary["tenants"], summary["locked"], summary["ingested"]), (1, 1, 0))
        connect.assert_not_called()
        self.assertEqual(self.cursor(), CURSOR)
        # once the holder is gone the same call runs
        with patch(CONNECT, return_value=FakeLiveImap({}, uidnext=CURSOR + 1)) as connect:
            self.assertEqual(poll_guest_email_inbox(tenant_slug="uzorita")["locked"], 0)
        connect.assert_called_once()
