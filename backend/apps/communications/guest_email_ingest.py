"""Poll tenant IMAP inbox for Booking.com guest email replies."""

from __future__ import annotations

import email
import imaplib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime

from django.db import transaction
from django.utils import timezone

from apps.communications.canonical_store import record_canonical_source
from apps.communications.conversation_ingest_status import mark_conversation_ingest
from apps.communications.guest_imap_issues import (
    check_mailbox_state,
    mark_resolved,
    record_failure,
)
from apps.communications.guest_imap_lock import guest_imap_single_flight
from apps.communications.guest_imap_protocol import mailbox_status, search_new_uids
from apps.communications.models import GuestImapIssueKind, GuestImapIssueStatus
from apps.communications.models import GuestInboundMessage, GuestMessageChannel
from apps.reservations.models import Reservation, ReservationVersionScope
from apps.reservations.reservation_version import touch_reservation_version
from apps.tenants.models import Tenant, TenantReceptionSettings
from apps.tenants.smtp import imap_host_for_email

logger = logging.getLogger(__name__)

IMAP_PORT = 993
BOOKING_GUEST_DOMAIN = "@guest.booking.com"
REPLY_LINE_MARKERS = (
    "please type your reply above this line",
    "##- please type your reply above this line",
)
CONFIRMATION_NUMBER_RE = re.compile(
    r"(?:Confirmation|Booking)\s+number\s*:\s*(\d{6,12})",
    re.IGNORECASE,
)
RES_ID_RE = re.compile(r"res_id=(\d{6,12})", re.IGNORECASE)
SAID_BLOCK_RE = re.compile(
    r"\bsaid:\s*\n+(.*?)(?:\n\s*Reply\b|\nReservation details|\n\s*Booking number:|\Z)",
    re.IGNORECASE | re.DOTALL,
)
RE_SUBJECT_LINE_RE = re.compile(r"^Re:\s", re.IGNORECASE)


@dataclass
class ParsedGuestEmail:
    message_id: str
    raw_from: str
    from_email: str
    subject: str
    body_text: str
    booking_code: str
    received_at: datetime | None


@dataclass
class IngestResult:
    ingested: int = 0
    skipped: int = 0
    errors: int = 0
    max_uid: int = 0
    # True when another poll/replay holds the tenant's IMAP lock and this call did nothing.
    skipped_locked: bool = False
    # Set when the poll refused to run (UIDVALIDITY changed/uninitialised/unavailable, cursor
    # ahead of the mailbox). The cursor was not touched; see GuestImapIssue (uid=0).
    blocked_reason: str = ""
    # Messages that hold the cursor (open) / were released but kept in the ledger (quarantined).
    issues_open: int = 0
    quarantined: int = 0


def _normalize_message_id(value: str) -> str:
    text = (value or "").strip()
    if text.startswith("<") and text.endswith(">"):
        return text[1:-1].strip()
    return text


def _header_addresses(msg: Message, *names: str) -> list[str]:
    values: list[str] = []
    for name in names:
        raw = msg.get(name, "") or ""
        if raw:
            values.append(str(raw))
    return values


def _address_contains_booking_guest(msg: Message) -> bool:
    blob = " ".join(_header_addresses(msg, "From", "Sender", "Reply-To")).lower()
    return BOOKING_GUEST_DOMAIN in blob


def _is_own_outbound_copy(msg: Message, guest_contact_email: str) -> bool:
    contact = (guest_contact_email or "").strip().lower()
    blob = " ".join(_header_addresses(msg, "From", "Sender")).lower()
    if contact and contact in blob:
        return True
    if "room_reservations@" in blob:
        return True
    return False


