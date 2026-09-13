"""Deliver the public invoice portal link after CIS returns a JIR.

Last inbound channel plus usable email (not OTA relay). Portal URL only.
"""

from __future__ import annotations

import logging

from apps.billing.models import Invoice
from apps.communications.guest_compose import HINT_INVOICE_LINK
from apps.communications.guest_email import _language_for_reservation
from apps.communications.guest_language_context import LanguageMode
from apps.communications.guest_language_resolver import GuestLanguageResolver
from apps.communications.guest_message_send import (
    build_message_channels,
    send_guest_message,
)
from apps.communications.invoice_email import (
    _public_invoice_url,
    resolve_invoice_recipient,
    send_invoice_email,
)
from apps.communications.models import (
    GuestMessageChannel,
    GuestMessageDraft,
    GuestMessageIntent,
    GuestOutboundMessageStatus,
)
from apps.reservations.models import Reservation

logger = logging.getLogger(__name__)

VALID_INVOICE_CHANNELS = frozenset(
    {
        GuestMessageChannel.EMAIL,
        GuestMessageChannel.WHATSAPP,
        GuestMessageChannel.BOOKING,
    }
)


def resolve_invoice_delivery_channels(
    *,
    last_channel: str,
    usable_email: bool,
    booking_available: bool,
    whatsapp_session_open: bool,
) -> list[str]:
    """Pick outbound channels: last inbound, with WA/email/booking fallbacks."""
    last = (last_channel or "").strip()
    chosen: list[str] = []

    if last == GuestMessageChannel.EMAIL:
        if usable_email:
            chosen.append(GuestMessageChannel.EMAIL)
        elif booking_available:
            chosen.append(GuestMessageChannel.BOOKING)
        elif whatsapp_session_open:
            chosen.append(GuestMessageChannel.WHATSAPP)
    elif last == GuestMessageChannel.WHATSAPP:
        if whatsapp_session_open:
            chosen.append(GuestMessageChannel.WHATSAPP)
        elif usable_email:
            chosen.append(GuestMessageChannel.EMAIL)
        elif booking_available:
            chosen.append(GuestMessageChannel.BOOKING)
    elif last == GuestMessageChannel.BOOKING:
        if booking_available:
            chosen.append(GuestMessageChannel.BOOKING)
        elif usable_email:
            chosen.append(GuestMessageChannel.EMAIL)
        elif whatsapp_session_open:
            chosen.append(GuestMessageChannel.WHATSAPP)
    elif usable_email:
        chosen.append(GuestMessageChannel.EMAIL)
    elif booking_available:
        chosen.append(GuestMessageChannel.BOOKING)
    elif whatsapp_session_open:
        chosen.append(GuestMessageChannel.WHATSAPP)

    if usable_email and GuestMessageChannel.EMAIL not in chosen:
        chosen.append(GuestMessageChannel.EMAIL)
    return chosen


def render_invoice_link_message(invoice: Invoice) -> str:
    reservation = invoice.reservation
    lang = _language_for_reservation(reservation)
    url = _public_invoice_url(invoice)
    property_name = reservation.property.name
    booking = reservation.booking_code or str(reservation.pk)
    if lang == "hr":
        return (
            f"Pozdrav {invoice.buyer_name},\n\n"
            f"račun za boravak u {property_name}.\n\n"
            f"Broj računa: {invoice.invoice_number}\n"
            f"Rezervacija: {booking}\n\n"
            f"{url}\n"
        )
    return (
        f"Hello {invoice.buyer_name},\n\n"
        f"here is your invoice for {property_name}.\n\n"
        f"Invoice: {invoice.invoice_number}\n"
        f"Reservation: {booking}\n\n"
        f"{url}\n"
    )


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


def _send_on_channel(invoice: Invoice, channel: str) -> dict:
    reservation = invoice.reservation
    if channel == GuestMessageChannel.EMAIL:
        result = send_invoice_email(invoice.pk)
        return {"channel": channel, **result}

    body = render_invoice_link_message(invoice)
    ctx = GuestLanguageResolver.resolve(reservation, mode=LanguageMode.PROACTIVE)
    draft = GuestMessageDraft.objects.create(
        tenant_id=reservation.tenant_id,
        reservation=reservation,
        intent=GuestMessageIntent.CUSTOM,
        hint=HINT_INVOICE_LINK,
        llm_body_text=body,
        final_body_text="",
        language=ctx.language[:8],
        language_source=ctx.source.value,
        language_reason=(ctx.reason or "")[:255],
        channel=channel,
    )
    outbound = send_guest_message(
        reservation=reservation,
        draft=draft,
        channel=channel,
        body_text=body,
        api_application=None,
    )
    sent = _outbound_looks_sent(outbound, draft)
    return {
        "channel": channel,
        "status": "sent" if sent else "queued",
        "invoice_id": invoice.pk,
        "draft_id": draft.pk,
    }


def deliver_invoice_link(invoice: Invoice) -> dict:
    """Send the portal invoice URL on last inbound channel and usable email."""
    invoice_id = invoice.pk
    if not (invoice.jir or "").strip():
        return {"status": "skipped", "reason": "no_jir", "invoice_id": invoice_id}

    reservation: Reservation = invoice.reservation
    from apps.reservations.evisitor_identity import invoice_delivery_blocked_guest

    if invoice_delivery_blocked_guest(reservation) is not None:
        return {
            "status": "skipped",
            "reason": "invented_identity",
            "invoice_id": invoice_id,
        }

    channels_info = build_message_channels(reservation)
    last = (channels_info.get("reply_channel") or "").strip()
    usable = bool(resolve_invoice_recipient(reservation))
    booking_available = bool(channels_info.get("booking", {}).get("available"))
    session_open = bool(channels_info.get("whatsapp", {}).get("session_open"))
    targets = resolve_invoice_delivery_channels(
        last_channel=last,
        usable_email=usable,
        booking_available=booking_available,
        whatsapp_session_open=session_open,
    )
    if not targets:
        return {
            "status": "skipped",
            "reason": "no_channel",
            "invoice_id": invoice_id,
            "last_channel": last,
        }

    results = []
    for channel in targets:
        if channel not in VALID_INVOICE_CHANNELS:
            continue
        try:
            results.append(_send_on_channel(invoice, channel))
        except Exception as exc:
            logger.exception(
                "invoice link send failed invoice_id=%s channel=%s",
                invoice_id,
                channel,
            )
            results.append(
                {
                    "channel": channel,
                    "status": "failed",
                    "invoice_id": invoice_id,
                    "error": str(exc),
                }
            )

    statuses = {row.get("status") for row in results}
    if "sent" in statuses or "queued" in statuses:
        overall = "sent"
    elif results:
        overall = "failed"
    else:
        overall = "skipped"
    return {
        "status": overall,
        "invoice_id": invoice_id,
        "last_channel": last,
        "channels": results,
    }
