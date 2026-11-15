"""Controlled, side-effect-free replay of historical guest emails from the IMAP inbox.

Why this exists: after an outage in live polling, the messages that arrived meanwhile
must be recorded WITHOUT re-running the live automation (LLM, auto-replies, push,
language/reservation updates), which would answer months-old messages.

Guarantees (each pinned by ``tests/test_guest_email_replay.py``):

- The mailbox is opened read-only (``EXAMINE``) and bodies are fetched with
  ``BODY.PEEK[]``: no flag changes, nothing is moved or deleted. Message content is
  held in memory only, is never logged and never appears in command output.
- Dry-run (default) writes nothing: no DB rows, no cursor, no version bump, no Redis stamp.
- ``--apply`` stores messages through ``ingest_parsed_email(side_effects=False)`` only. This
  module deliberately imports none of the auto-handlers, LLM, sending or push code.
- Everything in apply (inserts + cursor move) is ONE database transaction: any failure rolls
  the whole batch back. The cursor moves only after the entire UID range was fetched and
  every message resolved, and only if it still equals ``--expect-cursor`` (re-checked under
  a row lock).
- One MESSAGES version bump per affected reservation, after the commit.
- Mutually exclusive with live polling (tenant advisory lock) while applying.
"""

from __future__ import annotations

import dataclasses
import email
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any, Callable

from django.db import transaction

from apps.communications.guest_email_ingest import (
    ParsedGuestEmail,
    _address_contains_booking_guest,
    _connect_imap,
    _is_own_outbound_copy,
    ingest_parsed_email,
    match_reservation,
    parse_guest_email_bytes,
)
from apps.communications.guest_imap_lock import guest_imap_single_flight
from apps.communications.models import GuestInboundMessage
from apps.reservations.models import ReservationVersionScope
from apps.reservations.reservation_version import touch_reservation_version
from apps.tenants.models import Tenant, TenantReceptionSettings

logger = logging.getLogger(__name__)

MAX_REPLAY_RANGE = 200

# Outcomes (a "would_" prefix is never used: dry-run reports ``would_insert``).
WOULD_INSERT = "would_insert"
INSERTED = "inserted"
DUPLICATE = "duplicate"
SKIP_OWN_OUTBOUND = "skip_own_outbound"
SKIP_NOT_BOOKING_GUEST = "skip_not_booking_guest"
SKIP_NO_BOOKING_CODE = "skip_no_booking_code"
NO_RESERVATION = "no_reservation"
FETCH_ERROR = "fetch_error"
PARSE_ERROR = "parse_error"

_ALWAYS_BLOCKING = frozenset({FETCH_ERROR, PARSE_ERROR})

_MONTHS = {m: i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}
_INTERNALDATE_RE = re.compile(
    rb'INTERNALDATE "\s*(\d{1,2})-([A-Za-z]{3})-(\d{4}) (\d{2}):(\d{2}):(\d{2}) ([+-])(\d{2})(\d{2})"'
)
_STATUS_RE = re.compile(rb"(UIDVALIDITY|UIDNEXT)\s+(\d+)")


class ReplayError(Exception):
    """Refused before anything was written. The message never contains email content."""


class ReplayLocked(ReplayError):
    pass


class CursorMismatch(ReplayError):
    pass


@dataclass
class ReplayItem:
    uid: int
    internaldate: datetime | None
    outcome: str
    reservation_id: int | None = None
    reservation_status: str = ""


@dataclass
class ReplayReport:
    tenant_slug: str
    mode: str  # "dry-run" | "apply"
    from_uid: int
    to_uid: int
    uidvalidity: int | None = None
    uidnext: int | None = None
    cursor_before: int | None = None
    cursor_after: int | None = None
    items: list[ReplayItem] = field(default_factory=list)
    blocking: list[str] = field(default_factory=list)
    applied: bool = False
    touched_reservations: list[int] = field(default_factory=list)
    touch_failures: list[int] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for item in self.items:
            out[item.outcome] = out.get(item.outcome, 0) + 1
        return out


@dataclass
class _Fetched:
    uid: int
    internaldate: datetime | None
    raw: bytes | None
    error: bool = False


@dataclass
class _Candidate:
    item: ReplayItem
    parsed: ParsedGuestEmail | None = None


# ----------------------------------------------------------------------------- IMAP


