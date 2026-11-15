"""Replay must have NO side effects: no LLM, replies, push, language/reservation changes."""

import ast
import logging
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core import mail
from django.core.management import call_command
from django.db import transaction

from apps.communications import guest_email_replay as replay
from apps.communications.guest_email_ingest import ingest_parsed_email, parse_guest_email_bytes
from apps.communications.guest_email_replay import replay_tenant_guest_inbox
from apps.communications.models import (
    GuestInboundMessage, GuestMessageDraft, GuestMessageThreadState, GuestOutboundMessage,
)
from apps.communications.tests.guest_imap_test_support import CURSOR, FakeImap, ReplayCase, make_eml
from apps.reservations.models import Reservation

CONNECT = "apps.communications.guest_email_replay._connect_imap"
D = datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc)

# Texts chosen to trigger every live auto-handler if it were (wrongly) invoked.
TRIGGERING = {
    1122: "We will arrive at 22:30 tonight, is late check-in possible?",
    1123: "Could you please send an invoice for the company? My email is acc@example.com",
    1124: "Is there parking available for our car?",
}


def trigger_messages() -> dict:
    return {
        uid: (make_eml(message_id=f"t{uid}@x", text=text), D) for uid, text in TRIGGERING.items()
    }


DANGEROUS = [
    "apps.communications.guest_language_inbound.on_guest_inbound_message",
    "apps.communications.guest_arrival_inbound.maybe_handle_guest_arrival_inbound",
    "apps.communications.guest_invoice_inbound.maybe_handle_guest_invoice_inbound",
    "apps.communications.guest_parking_inbound.maybe_handle_guest_parking_inbound",
    "apps.communications.guest_message_send.send_guest_message",
    "apps.ai.provider.llm_configured",
    "apps.core.tasks.notify_guest_message_inbound.delay",
    "apps.core.tasks.notify_guest_message_inbound.apply_async",
    "apps.core.tasks.send_push_notification.delay",
    "apps.integrations.whatsapp.client._post_whatsapp_message",
    "httpx.post",
    "httpx.get",
    "httpx.Client.send",
    "smtplib.SMTP.sendmail",
    "apps.communications.guest_email_ingest.touch_reservation_version",
]


def world(case) -> dict:
    """Everything the live automation could have changed."""
    return {
        "reservations": list(Reservation.objects.order_by("pk").values()),
        "thread_state": GuestMessageThreadState.objects.count(),
        "drafts": GuestMessageDraft.objects.count(),
        "outbound": GuestOutboundMessage.objects.count(),
        "outbox": len(mail.outbox),
    }


class NoSideEffectsTests(ReplayCase):
    def arm(self):
        patchers = [patch(t, side_effect=AssertionError(f"side effect reached: {t}")) for t in DANGEROUS]
        mocks = [p.start() for p in patchers]
        for p in patchers:
            self.addCleanup(p.stop)
        return dict(zip(DANGEROUS, mocks))

    def test_control_the_default_ingest_path_really_reaches_the_automation(self):
        """Without this, the armed patches could be vacuous (e.g. wrong import path)."""
        self.arm()
        parsed = parse_guest_email_bytes(make_eml(message_id="ctl@x", text=TRIGGERING[1122]))
        with self.assertRaisesMessage(AssertionError, "on_guest_inbound_message"):
            ingest_parsed_email(self.tenant_fresh(), parsed)  # default side_effects=True

    def test_ingest_with_side_effects_false_stores_only(self):
        self.arm()
        before = world(self)
        parsed = parse_guest_email_bytes(make_eml(message_id="so@x", text=TRIGGERING[1122]))
        row = ingest_parsed_email(self.tenant_fresh(), parsed, notify=True, side_effects=False)
        self.assertIsNotNone(row)
        self.assertEqual(world(self), before)
        self.assertEqual(GuestInboundMessage.objects.count(), 1)

    def test_full_apply_with_triggering_texts_changes_nothing_but_the_stored_messages(self):
        mocks = self.arm()
        before = world(self)
        with patch(CONNECT, return_value=FakeImap(trigger_messages())), \
                patch("apps.communications.guest_email_replay.touch_reservation_version"):
            call_command("replay_guest_email", "--from-uid", "1122", "--to-uid", "1124",
                         "--apply", "--expect-cursor", str(CURSOR), stdout=StringIO())
        self.assertEqual(GuestInboundMessage.objects.count(), 3)
        self.assertEqual(world(self), before)  # reservations, language/thread state, drafts, outbound, mail
        for name, m in mocks.items():
            m.assert_not_called()
        # the guest's stated arrival time / invoice e-mail / language were NOT written
        res = Reservation.objects.get(pk=self.res_a.pk)
        self.assertEqual(res.updated_at, before["reservations"][0]["updated_at"])
        self.assertEqual(GuestMessageThreadState.objects.count(), 0)

    def test_dry_run_with_triggering_texts_changes_nothing(self):
        mocks = self.arm()
        before = world(self)
        with patch(CONNECT, return_value=FakeImap(trigger_messages())):
            call_command("replay_guest_email", "--from-uid", "1122", "--to-uid", "1124", stdout=StringIO())
        self.assertEqual(GuestInboundMessage.objects.count(), 0)
        self.assertEqual(world(self), before)
        for m in mocks.values():
            m.assert_not_called()

    def test_notify_flag_is_ignored_when_side_effects_are_off(self):
        mocks = self.arm()
        parsed = parse_guest_email_bytes(make_eml(message_id="nf@x"))
        ingest_parsed_email(self.tenant_fresh(), parsed, notify=True, side_effects=False)
        mocks["apps.core.tasks.notify_guest_message_inbound.delay"].assert_not_called()

    def test_conversation_language_state_is_not_created_or_overwritten(self):
        with patch(CONNECT, return_value=FakeImap(trigger_messages())), \
                patch("apps.communications.guest_email_replay.touch_reservation_version"):
            replay_tenant_guest_inbox(self.tenant_fresh(), from_uid=1122, to_uid=1124, apply=True,
                                      expect_cursor=CURSOR, connect=lambda s: FakeImap(trigger_messages()))
        # live ingestion would write GuestMessageThreadState.conversation_language per message
        self.assertEqual(GuestMessageThreadState.objects.count(), 0)


