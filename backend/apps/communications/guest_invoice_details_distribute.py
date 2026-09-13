"""Send guest invoice-details form link (WhatsApp / email / Booking)."""

from __future__ import annotations

import logging

from apps.communications.guest_compose import (
    HINT_GUEST_INVOICE_DETAILS_LINK,
    guest_invoice_details_link_email_subject,
    render_guest_invoice_details_link_email_html,
    render_guest_invoice_details_link_message,
)
from apps.communications.guest_email import _guest_recipient
from apps.communications.guest_language_context import LanguageMode
from apps.communications.guest_language_resolver import GuestLanguageResolver
from apps.communications.guest_message_send import (
    send_guest_email_with_timeline_record,
    send_guest_message,
)
from apps.communications.models import (
    GuestMessageChannel,
    GuestMessageDraft,
    GuestMessageIntent,
    GuestOutboundMessageStatus,
)
from apps.reservations.guest_invoice_details_access import (
    build_guest_invoice_details_url,
    ensure_active_invoice_details_access,
)
from apps.reservations.models import GuestInvoiceDetailsAccessCreatedFrom, Reservation

logger = logging.getLogger(__name__)

VALID_INVOICE_DETAILS_CHANNELS = frozenset(
    {
        GuestMessageChannel.WHATSAPP,
        GuestMessageChannel.EMAIL,
        GuestMessageChannel.BOOKING,
    }
)

_CHANNEL_CREATED_FROM = {
    GuestMessageChannel.WHATSAPP: GuestInvoiceDetailsAccessCreatedFrom.WHATSAPP,
    GuestMessageChannel.EMAIL: GuestInvoiceDetailsAccessCreatedFrom.EMAIL,
    GuestMessageChannel.BOOKING: GuestInvoiceDetailsAccessCreatedFrom.BOOKING,
}


def _outbound_looks_sent(outbound, draft: GuestMessageDraft) -> bool:
    sent = False
    if hasattr(outbound, "status"):
        sent = outbound.status == GuestOutboundMessageStatus.SENT
        if not sent:
            sent = getattr(outbound, "status", "") == "sent"
    if not sent:
        draft.refresh_from_db(fields=["sent_at"])
        sent = draft.sent_at is not None
    return sent


def send_guest_invoice_details_link(
    reservation: Reservation,
    *,
    channel: str,
    created_from: str | None = None,
    access_created_from: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Ensure invoice-details access and send the form URL on ``channel``."""
    base: dict = {
        "reservation_id": reservation.pk,
        "hint": HINT_GUEST_INVOICE_DETAILS_LINK,
        "channel": channel,
    }
    if created_from is not None:
        base["created_from"] = created_from

    if channel not in VALID_INVOICE_DETAILS_CHANNELS:
        return {**base, "status": "skipped", "reason": "unknown_channel"}

    if channel == GuestMessageChannel.EMAIL and not _guest_recipient(reservation):
        return {**base, "status": "skipped", "reason": "no_email"}

    token_from = (
        access_created_from
        or _CHANNEL_CREATED_FROM.get(channel, GuestInvoiceDetailsAccessCreatedFrom.SYSTEM)
    )
    try:
        access = ensure_active_invoice_details_access(
            reservation,
            created_from=token_from,
        )
    except ValueError as exc:
        return {**base, "status": "skipped", "reason": str(exc)}

    invoice_details_url = build_guest_invoice_details_url(access, reservation)
    body = render_guest_invoice_details_link_message(
        reservation,
        invoice_details_url=invoice_details_url,
    )
    if not (body or "").strip():
        return {**base, "status": "skipped", "reason": "empty_body"}

    if dry_run:
        return {
            **base,
            "status": "dry_run",
            "invoice_details_url": invoice_details_url,
            "access_id": access.pk,
        }

    ctx = GuestLanguageResolver.resolve(reservation, mode=LanguageMode.PROACTIVE)
    draft = GuestMessageDraft.objects.create(
        tenant_id=reservation.tenant_id,
        reservation=reservation,
        intent=GuestMessageIntent.CUSTOM,
        hint=HINT_GUEST_INVOICE_DETAILS_LINK,
        llm_body_text=body,
        final_body_text="",
        language=ctx.language[:8],
        language_source=ctx.source.value,
        language_reason=(ctx.reason or "")[:255],
        channel=channel,
    )

    try:
        if channel == GuestMessageChannel.EMAIL:
            outbound = send_guest_email_with_timeline_record(
                reservation,
                body,
                subject=guest_invoice_details_link_email_subject(reservation),
                body_html=render_guest_invoice_details_link_email_html(
                    reservation,
                    invoice_details_url=invoice_details_url,
                ),
                draft=draft,
                intent=GuestMessageIntent.CUSTOM,
                hint=HINT_GUEST_INVOICE_DETAILS_LINK,
            )
        else:
            outbound = send_guest_message(
                reservation=reservation,
                draft=draft,
                channel=channel,
                body_text=body,
                api_application=None,
            )
    except Exception as exc:
        logger.exception(
            "guest invoice details link send failed reservation_id=%s channel=%s",
            reservation.pk,
            channel,
        )
        return {
            **base,
            "status": "failed",
            "draft_id": draft.pk,
            "invoice_details_url": invoice_details_url,
            "access_id": access.pk,
            "error": str(exc),
        }

    sent = _outbound_looks_sent(outbound, draft)
    return {
        **base,
        "status": "sent" if sent else "queued",
        "draft_id": draft.pk,
        "invoice_details_url": invoice_details_url,
        "access_id": access.pk,
    }