def _parse_internaldate(chunks: list[bytes]) -> datetime | None:
    for chunk in chunks:
        m = _INTERNALDATE_RE.search(chunk)
        if not m:
            continue
        day, mon, year, hh, mm, ss, sign, oh, om = m.groups()
        month = _MONTHS.get(mon.decode().title())
        if month is None:
            return None
        offset = (int(oh) * 60 + int(om)) * (1 if sign == b"+" else -1)
        local = datetime(int(year), month, int(day), int(hh), int(mm), int(ss))
        return (local - timedelta(minutes=offset)).replace(tzinfo=dt_timezone.utc)
    return None


def _status_numbers(data: Any) -> dict[str, int]:
    blob = b" ".join(x for x in (data or []) if isinstance(x, bytes))
    return {k.decode(): int(v) for k, v in _STATUS_RE.findall(blob)}


def _fetch_one(client: Any, uid: int) -> _Fetched:
    try:
        typ, data = client.uid("FETCH", str(uid), "(INTERNALDATE BODY.PEEK[])")
    except Exception:
        return _Fetched(uid, None, None, error=True)
    if typ != "OK" or not data:
        return _Fetched(uid, None, None, error=True)
    raw: bytes | None = None
    meta: list[bytes] = []
    for part in data:
        if isinstance(part, tuple) and len(part) >= 2:
            meta.append(part[0])
            if isinstance(part[1], (bytes, bytearray)):
                raw = bytes(part[1])
        elif isinstance(part, bytes):
            meta.append(part)
    if not raw:
        return _Fetched(uid, _parse_internaldate(meta), None, error=True)
    return _Fetched(uid, _parse_internaldate(meta), raw)


def _read_range(
    settings: TenantReceptionSettings,
    *,
    from_uid: int,
    to_uid: int,
    connect: Callable[[TenantReceptionSettings], Any],
) -> tuple[int | None, int | None, list[_Fetched]]:
    """Read-only: STATUS, EXAMINE, UID SEARCH <range>, then BODY.PEEK per UID."""
    client = connect(settings)
    try:
        typ, data = client.status("INBOX", "(UIDVALIDITY UIDNEXT)")
        numbers = _status_numbers(data) if typ == "OK" else {}
        typ, _ = client.select("INBOX", readonly=True)
        if typ != "OK":
            raise ReplayError("could not open INBOX read-only")
        typ, data = client.uid("SEARCH", None, f"UID {from_uid}:{to_uid}")
        if typ != "OK":
            raise ReplayError("IMAP UID SEARCH failed")
        found = sorted(
            int(x) for x in (data[0].split() if data and data[0] else []) if from_uid <= int(x) <= to_uid
        )
        fetched = [_fetch_one(client, uid) for uid in found]
        return numbers.get("UIDVALIDITY"), numbers.get("UIDNEXT"), fetched
    finally:
        try:
            client.logout()
        except Exception:
            pass


# ------------------------------------------------------------------------ evaluation


def _evaluate(
    tenant: Tenant,
    settings: TenantReceptionSettings,
    fetched: _Fetched,
    seen_message_ids: set[str],
) -> _Candidate:
    item = ReplayItem(fetched.uid, fetched.internaldate, FETCH_ERROR)
    if fetched.error or fetched.raw is None:
        return _Candidate(item)
    try:
        msg = email.message_from_bytes(fetched.raw)
        if _is_own_outbound_copy(msg, settings.guest_contact_email):
            item.outcome = SKIP_OWN_OUTBOUND
            return _Candidate(item)
        if not _address_contains_booking_guest(msg):
            item.outcome = SKIP_NOT_BOOKING_GUEST
            return _Candidate(item)
        parsed = parse_guest_email_bytes(fetched.raw)
    except Exception as exc:  # message text must never reach logs: type name only
        logger.warning("guest email replay parse error uid=%s type=%s", fetched.uid, type(exc).__name__)
        item.outcome = PARSE_ERROR
        return _Candidate(item)
    if parsed is None:
        item.outcome = SKIP_NO_BOOKING_CODE
        return _Candidate(item)
    if parsed.received_at is None:
        # No usable Date header: keep a historical date, never "now".
        parsed = dataclasses.replace(parsed, received_at=fetched.internaldate)

    mid = parsed.message_id
    if mid and (
        mid in seen_message_ids
        or GuestInboundMessage.objects.filter(tenant=tenant, message_id=mid).exists()
    ):
        item.outcome = DUPLICATE
        return _Candidate(item, parsed)
    if mid:
        seen_message_ids.add(mid)

    reservation = match_reservation(tenant, parsed.booking_code)
    if reservation is None:
        item.outcome = NO_RESERVATION
        return _Candidate(item, parsed)
    item.outcome = WOULD_INSERT
    item.reservation_id = reservation.pk
    item.reservation_status = str(reservation.status)
    return _Candidate(item, parsed)