def _extract_plain_text(msg: Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        return payload.decode(charset, errors="replace")
                    except LookupError:
                        return payload.decode("utf-8", errors="replace")
        return ""
    payload = msg.get_payload(decode=True)
    if not payload:
        return str(msg.get_payload() or "")
    charset = msg.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def extract_booking_code(*, body_text: str, subject: str) -> str:
    for source in (body_text, subject):
        match = CONFIRMATION_NUMBER_RE.search(source or "")
        if match:
            return match.group(1)
        match = RES_ID_RE.search(source or "")
        if match:
            return match.group(1)
    return ""


def extract_guest_body_text(body_text: str) -> str:
    text = (body_text or "").replace("\r\n", "\n").strip()
    if not text:
        return ""

    match = SAID_BLOCK_RE.search(text)
    if match:
        block = match.group(1).strip()
        lines: list[str] = []
        for line in block.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if RE_SUBJECT_LINE_RE.match(stripped):
                continue
            lines.append(stripped)
        if lines:
            return "\n".join(lines).strip()

    lower = text.lower()
    for marker in REPLY_LINE_MARKERS:
        idx = lower.find(marker)
        if idx > 0:
            above = text[:idx].strip()
            if above:
                return above

    return ""


def parse_guest_email_bytes(raw: bytes) -> ParsedGuestEmail | None:
    msg = email.message_from_bytes(raw)
    if not _address_contains_booking_guest(msg):
        return None

    plain = _extract_plain_text(msg)
    guest_text = extract_guest_body_text(plain)
    if not guest_text:
        return None

    subject = str(msg.get("Subject", "") or "").strip()
    booking_code = extract_booking_code(body_text=plain, subject=subject)
    if not booking_code:
        return None

    raw_from = str(msg.get("From", "") or "").strip()
    _, from_email = parseaddr(raw_from)
    message_id = _normalize_message_id(str(msg.get("Message-ID", "") or ""))

    received_at: datetime | None = None
    date_raw = msg.get("Date")
    if date_raw:
        try:
            received_at = parsedate_to_datetime(str(date_raw))
            if timezone.is_naive(received_at):
                received_at = timezone.make_aware(received_at, dt_timezone.utc)
        except (TypeError, ValueError, OverflowError):
            received_at = None

    return ParsedGuestEmail(
        message_id=message_id,
        raw_from=raw_from,
        from_email=(from_email or "").strip(),
        subject=subject,
        body_text=guest_text,
        booking_code=booking_code,
        received_at=received_at,
    )


def match_reservation(tenant: Tenant, booking_code: str) -> Reservation | None:
    code = (booking_code or "").strip()
    if not code:
        return None
    return (
        Reservation.objects.filter(tenant=tenant, booking_code=code)
        .order_by("-pk")
        .first()
    )


def ingest_parsed_email(
    tenant: Tenant,
    parsed: ParsedGuestEmail,
    *,
    notify: bool = True,
    side_effects: bool = True,
) -> GuestInboundMessage | None:
    """Store one guest email and (by default) run the live inbound automation.

    ``side_effects=True`` (default, unchanged behaviour): language update, push
    notification (``notify``), arrival/invoice/parking auto-handlers (LLM, replies,
    reservation changes) and the reservation version bump.

    ``side_effects=False`` (historical replay): persist only — dedupe, reservation
    match, the ``GuestInboundMessage`` with its original ``received_at`` and the
    canonical dual-write (``record_canonical_source`` never pushes). ``notify`` is
    ignored. Nothing is sent, no LLM is reached, no reservation or conversation
    state is modified and no version is bumped; the replay command bumps the
    MESSAGES version once per reservation after it commits.
    """
    if parsed.message_id:
        existing = GuestInboundMessage.objects.filter(
            tenant=tenant,
            message_id=parsed.message_id,
        ).first()
        if existing is not None:
            record_canonical_source(existing)
            return None

    reservation = match_reservation(tenant, parsed.booking_code)
    if reservation is None:
        logger.warning(
            "guest email ingest: no reservation for booking_code",
            extra={"tenant_slug": tenant.slug, "booking_code": parsed.booking_code},
        )
        return None

    with transaction.atomic():
        row = GuestInboundMessage.objects.create(
            tenant=tenant,
            reservation=reservation,
            channel=GuestMessageChannel.EMAIL,
            body_text=parsed.body_text,
            from_email=parsed.from_email,
            raw_from=parsed.raw_from,
            subject=parsed.subject[:200],
            message_id=parsed.message_id,
            received_at=parsed.received_at,
        )
        record_canonical_source(row)

    if not side_effects:
        return row

    from apps.communications.guest_language_inbound import on_guest_inbound_message

    on_guest_inbound_message(
        reservation,
        body=parsed.body_text,
        channel="email",
        received_at=parsed.received_at,
    )

    if notify:
        from apps.core.tasks import notify_guest_message_inbound

        notify_guest_message_inbound.delay(
            reservation.pk,
            channel="email",
            body_preview=parsed.body_text[:200],
        )

    from apps.communications.guest_arrival_inbound import maybe_handle_guest_arrival_inbound
    from apps.communications.guest_parking_inbound import maybe_handle_guest_parking_inbound

    arrival_result = maybe_handle_guest_arrival_inbound(
        reservation,
        parsed.body_text,
        channel="email",
    )
    if arrival_result is None:
        from apps.communications.guest_invoice_inbound import (
            maybe_handle_guest_invoice_inbound,
        )

        invoice_result = maybe_handle_guest_invoice_inbound(
            reservation,
            parsed.body_text,
            channel="email",
        )
        if invoice_result is None:
            maybe_handle_guest_parking_inbound(
                reservation,
                parsed.body_text,
                channel="email",
            )

    touch_reservation_version(
        reservation.pk,
        ReservationVersionScope.MESSAGES,
        reason="email_inbound",
    )

    logger.info(
        "guest email ingested",
        extra={
            "tenant_slug": tenant.slug,
            "reservation_id": reservation.pk,
            "booking_code": parsed.booking_code,
            "message_id": parsed.message_id,
        },
    )
    return row


def _connect_imap(settings: TenantReceptionSettings) -> imaplib.IMAP4_SSL:
    email_addr = (settings.guest_contact_email or "").strip()
    password = settings.get_guest_smtp_password()
    host = imap_host_for_email(email_addr)
    if not email_addr or not password or not host:
        raise ValueError("guest_imap_not_configured")

    client = imaplib.IMAP4_SSL(host, IMAP_PORT)
    client.login(email_addr, password)
    return client


def poll_tenant_guest_inbox(
    tenant: Tenant,
    *,
    since_uid: int | None = None,
    notify: bool = True,
) -> IngestResult:
    """Fetch new INBOX messages and ingest Booking.com guest replies."""
    settings = getattr(tenant, "reception_settings", None)
    if settings is None:
        return IngestResult()

    if not settings.guest_imap_enabled or not settings.has_guest_smtp_password:
        return IngestResult()

    start_uid = since_uid if since_uid is not None else settings.guest_imap_last_uid

    # Single-flight per tenant, shared with replay_guest_email: a second poll (beat tick,
    # CLI, replay) must not race the first on the same mailbox/cursor/auto-replies.
    with guest_imap_single_flight(tenant.pk) as acquired:
        if not acquired:
            logger.info(
                "guest imap poll skipped: tenant imap lock is held",
                extra={"tenant_slug": tenant.slug},
            )
            return IngestResult(max_uid=start_uid, skipped_locked=True)
        return _poll_tenant_guest_inbox_locked(tenant, settings, start_uid, notify=notify)


# Upper bound of messages handled per poll: keeps one poll (and the tenant lock) short after a
# long outage; the rest is picked up by the next poll. Per-message logic is unchanged.
POLL_BATCH_LIMIT = 100

_PASS = "pass"  # the cursor may move past this message
_HOLD = "hold"  # the cursor must wait: recorded in the ledger, retried by the next poll


def _message_already_stored(tenant: Tenant, parsed: ParsedGuestEmail) -> bool:
    return bool(parsed.message_id) and GuestInboundMessage.objects.filter(
        tenant=tenant, message_id=parsed.message_id
    ).exists()


def _advance_cursor(settings: TenantReceptionSettings, uid: int) -> None:
    if uid > settings.guest_imap_last_uid:
        settings.guest_imap_last_uid = uid
        settings.save(update_fields=["guest_imap_last_uid", "updated_at"])


def _poll_tenant_guest_inbox_locked(
    tenant: Tenant,
    settings: TenantReceptionSettings,
    start_uid: int,
    *,
    notify: bool,
) -> IngestResult:
    result = IngestResult(max_uid=start_uid)

    try:
        client = _connect_imap(settings)
    except Exception as exc:
        logger.exception(
            "guest imap connect failed",
            extra={"tenant_slug": tenant.slug, "error": str(exc)},
        )
        result.errors += 1
        return result

    try:
        # 1. Mailbox identity first: UIDs are only comparable within one UIDVALIDITY.
        numbers = mailbox_status(client)
        blocked = check_mailbox_state(tenant, settings, numbers)
        if blocked:
            result.blocked_reason = blocked
            result.errors += 1
            return result  # cursor and UIDVALIDITY untouched; no ingest stamp

        client.select("INBOX")
        mark_conversation_ingest("email", "poll")
        uidvalidity = numbers["UIDVALIDITY"]

        # 2. Only UIDs above the starting point, never an open-ended ``n:*`` range.
        uid_list = search_new_uids(client, after_uid=start_uid, uidnext=numbers["UIDNEXT"])
        if not uid_list:
            return result

        # 3. Process in UID order. The cursor follows the longest prefix of messages that are
        #    processed/skipped/quarantined; a message that is still being retried holds it, but
        #    never stops the messages after it from being processed (dedupe makes re-polls safe).
        frontier = start_uid
        holding = False
        for uid in uid_list[:POLL_BATCH_LIMIT]:
            outcome = _process_uid(
                client, tenant, settings, uid, uidvalidity, notify=notify, result=result
            )
            if outcome == _HOLD:
                holding = True
            elif not holding:
                frontier = uid
                _advance_cursor(settings, frontier)  # durable per message: a killed poll resumes here
        result.max_uid = max(start_uid, frontier)
    except Exception as exc:
        logger.exception(
            "guest imap poll failed",
            extra={"tenant_slug": tenant.slug, "error": str(exc)},
        )
        result.errors += 1
    finally:
        try:
            client.logout()
        except Exception:
            pass

    return result


def _process_uid(
    client,
    tenant: Tenant,
    settings: TenantReceptionSettings,
    uid: int,
    uidvalidity: int,
    *,
    notify: bool,
    result: IngestResult,
) -> str:
    """Handle one message. Per-message problems never raise; they go to the ledger."""

    def fail(kind: str, error_type: str) -> str:
        issue = record_failure(
            tenant, uid=uid, uidvalidity=uidvalidity, kind=kind, error_type=error_type
        )
        if kind != GuestImapIssueKind.NO_RESERVATION:
            result.errors += 1
        logger.warning(
            "guest imap message not processed",
            extra={
                "tenant_slug": tenant.slug,
                "uid": uid,
                "kind": str(kind),
                "attempts": issue.attempts,
                "status": str(issue.status),
                "error_type": error_type,
            },
        )
        if issue.status == GuestImapIssueStatus.OPEN:
            result.issues_open += 1
            return _HOLD
        result.quarantined += 1
        return _PASS  # durably recorded and retried enough: the cursor may pass

    def done() -> str:
        mark_resolved(tenant, uid=uid, uidvalidity=uidvalidity)
        return _PASS

    try:
        status, fetched = client.uid("FETCH", str(uid), "(RFC822)")
    except Exception as exc:
        return fail(GuestImapIssueKind.FETCH_FAILED, type(exc).__name__)
    raw_part = fetched[0] if status == "OK" and fetched else None
    raw_bytes = raw_part[1] if isinstance(raw_part, tuple) else None
    if not raw_bytes:
        return fail(GuestImapIssueKind.FETCH_FAILED, "EmptyResponse")

    try:
        msg = email.message_from_bytes(raw_bytes)
        if _is_own_outbound_copy(msg, settings.guest_contact_email):
            result.skipped += 1
            return done()
        if not _address_contains_booking_guest(msg):
            result.skipped += 1
            return done()
        parsed = parse_guest_email_bytes(raw_bytes)
    except Exception as exc:
        return fail(GuestImapIssueKind.PARSE_FAILED, type(exc).__name__)
    if parsed is None:
        result.skipped += 1
        return done()

    try:
        row = ingest_parsed_email(tenant, parsed, notify=notify)
    except Exception as exc:
        return fail(GuestImapIssueKind.INGEST_FAILED, type(exc).__name__)
    if row is not None:
        result.ingested += 1
        return done()
    if _message_already_stored(tenant, parsed):
        result.skipped += 1  # dedupe: already ingested earlier (e.g. a re-poll while the cursor waited)
        return done()
    result.skipped += 1  # unchanged counter semantics: no reservation counted as skipped
    return fail(GuestImapIssueKind.NO_RESERVATION, "")