class StaticIsolationTests(ReplayCase):
    FORBIDDEN = (
        "guest_arrival_inbound", "guest_invoice_inbound", "guest_parking_inbound",
        "guest_language_inbound", "conversation_language_store", "guest_message_send",
        "guest_compose", "apps.ai", "apps.core.tasks", "apps.integrations", "fcm", "httpx",
        "smtplib", "django.core.mail", "requests",
    )

    def test_replay_module_does_not_import_automation_llm_sending_or_push(self):
        path = Path(replay.__file__)
        imported = []
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                imported += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
                imported += [f"{node.module}.{a.name}" for a in node.names]
        bad = [i for i in imported for f in self.FORBIDDEN if f in i]
        self.assertEqual(bad, [], "replay must stay isolated from live automation")

    def test_replay_command_module_is_equally_isolated(self):
        cmd = Path(replay.__file__).parent / "management" / "commands" / "replay_guest_email.py"
        src = cmd.read_text()
        for f in self.FORBIDDEN:
            self.assertNotIn(f"import {f}", src)
        self.assertNotIn("guest_email_ingest import", src)  # it only goes through guest_email_replay


class NoContentInLogsOrOutputTests(ReplayCase):
    SENTINELS = ("SENTINEL-BODY-77", "SENTINEL-SUBJECT-88", "sentinel-sender-99", "sentinel-mid-55")

    def capture(self):
        records: list[logging.LogRecord] = []

        class H(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler, root = H(level=logging.DEBUG), logging.getLogger()
        old = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(handler)
        self.addCleanup(lambda: (root.removeHandler(handler), root.setLevel(old)))
        return records

    def msgs(self):
        return {1122: (make_eml(message_id=self.SENTINELS[3], text=self.SENTINELS[0], subject=self.SENTINELS[1],
                                sender=f"{self.SENTINELS[2]}@guest.booking.com"), D),
                1123: (make_eml(message_id="nores@x", code="9999999999", text=self.SENTINELS[0]), D)}

    def assert_clean(self, records, out):
        blob = out + "\n".join(f"{r.getMessage()} {r.__dict__}" for r in records)
        for s in self.SENTINELS:
            self.assertNotIn(s, blob)

    def test_dry_run_and_apply_never_emit_message_content(self):
        records = self.capture()
        out = StringIO()
        with patch(CONNECT, return_value=FakeImap(self.msgs())), \
                patch("apps.communications.guest_email_replay.touch_reservation_version"):
            call_command("replay_guest_email", "--from-uid", "1122", "--to-uid", "1123", stdout=out)
            call_command("replay_guest_email", "--from-uid", "1122", "--to-uid", "1123", "--apply",
                         "--expect-cursor", str(CURSOR), "--accept-unmatched", stdout=out)
        self.assertTrue(records, "expected at least the 'applied' log line")
        self.assert_clean(records, out.getvalue())

    def test_parse_errors_log_only_the_exception_type(self):
        records = self.capture()
        with patch.object(replay, "parse_guest_email_bytes", side_effect=ValueError(self.SENTINELS[0])):
            report = replay_tenant_guest_inbox(self.tenant_fresh(), from_uid=1122, to_uid=1122,
                                               connect=lambda s: FakeImap({1122: self.msgs()[1122]}))
        self.assertEqual(report.items[0].outcome, "parse_error")
        self.assert_clean(records, "\n".join(format_ for format_ in replay.format_report(report)))

    def test_dry_run_keeps_content_only_in_memory(self):
        """No file written under the temp/media/private roots during a dry-run."""
        import os
        import tempfile
        before = {os.path.join(d, f) for d, _, fs in os.walk(tempfile.gettempdir()) for f in fs}
        with patch(CONNECT, return_value=FakeImap(self.msgs())):
            call_command("replay_guest_email", "--from-uid", "1122", "--to-uid", "1123", stdout=StringIO())
        after = {os.path.join(d, f) for d, _, fs in os.walk(tempfile.gettempdir()) for f in fs}
        self.assertEqual(after - before, set())