# ---------------------------------------------------------------------------- public


def replay_tenant_guest_inbox(
    tenant: Tenant,
    *,
    from_uid: int,
    to_uid: int,
    apply: bool = False,
    expect_cursor: int | None = None,
    expect_uidvalidity: int | None = None,
    accept_unmatched: bool = False,
    max_range: int = MAX_REPLAY_RANGE,
    allow_large_range: bool = False,
    connect: Callable[[TenantReceptionSettings], Any] | None = None,
) -> ReplayReport:
    # ---- argument validation: nothing below runs (no IMAP, no DB write) if this fails
    if from_uid < 1 or to_uid < from_uid:
        raise ReplayError("invalid UID range")
    if (to_uid - from_uid + 1) > max_range and not allow_large_range:
        raise ReplayError(f"UID range larger than {max_range}; pass --allow-large-range to confirm")
    if apply:
        if expect_cursor is None:
            raise ReplayError("--apply requires --expect-cursor")
        if from_uid != expect_cursor + 1:
            raise ReplayError(
                "range must start right after the cursor (from-uid == expect-cursor + 1), "
                "otherwise moving the cursor would skip messages"
            )

    settings = getattr(tenant, "reception_settings", None)
    if settings is None or not settings.has_guest_smtp_password or not settings.guest_contact_email:
        raise ReplayError("tenant has no guest IMAP credentials configured")
    settings.refresh_from_db(fields=["guest_imap_last_uid"])  # never trust a cached tenant object
    cursor_now = settings.guest_imap_last_uid
    if apply and cursor_now != expect_cursor:
        raise CursorMismatch(f"cursor is {cursor_now}, expected {expect_cursor}; nothing was done")

    report = ReplayReport(
        tenant_slug=tenant.slug,
        mode="apply" if apply else "dry-run",
        from_uid=from_uid,
        to_uid=to_uid,
        cursor_before=cursor_now,
    )
    connect = connect or _connect_imap

    if not apply:
        # Read-only end to end: no lock needed, nothing is written.
        _collect(tenant, settings, report, connect, expect_uidvalidity, accept_unmatched, apply=False)
        return report

    with guest_imap_single_flight(tenant.pk) as acquired:
        if not acquired:
            raise ReplayLocked("another poll/replay holds the tenant IMAP lock; nothing was done")
        candidates = _collect(
            tenant, settings, report, connect, expect_uidvalidity, accept_unmatched, apply=True
        )
        if report.blocking:
            raise ReplayError("refused: " + "; ".join(report.blocking))
        _apply(tenant, settings, report, candidates, expect_cursor=expect_cursor)

    _touch_versions(report)
    logger.info(
        "guest email replay applied",
        extra={
            "tenant_slug": tenant.slug,
            "from_uid": from_uid,
            "to_uid": to_uid,
            "counts": report.counts(),
            "reservations_touched": len(report.touched_reservations),
        },
    )
    return report


