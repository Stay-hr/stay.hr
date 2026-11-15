"""Retry messages recorded in the GuestImapIssue ledger (quarantined or still open).

For UIDs the IMAP cursor already moved past (or is waiting on). Read-only towards the mailbox
(EXAMINE, BODY.PEEK[]); content stays in memory and is never logged or printed. Dry-run by
default. By default retried messages are stored WITHOUT the live automation (no LLM, replies,
push, language/reservation updates) — they are late by definition; ``with_automation=True`` is
an explicit opt-in. Does not touch the cursor. Holds the tenant IMAP lock while applying.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from django.db import transaction

from apps.communications.guest_email_ingest import _connect_imap, ingest_parsed_email
from apps.communications.guest_email_replay import (
    DUPLICATE,
    FETCH_ERROR,
    INSERTED,
    NO_RESERVATION,
    PARSE_ERROR,
    WOULD_INSERT,
    _evaluate,
    _fetch_one,
)
from apps.communications.guest_imap_issues import MAILBOX_KINDS, mark_resolved
from apps.communications.guest_imap_lock import guest_imap_single_flight
from apps.communications.guest_imap_protocol import mailbox_status
from apps.communications.models import GuestImapIssue, GuestImapIssueStatus
from apps.reservations.models import ReservationVersionScope
from apps.reservations.reservation_version import touch_reservation_version
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

STALE = "stale_uidvalidity"
STILL_FAILING = "still_failing"


class RetryError(Exception):
    pass


@dataclass
class RetryItem:
    uid: int
    kind: str
    status: str
    attempts: int
    outcome: str = ""


@dataclass
class RetryReport:
    tenant_slug: str
    mode: str
    uidvalidity: int | None = None
    items: list[RetryItem] = field(default_factory=list)
    applied: bool = False
    touched_reservations: list[int] = field(default_factory=list)


def retry_issues(
    tenant: Tenant,
    *,
    apply: bool = False,
    uids: list[int] | None = None,
    with_automation: bool = False,
    connect: Callable[[Any], Any] | None = None,
) -> RetryReport:
    settings = getattr(tenant, "reception_settings", None)
    if settings is None or not settings.has_guest_smtp_password or not settings.guest_contact_email:
        raise RetryError("tenant has no guest IMAP credentials configured")
    settings.refresh_from_db(fields=["guest_imap_uidvalidity"])
    if settings.guest_imap_uidvalidity is None:
        raise RetryError("UIDVALIDITY is not initialised; run init_guest_imap_uidvalidity first")

    qs = GuestImapIssue.objects.filter(
        tenant=tenant, uid__gt=0, status__in=(GuestImapIssueStatus.OPEN, GuestImapIssueStatus.QUARANTINED)
    ).order_by("uid")
    if uids:
        qs = qs.filter(uid__in=uids)
    issues = [i for i in qs if i.kind not in MAILBOX_KINDS]
    report = RetryReport(tenant_slug=tenant.slug, mode="apply" if apply else "dry-run")
    if not issues:
        return report

    def run() -> None:
        _retry(tenant, settings, report, issues, apply, with_automation, connect or _connect_imap)

    if not apply:
        run()
        return report
    with guest_imap_single_flight(tenant.pk) as acquired:
        if not acquired:
            raise RetryError("another poll/replay holds the tenant IMAP lock; nothing was done")
        run()
    _touch(report, with_automation)
    return report


def _retry(tenant, settings, report, issues, apply, with_automation, connect) -> None:
    client = connect(settings)
    try:
        numbers = mailbox_status(client)
        report.uidvalidity = numbers.get("UIDVALIDITY")
        if report.uidvalidity != settings.guest_imap_uidvalidity:
            raise RetryError(
                f"server UIDVALIDITY {report.uidvalidity} != stored {settings.guest_imap_uidvalidity}: "
                "UIDs in the ledger cannot be compared with this mailbox"
            )
        typ, _ = client.select("INBOX", readonly=True)
        if typ != "OK":
            raise RetryError("could not open INBOX read-only")
        fetched = {i.uid: _fetch_one(client, i.uid) for i in issues if i.uidvalidity == report.uidvalidity}
    finally:
        try:
            client.logout()
        except Exception:
            pass

    seen: set[str] = set()
    inserted: list[int] = []
    for issue in issues:
        item = RetryItem(issue.uid, str(issue.kind), str(issue.status), issue.attempts)
        report.items.append(item)
        if issue.uidvalidity != report.uidvalidity:
            item.outcome = STALE
            continue
        cand = _evaluate(tenant, settings, fetched[issue.uid], seen)
        item.outcome = cand.item.outcome
        if cand.item.outcome in (FETCH_ERROR, PARSE_ERROR, NO_RESERVATION):
            item.outcome = f"{STILL_FAILING}:{cand.item.outcome}"
            continue
        if not apply:
            continue
        with transaction.atomic():
            if cand.item.outcome == WOULD_INSERT and cand.parsed is not None:
                row = ingest_parsed_email(
                    tenant, cand.parsed, notify=with_automation, side_effects=with_automation
                )
                if row is None:
                    item.outcome = DUPLICATE
                else:
                    item.outcome = INSERTED
                    inserted.append(row.reservation_id)
            mark_resolved(tenant, uid=issue.uid, uidvalidity=issue.uidvalidity)
    if apply:
        report.applied = True
        report.touched_reservations = sorted(set(inserted))


def _touch(report: RetryReport, with_automation: bool) -> None:
    if with_automation:
        return  # the live path already bumped the version per message
    done = []
    for rid in report.touched_reservations:
        try:
            touch_reservation_version(rid, ReservationVersionScope.MESSAGES, reason="guest_imap_issue_retry")
            done.append(rid)
        except Exception:
            logger.exception("guest imap issue retry version bump failed reservation_id=%s", rid)
    report.touched_reservations = done


def format_retry_report(r: RetryReport) -> list[str]:
    lines = [f"{'APPLY' if r.mode == 'apply' else 'DRY-RUN'} tenant={r.tenant_slug} uidvalidity={r.uidvalidity}"]
    for i in r.items:
        lines.append(f"  uid={i.uid} kind={i.kind} status={i.status} attempts={i.attempts} -> {i.outcome or '-'}")
    if not r.items:
        lines.append("no open or quarantined message issues")
    lines.append(
        "resolved: ids/outcomes above" if r.applied else "nothing was written"
        + (" (dry-run)" if r.mode == "dry-run" else "")
    )
    if r.touched_reservations:
        lines.append(f"MESSAGES version bumped for reservations {r.touched_reservations}")
    return lines
