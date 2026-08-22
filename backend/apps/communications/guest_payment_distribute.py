"""Send guest payment instruction link (WhatsApp / email)."""

from __future__ import annotations

import logging
from decimal import Decimal

from apps.communications.guest_compose import (
    HINT_GUEST_PAYMENT_LINK,
    guest_payment_link_email_subject,
    render_guest_payment_link_email_html,
    render_guest_payment_link_message,
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
from apps.reservations.guest_payment_access import (
    build_guest_payment_url,
    ensure_active_payment_access,
)
from apps.reservations.models import GuestPaymentAccessCreatedFrom, Reservation

logger = logging.getLogger(__name__)

VALID_PAYMENT_CHANNELS = frozenset(
    {
        GuestMessageChannel.WHATSAPP,
        GuestMessageChannel.EMAIL,
    }
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


def send_guest_payment_link(
    reservation: Reservation,
    *,
    channel: str,
    payment_created_from: str = GuestPaymentAccessCreatedFrom.RECEPTION_MANUAL,
    dry_run: bool = False,
    created_from: str | None = None,
) -> dict:
    """Ensure payment access and send the payment URL on ``channel``."""
    base: dict = {
        "reservation_id": reservation.pk,
        "hint": HINT_GUEST_PAYMENT_LINK,
        "channel": channel,
    }
    if created_from is not None:
        base["created_from"] = created_from

    if channel not in VALID_PAYMENT_CHANNELS:
        return {**base, "status": "skipped", "reason": "unknown_channel"}

    if channel == GuestMessageChannel.EMAIL and not _guest_recipient(reservation):
        return {**base, "status": "skipped", "reason": "no_email"}

    if reservation.amount is None:
        return {**base, "status": "skipped", "reason": "no_amount"}

    try:
        access = ensure_active_payment_access(
            reservation,
            created_from=payment_created_from,
        )
    except ValueError as exc:
        return {**base, "status": "skipped", "reason": str(exc)}

    payment_url = build_guest_payment_url(access, reservation)
    amount_str = str(Decimal(reservation.amount).quantize(Decimal("0.01")))
    body = render_guest_payment_link_message(
        reservation,
        payment_url=payment_url,
        payment_amount=amount_str,
    )
    if not (body or "").strip():
        return {**base, "status": "skipped", "reason": "empty_body"}

    if dry_run:
        return {
            **base,
            "status": "dry_run",
            "payment_url": payment_url,
            "access_id": access.pk,
        }

    ctx = GuestLanguageResolver.resolve(reservation, mode=LanguageMode.PROACTIVE)
    draft = GuestMessageDraft.objects.create(
        tenant_id=reservation.tenant_id,
        reservation=reservation,
        intent=GuestMessageIntent.CUSTOM,
        hint=HINT_GUEST_PAYMENT_LINK,
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
                subject=guest_payment_link_email_subject(reservation),
                body_html=render_guest_payment_link_email_html(
                    reservation,
                    payment_url=payment_url,
                    payment_amount=amount_str,
                ),
                draft=draft,
                intent=GuestMessageIntent.CUSTOM,
                hint=HINT_GUEST_PAYMENT_LINK,
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
            "guest payment link send failed reservation_id=%s channel=%s",
            reservation.pk,
            channel,
        )
        return {
            **base,
            "status": "failed",
            "draft_id": draft.pk,
            "payment_url": payment_url,
            "access_id": access.pk,
            "error": str(exc),
        }

    sent = _outbound_looks_sent(outbound, draft)
    return {
        **base,
        "status": "sent" if sent else "queued",
        "draft_id": draft.pk,
        "payment_url": payment_url,
        "access_id": access.pk,
    }