def _collect(
    tenant: Tenant,
    settings: TenantReceptionSettings,
    report: ReplayReport,
    connect: Callable[[TenantReceptionSettings], Any],
    expect_uidvalidity: int | None,
    accept_unmatched: bool,
    *,
    apply: bool,
) -> list[_Candidate]:
    uidvalidity, uidnext, fetched = _read_range(
        settings, from_uid=report.from_uid, to_uid=report.to_uid, connect=connect
    )
    report.uidvalidity, report.uidnext = uidvalidity, uidnext

    if expect_uidvalidity is not None and uidvalidity != expect_uidvalidity:
        report.blocking.append(f"UIDVALIDITY is {uidvalidity}, expected {expect_uidvalidity}")
    stored = settings.guest_imap_uidvalidity
    if stored is not None and uidvalidity != stored:
        # UIDs (and therefore the cursor) belong to another mailbox generation.
        report.blocking.append(f"UIDVALIDITY is {uidvalidity}, but the stored value is {stored}")
    if uidnext is None:
        report.blocking.append("server did not report UIDNEXT")
    elif report.to_uid > uidnext - 1:
        # Advancing the cursor past UIDNEXT-1 would skip mail that has not arrived yet.
        report.blocking.append(f"to-uid {report.to_uid} is beyond UIDNEXT-1 ({uidnext - 1})")

    seen: set[str] = set()
    candidates = [_evaluate(tenant, settings, f, seen) for f in fetched]
    report.items = [c.item for c in candidates]
    for c in candidates:
        if c.item.outcome in _ALWAYS_BLOCKING:
            report.blocking.append(f"uid {c.item.uid}: {c.item.outcome}")
        elif c.item.outcome == NO_RESERVATION and not accept_unmatched:
            report.blocking.append(
                f"uid {c.item.uid}: {NO_RESERVATION} (pass --accept-unmatched to skip it knowingly)"
            )
    return candidates


def _apply(
    tenant: Tenant,
    settings: TenantReceptionSettings,
    report: ReplayReport,
    candidates: list[_Candidate],
    *,
    expect_cursor: int | None,
) -> None:
    """All inserts and the cursor move happen in ONE transaction (all-or-nothing)."""
    inserted_reservations: list[int] = []
    with transaction.atomic():
        locked = TenantReceptionSettings.objects.select_for_update().get(pk=settings.pk)
        if locked.guest_imap_last_uid != expect_cursor:
            raise CursorMismatch(
                f"cursor changed to {locked.guest_imap_last_uid} (expected {expect_cursor}); rolled back"
            )
        for cand in candidates:
            if cand.item.outcome != WOULD_INSERT or cand.parsed is None:
                continue
            row = ingest_parsed_email(tenant, cand.parsed, side_effects=False)
            if row is None:
                cand.item.outcome = DUPLICATE
                continue
            cand.item.outcome = INSERTED
            inserted_reservations.append(row.reservation_id)
        locked.guest_imap_last_uid = report.to_uid
        locked.save(update_fields=["guest_imap_last_uid", "updated_at"])
    report.cursor_after = report.to_uid
    report.applied = True
    report.touched_reservations = sorted(set(inserted_reservations))


def _touch_versions(report: ReplayReport) -> None:
    """One MESSAGES bump per affected reservation, only after the commit succeeded."""
    done: list[int] = []
    for reservation_id in report.touched_reservations:
        try:
            touch_reservation_version(
                reservation_id, ReservationVersionScope.MESSAGES, reason="guest_email_replay"
            )
            done.append(reservation_id)
        except Exception:
            report.touch_failures.append(reservation_id)
            logger.exception("guest email replay version bump failed reservation_id=%s", reservation_id)
    report.touched_reservations = done


def format_report(report: ReplayReport) -> list[str]:
    """Human-readable lines. Contains ids, dates, outcomes and counts only — never content."""
    lines = [
        f"{'APPLY' if report.mode == 'apply' else 'DRY-RUN'} tenant={report.tenant_slug} "
        f"uid_range={report.from_uid}-{report.to_uid} uidvalidity={report.uidvalidity} "
        f"uidnext={report.uidnext} cursor_before={report.cursor_before}"
    ]
    for item in report.items:
        when = item.internaldate.isoformat() if item.internaldate else "-"
        extra = (
            f" reservation_id={item.reservation_id} reservation_status={item.reservation_status}"
            if item.reservation_id
            else ""
        )
        lines.append(f"  uid={item.uid} internaldate={when} outcome={item.outcome}{extra}")
    lines.append("summary: " + (", ".join(f"{k}={v}" for k, v in sorted(report.counts().items())) or "no messages"))
    if report.blocking:
        lines.append("apply would be REFUSED: " + "; ".join(report.blocking))
    if report.applied:
        lines.append(
            f"applied: cursor {report.cursor_before} -> {report.cursor_after}; "
            f"MESSAGES version bumped for reservations {report.touched_reservations}"
            + (f"; bump FAILED for {report.touch_failures}" if report.touch_failures else "")
        )
    else:
        lines.append("nothing was written (dry-run)" if report.mode == "dry-run" else "nothing was written")
    return lines
