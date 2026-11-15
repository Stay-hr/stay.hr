"""Shared fixtures for guest IMAP replay / single-flight tests (defines no tests)."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from django.test import TestCase, TransactionTestCase
from django.utils import timezone as dj_timezone

from apps.communications.guest_imap_lock import guest_imap_single_flight
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant, TenantReceptionSettings

CODE_A = "5238895494"
CODE_B = "7000000001"
CURSOR = 1121
UIDVALIDITY = 1775254578


def make_eml(
    *,
    message_id: str,
    code: str = CODE_A,
    text: str = "Hello there",
    date: str | None = "Thu, 04 Jun 2026 17:00:49 +0200",
    sender: str = "guest-abc@guest.booking.com",
    subject: str = "Re: Booking",
) -> bytes:
    head = (
        f"From: Guest <{sender}>\r\nTo: room_reservations@uzorita.hr\r\n"
        f"Subject: {subject}\r\nMessage-ID: <{message_id}>\r\n"
    )
    if date:
        head += f"Date: {date}\r\n"
    head += "Content-Type: text/plain; charset=utf-8\r\n\r\n"
    return (head + f"Confirmation number: {code}\r\nGuest said:\r\n{text}\r\n").encode()


def imap_date(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%d-%b-%Y %H:%M:%S +0000")


class FakeImap:
    """Strict read-only IMAP double: any write command or non-PEEK fetch fails the test."""

    def __init__(self, messages: dict[int, tuple[bytes, datetime]], *, uidnext: int | None = None,
                 uidvalidity: int = UIDVALIDITY, fail_fetch: tuple[int, ...] = ()):
        self.messages = messages
        self.uidvalidity = uidvalidity
        self.uidnext = uidnext if uidnext is not None else (max(messages) + 1 if messages else 1)
        self.fail_fetch = set(fail_fetch)
        self.log: list[tuple] = []
        self.logged_out = False
        self.on_uid = None  # optional hook called at each uid() command (lock-held probes)

    # -- IMAP surface used by the replay
    def status(self, mailbox, items):
        self.log.append(("STATUS", mailbox, items))
        return "OK", [f"{mailbox} (UIDVALIDITY {self.uidvalidity} UIDNEXT {self.uidnext})".encode()]

    def select(self, mailbox="INBOX", readonly=False):
        self.log.append(("SELECT", mailbox, readonly))
        assert readonly is True, "replay must open the mailbox read-only (EXAMINE)"
        return "OK", [str(len(self.messages)).encode()]

    def uid(self, command, *args):
        cmd = command.upper()
        self.log.append(("UID", cmd, args))
        if self.on_uid:
            self.on_uid(cmd)
        assert cmd in ("SEARCH", "FETCH"), f"write/unsafe IMAP command: {cmd}"
        if cmd == "SEARCH":
            lo, hi = args[1].removeprefix("UID ").split(":")
            hi = self.uidnext - 1 if hi == "*" else int(hi)
            found = sorted(u for u in self.messages if int(lo) <= u <= hi)
            return "OK", [" ".join(map(str, found)).encode()]
        uid = int(args[0])
        item = args[1]
        meta_only = item.strip("() ") == "INTERNALDATE"
        assert meta_only or ("PEEK" in item and "RFC822" not in item), "fetch must not mark messages read"
        if uid in self.fail_fetch or uid not in self.messages:
            return "NO", [b"fetch failed"]
        raw, internal = self.messages[uid]
        if meta_only:
            return "OK", [f'1 (UID {uid} INTERNALDATE "{imap_date(internal)}")'.encode()]
        head = f'1 (UID {uid} INTERNALDATE "{imap_date(internal)}" BODY[] {{{len(raw)}}}'.encode()
        return "OK", [(head, raw), b")"]

    def logout(self):
        self.logged_out = True

    def commands(self) -> list[str]:
        return [e[1] if e[0] == "UID" else e[0] for e in self.log]


class FakeLiveImap:
    """IMAP double for the LIVE poll (read-write select, RFC822 fetch) with real ``n:*`` semantics."""

    def __init__(self, messages: dict[int, bytes], *, uidvalidity: int = UIDVALIDITY,
                 uidnext: int | None = None, status_ok: bool = True, ignore_search_bounds: bool = False,
                 fetch_no=(), fetch_raise=(), fetch_empty=()):
        self.messages = dict(messages)
        self.uidvalidity = uidvalidity
        self.uidnext = uidnext if uidnext is not None else (max(messages) + 1 if messages else 1)
        self.status_ok = status_ok
        self.ignore_search_bounds = ignore_search_bounds
        self.fetch_no, self.fetch_raise, self.fetch_empty = set(fetch_no), set(fetch_raise), set(fetch_empty)
        self.log: list[tuple] = []
        self.on_fetch = None  # hook(uid) called before every FETCH

    def status(self, mailbox, items):
        self.log.append(("STATUS",))
        if not self.status_ok:
            return "NO", [b"STATUS failed"]
        return "OK", [f"{mailbox} (UIDVALIDITY {self.uidvalidity} UIDNEXT {self.uidnext})".encode()]

    def select(self, mailbox="INBOX", readonly=False):
        self.log.append(("SELECT", readonly))
        return "OK", [str(len(self.messages)).encode()]

    def uid(self, command, *args):
        cmd = command.upper()
        self.log.append(("UID", cmd, args))
        assert cmd in ("SEARCH", "FETCH"), f"live poll used an unexpected command: {cmd}"
        if cmd == "SEARCH":
            lo, hi = args[1].removeprefix("UID ").split(":")
            top = max(self.messages) if self.messages else 0
            lo = int(lo)
            hi = top if hi == "*" else int(hi)
            a, b = min(lo, hi), max(lo, hi)  # IMAP: n:* with n > max UID still matches the highest UID
            if self.ignore_search_bounds:
                a, b = 0, 10**9
            return "OK", [" ".join(str(u) for u in sorted(self.messages) if a <= u <= b).encode()]
        uid = int(args[0])
        if self.on_fetch:
            self.on_fetch(uid)
        if uid in self.fetch_raise:
            import imaplib
            raise imaplib.IMAP4.abort("connection reset")
        if uid in self.fetch_no or uid not in self.messages:
            return "NO", [b"fetch failed"]
        if uid in self.fetch_empty:
            return "OK", [None]
        raw = self.messages[uid]
        return "OK", [(f"1 (UID {uid} RFC822 {{{len(raw)}}}".encode(), raw), b")"]

    def logout(self):
        self.log.append(("LOGOUT",))

    def fetched_uids(self) -> list[int]:
        return [int(e[2][0]) for e in self.log if e[0] == "UID" and e[1] == "FETCH"]

    def searches(self) -> list[str]:
        return [e[2][1] for e in self.log if e[0] == "UID" and e[1] == "SEARCH"]


class _WorldMixin:
    """Tenant ``uzorita`` with IMAP cursor 1121 and two EXPECTED reservations."""

    def setUp(self):
        self.tenant = Tenant.objects.create(slug="uzorita", name="Uzorita")
        self.settings_row = TenantReceptionSettings.objects.create(
            tenant=self.tenant,
            guest_contact_email="room_reservations@uzorita.hr",
            guest_smtp_password_encrypted="not-a-real-secret",  # connect is always faked
            guest_imap_enabled=True,
            guest_imap_last_uid=CURSOR,
        )
        prop = Property.objects.create(
            tenant=self.tenant, slug="uzorita", name="Uzorita",
            guest_parking_auto_reply_enabled=True, guest_invoice_auto_reply_enabled=True,
        )
        today = dj_timezone.localdate()
        self.res_a = Reservation.objects.create(
            tenant=self.tenant, property=prop, booking_code=CODE_A, check_in=today, check_out=today,
            booker_name="Test A", status=Reservation.Status.EXPECTED,
        )
        self.res_b = Reservation.objects.create(
            tenant=self.tenant, property=prop, booking_code=CODE_B, check_in=today, check_out=today,
            booker_name="Test B", status=Reservation.Status.EXPECTED,
        )

    def tenant_fresh(self) -> Tenant:
        return Tenant.objects.select_related("reception_settings").get(pk=self.tenant.pk)

    def cursor(self) -> int:
        return TenantReceptionSettings.objects.get(pk=self.settings_row.pk).guest_imap_last_uid


class ReplayCase(_WorldMixin, TestCase):
    pass


class ReplayTxCase(_WorldMixin, TransactionTestCase):
    """Committed data: other threads/connections can see it (needed for real concurrency)."""


def run_in_thread(fn):
    """Run ``fn`` on its own DB connection; return {'value': ..} or {'error': exc}."""
    out: dict = {}

    def worker():
        from django.db import connection
        try:
            out["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - captured for assertions
            out["error"] = exc
        finally:
            connection.close()

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    th.join(30)
    return out


@contextmanager
def hold_guest_imap_lock(tenant_id: int):
    """Hold the tenant IMAP lock from ANOTHER database connection (own thread)."""
    acquired_evt, release_evt, state = threading.Event(), threading.Event(), {}

    def worker():
        from django.db import connection
        try:
            with guest_imap_single_flight(tenant_id) as ok:
                state["ok"] = ok
                acquired_evt.set()
                release_evt.wait(15)
        finally:
            connection.close()

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    assert acquired_evt.wait(15), "lock holder thread did not start"
    assert state["ok"] is True
    try:
        yield
    finally:
        release_evt.set()
        th.join(15)


def try_lock_from_thread(tenant_id: int) -> bool:
    """Whether a different connection can take the lock right now."""
    out = {}

    def worker():
        from django.db import connection
        try:
            with guest_imap_single_flight(tenant_id) as ok:
                out["ok"] = ok
        finally:
            connection.close()

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    th.join(15)
    return out["ok"]
