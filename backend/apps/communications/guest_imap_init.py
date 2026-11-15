"""Explicit, verified initialisation of ``guest_imap_uidvalidity`` for an existing cursor.

Polling refuses to run for a tenant that has a cursor but no stored UIDVALIDITY, because
adopting "whatever the server says now" would also accept a mailbox that was replaced
behind our back. This command is the only way to store it for such a tenant, and it never
trusts the server alone: the operator must assert both the UIDVALIDITY and the cursor, the
server must confirm the UIDVALIDITY, and the cursor must not be beyond the mailbox.

Read-only towards the mailbox (STATUS, EXAMINE, SEARCH, INTERNALDATE). Dry-run by default.
No message content is read.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.communications.guest_email_ingest import _connect_imap
from apps.communications.guest_email_replay import _parse_internaldate
from apps.communications.guest_imap_lock import guest_imap_single_flight
from apps.communications.guest_imap_protocol import mailbox_status
from apps.communications.models import GuestInboundMessage
from apps.tenants.models import Tenant, TenantReceptionSettings

logger = logging.getLogger(__name__)


class InitError(Exception):
    """Refused before anything was written."""


@dataclass
class InitReport:
    tenant_slug: str
    mode: str
    server_uidvalidity: int | None = None
    server_uidnext: int | None = None
    stored_uidvalidity: int | None = None
    cursor: int | None = None
    cursor_uid_present: bool | None = None
    cursor_uid_internaldate: datetime | None = None
    latest_ingested_received_at: datetime | None = None
    blocking: list[str] = field(default_factory=list)
    already_initialised: bool = False
    applied: bool = False


def initialise_uidvalidity(
    tenant: Tenant,
    *,
    expect_uidvalidity: int,
    expect_cursor: int,
    apply: bool = False,
    replace_from: int | None = None,
    connect: Callable[[TenantReceptionSettings], Any] | None = None,
) -> InitReport:
    settings = getattr(tenant, "reception_settings", None)
    if settings is None or not settings.has_guest_smtp_password or not settings.guest_contact_email:
        raise InitError("tenant has no guest IMAP credentials configured")
    settings.refresh_from_db(fields=["guest_imap_last_uid", "guest_imap_uidvalidity"])
    report = InitReport(
        tenant_slug=tenant.slug,
        mode="apply" if apply else "dry-run",
        stored_uidvalidity=settings.guest_imap_uidvalidity,
        cursor=settings.guest_imap_last_uid,
    )
    report.latest_ingested_received_at = GuestInboundMessage.objects.filter(
        tenant=tenant, channel="email"
    ).aggregate(m=Max("received_at"))["m"]

    _read_server(settings, report, connect or _connect_imap)
    _judge(report, expect_uidvalidity, expect_cursor, replace_from)

    if not apply or report.already_initialised:
        return report
    if report.blocking:
        raise InitError("refused: " + "; ".join(report.blocking))

    with guest_imap_single_flight(tenant.pk) as acquired:
        if not acquired:
            raise InitError("another poll/replay holds the tenant IMAP lock; nothing was done")
        with transaction.atomic():
            locked = TenantReceptionSettings.objects.select_for_update().get(pk=settings.pk)
            if locked.guest_imap_last_uid != expect_cursor:
                raise InitError(f"cursor changed to {locked.guest_imap_last_uid}; nothing was done")
            if locked.guest_imap_uidvalidity != report.stored_uidvalidity:
                raise InitError("stored UIDVALIDITY changed meanwhile; nothing was done")
            locked.guest_imap_uidvalidity = expect_uidvalidity
            locked.save(update_fields=["guest_imap_uidvalidity", "updated_at"])
    report.applied = True
    report.stored_uidvalidity = expect_uidvalidity
    logger.info(
        "guest imap uidvalidity initialised",
        extra={"tenant_slug": tenant.slug, "uidvalidity": expect_uidvalidity, "cursor": expect_cursor},
    )
    return report


def _read_server(settings: TenantReceptionSettings, report: InitReport, connect) -> None:
    client = connect(settings)
    try:
        numbers = mailbox_status(client)
        report.server_uidvalidity = numbers.get("UIDVALIDITY")
        report.server_uidnext = numbers.get("UIDNEXT")
        typ, _ = client.select("INBOX", readonly=True)
        if typ != "OK":
            raise InitError("could not open INBOX read-only")
        cursor = report.cursor or 0
        if cursor > 0:
            typ, data = client.uid("SEARCH", None, f"UID {cursor}:{cursor}")
            found = [int(x) for x in (data[0].split() if typ == "OK" and data and data[0] else [])]
            report.cursor_uid_present = cursor in found
            if report.cursor_uid_present:
                typ, data = client.uid("FETCH", str(cursor), "(INTERNALDATE)")
                chunks = [p[0] if isinstance(p, tuple) else p for p in (data or []) if p]
                report.cursor_uid_internaldate = _parse_internaldate([c for c in chunks if isinstance(c, bytes)])
    finally:
        try:
            client.logout()
        except Exception:
            pass


def _judge(report: InitReport, expect_uidvalidity: int, expect_cursor: int, replace_from: int | None) -> None:
    stored = report.stored_uidvalidity
    if stored is not None:
        if replace_from is None:
            if stored == expect_uidvalidity:
                report.already_initialised = True
                return
            report.blocking.append(
                f"already initialised with {stored}; changing it needs --replace-from {stored} "
                "and a deliberate decision about the cursor"
            )
        elif replace_from != stored:
            report.blocking.append(f"--replace-from {replace_from} does not match the stored value {stored}")
    elif replace_from is not None:
        report.blocking.append("--replace-from given but no UIDVALIDITY is stored")

    if report.server_uidvalidity is None or report.server_uidnext is None:
        report.blocking.append("server did not report UIDVALIDITY/UIDNEXT")
    elif report.server_uidvalidity != expect_uidvalidity:
        report.blocking.append(
            f"server UIDVALIDITY is {report.server_uidvalidity}, operator asserted {expect_uidvalidity}"
        )
    if report.cursor != expect_cursor:
        report.blocking.append(f"cursor is {report.cursor}, operator asserted {expect_cursor}")
    if report.server_uidnext is not None and (report.cursor or 0) > report.server_uidnext - 1:
        report.blocking.append(
            f"cursor {report.cursor} is beyond the mailbox (UIDNEXT-1 = {report.server_uidnext - 1}): "
            "the mailbox looks replaced"
        )


def format_init_report(r: InitReport) -> list[str]:
    def iso(v):
        return v.isoformat() if v else "-"

    lines = [
        f"{'APPLY' if r.mode == 'apply' else 'DRY-RUN'} tenant={r.tenant_slug} "
        f"server_uidvalidity={r.server_uidvalidity} server_uidnext={r.server_uidnext} "
        f"stored_uidvalidity={r.stored_uidvalidity} cursor={r.cursor}",
        f"  cursor UID present on server: {r.cursor_uid_present}; its INTERNALDATE: {iso(r.cursor_uid_internaldate)}",
        f"  newest ingested e-mail received_at: {iso(r.latest_ingested_received_at)}  "
        "(compare the two dates above: they should be about the same moment)",
    ]
    if r.already_initialised:
        lines.append("already initialised with the asserted value: nothing to do")
    elif r.blocking:
        lines.append("apply would be REFUSED: " + "; ".join(r.blocking))
    elif r.applied:
        lines.append(f"applied: guest_imap_uidvalidity = {r.stored_uidvalidity}")
    else:
        lines.append("checks passed; nothing was written (dry-run) — re-run with --apply")
    return lines
