"""Durable ledger for guest-inbox messages that could not be processed, and mailbox guards.

Cursor contract (see ``guest_email_ingest``): the IMAP cursor may only move past a message
that is either processed, legitimately skipped, or *durably recorded here*. While an issue is
``open`` the cursor waits and polling retries the UID; after the per-kind attempt limit the
issue becomes ``quarantined`` — the cursor is released (later mail is never blocked for good)
and the UID stays in this ledger for ``retry_guest_imap_issues``. Nothing is lost silently.

Only UIDs, kinds, counters and an exception *type name* are stored — never content.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from apps.communications.models import GuestImapIssue, GuestImapIssueKind, GuestImapIssueStatus
from apps.tenants.models import Tenant, TenantReceptionSettings

logger = logging.getLogger(__name__)

Kind = GuestImapIssueKind
Status = GuestImapIssueStatus

# Polls (2 min apart when scheduled) a message may hold the cursor before it is quarantined.
MAX_ATTEMPTS = {
    Kind.FETCH_FAILED: 10,  # transient network/server trouble
    Kind.INGEST_FAILED: 10,  # e.g. database hiccup
    Kind.NO_RESERVATION: 10,  # the reservation may still be imported (Channex)
    Kind.PARSE_FAILED: 2,  # deterministic: the same bytes fail again
}
MAILBOX_KINDS = frozenset(
    {Kind.UIDVALIDITY_CHANGED, Kind.UIDVALIDITY_UNINITIALIZED, Kind.UIDVALIDITY_UNAVAILABLE, Kind.CURSOR_AHEAD}
)


def record_failure(
    tenant: Tenant, *, uid: int, uidvalidity: int, kind: str, error_type: str = ""
) -> GuestImapIssue:
    """Upsert the ledger row for one message and count one more attempt (atomic)."""
    with transaction.atomic():
        GuestImapIssue.objects.get_or_create(
            tenant=tenant, uidvalidity=uidvalidity, uid=uid, defaults={"kind": kind}
        )
        issue = GuestImapIssue.objects.select_for_update().get(
            tenant=tenant, uidvalidity=uidvalidity, uid=uid
        )
        issue.attempts += 1
        issue.kind = kind
        issue.error_type = (error_type or "")[:64]
        issue.last_attempt_at = timezone.now()
        issue.resolved_at = None
        issue.status = Status.QUARANTINED if issue.attempts >= MAX_ATTEMPTS[kind] else Status.OPEN
        issue.save()
    return issue


def mark_resolved(tenant: Tenant, *, uid: int, uidvalidity: int) -> int:
    return GuestImapIssue.objects.filter(
        tenant=tenant, uidvalidity=uidvalidity, uid=uid, status__in=(Status.OPEN, Status.QUARANTINED)
    ).update(status=Status.RESOLVED, resolved_at=timezone.now())


def record_mailbox_issue(tenant: Tenant, *, kind: str, uidvalidity: int | None) -> GuestImapIssue:
    """One row per (tenant, server UIDVALIDITY); repeated blocked polls bump ``attempts``."""
    with transaction.atomic():
        key = {"tenant": tenant, "uidvalidity": uidvalidity or 0, "uid": 0}
        GuestImapIssue.objects.get_or_create(**key, defaults={"kind": kind})
        issue = GuestImapIssue.objects.select_for_update().get(**key)
        issue.attempts += 1
        issue.kind = kind
        issue.status = Status.OPEN
        issue.last_attempt_at = timezone.now()
        issue.resolved_at = None
        issue.save()
    return issue


def resolve_mailbox_issues(tenant: Tenant) -> None:
    GuestImapIssue.objects.filter(tenant=tenant, uid=0, status=Status.OPEN).update(
        status=Status.RESOLVED, resolved_at=timezone.now()
    )


def check_mailbox_state(tenant: Tenant, settings: TenantReceptionSettings, numbers: dict[str, int]) -> str | None:
    """Gate every poll. Returns None when polling may proceed, else the blocking kind.

    Never changes the cursor and never overwrites a stored UIDVALIDITY. The only automatic
    write is the first-ever adoption for a tenant whose cursor is still 0 (nothing to protect).
    A tenant that already has a cursor but no stored UIDVALIDITY must be initialised by an
    operator (``init_guest_imap_uidvalidity``): auto-accepting the server's value there would
    also accept a mailbox that was replaced behind our back.
    """
    settings.refresh_from_db(fields=["guest_imap_last_uid", "guest_imap_uidvalidity"])
    server = numbers.get("UIDVALIDITY")
    uidnext = numbers.get("UIDNEXT")
    stored = settings.guest_imap_uidvalidity
    cursor = settings.guest_imap_last_uid

    kind: str | None = None
    if server is None or uidnext is None:
        kind = Kind.UIDVALIDITY_UNAVAILABLE
    elif stored is None:
        if cursor == 0:
            adopted = TenantReceptionSettings.objects.filter(
                pk=settings.pk, guest_imap_uidvalidity__isnull=True, guest_imap_last_uid=0
            ).update(guest_imap_uidvalidity=server, updated_at=timezone.now())
            if adopted:
                settings.guest_imap_uidvalidity = server
                logger.info(
                    "guest imap uidvalidity adopted for a tenant with no cursor",
                    extra={"tenant_slug": tenant.slug, "uidvalidity": server},
                )
        else:
            kind = Kind.UIDVALIDITY_UNINITIALIZED
    elif stored != server:
        kind = Kind.UIDVALIDITY_CHANGED
    if kind is None and uidnext is not None and cursor > uidnext - 1:
        kind = Kind.CURSOR_AHEAD

    if kind is None:
        resolve_mailbox_issues(tenant)
        return None

    try:
        record_mailbox_issue(tenant, kind=kind, uidvalidity=server)
    except Exception:  # the refusal itself must not depend on the ledger being writable
        logger.exception("guest imap mailbox issue could not be recorded")
    logger.error(
        "guest imap poll REFUSED: %s (cursor and UIDVALIDITY left untouched; operator action required)",
        kind,
        extra={
            "tenant_slug": tenant.slug,
            "reason": str(kind),
            "stored_uidvalidity": stored,
            "server_uidvalidity": server,
            "server_uidnext": uidnext,
            "cursor": cursor,
        },
    )
    return str(kind)
