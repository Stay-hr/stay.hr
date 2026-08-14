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
) -> GuestInboundMessage | None:
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
        client.select("INBOX")
        mark_conversation_ingest("email", "poll")
        search_from = max(start_uid, 0) + 1
        status, data = client.uid("SEARCH", None, f"UID {search_from}:*")
        if status != "OK" or not data or not data[0]:
            return result

        uid_list = [int(item) for item in data[0].split() if item]
        if not uid_list:
            return result

        for uid in uid_list:
            result.max_uid = max(result.max_uid, uid)
            status, fetched = client.uid("FETCH", str(uid), "(RFC822)")
            if status != "OK" or not fetched or not fetched[0]:
                result.errors += 1
                continue

            raw_part = fetched[0]
            raw_bytes = raw_part[1] if isinstance(raw_part, tuple) else None
            if not raw_bytes:
                result.errors += 1
                continue

            msg = email.message_from_bytes(raw_bytes)
            if _is_own_outbound_copy(msg, settings.guest_contact_email):
                result.skipped += 1
                continue
            if not _address_contains_booking_guest(msg):
                result.skipped += 1
                continue

            parsed = parse_guest_email_bytes(raw_bytes)
            if parsed is None:
                result.skipped += 1
                continue

            row = ingest_parsed_email(tenant, parsed, notify=notify)
            if row is None:
                result.skipped += 1
            else:
                result.ingested += 1

        if result.max_uid > settings.guest_imap_last_uid:
            settings.guest_imap_last_uid = result.max_uid
            settings.save(update_fields=["guest_imap_last_uid", "updated_at"])
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
