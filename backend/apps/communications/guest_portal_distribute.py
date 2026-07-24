"""Send guest portal link after web check-in completes — same channel as check-in."""

from __future__ import annotations

import logging

from apps.communications.guest_compose import (
    HINT_GUEST_PORTAL_LINK,
    HINT_GUEST_PORTAL_LINK_URL,
    guest_portal_link_email_subject,
    render_guest_portal_link_email_html,
    render_guest_portal_link_message,
    render_guest_portal_link_url_only,
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
from apps.reservations.guest_portal_access import (
    build_guest_portal_url,
    ensure_active_portal_access,
)
from apps.reservations.models import (
    GuestCheckInSession,
    GuestCheckInSessionCreatedFrom,
    GuestCheckInSessionStatus,
    GuestPortalAccessCreatedFrom,
    Reservation,
)

logger = logging.getLogger(__name__)

_SESSION_TO_PORTAL_CREATED_FROM = {
    GuestCheckInSessionCreatedFrom.WHATSAPP_AUTOCHECKIN: GuestPortalAccessCreatedFrom.WHATSAPP,
    GuestCheckInSessionCreatedFrom.EMAIL: GuestPortalAccessCreatedFrom.EMAIL,
    GuestCheckInSessionCreatedFrom.RECEPTION_MANUAL: GuestPortalAccessCreatedFrom.RECEPTION_MANUAL,
    GuestCheckInSessionCreatedFrom.CHANNEX: GuestPortalAccessCreatedFrom.SYSTEM,
}

VALID_PORTAL_CHANNELS = frozenset(
    {
        GuestMessageChannel.BOOKING,
        GuestMessageChannel.WHATSAPP,
        GuestMessageChannel.EMAIL,
    }
)


def portal_link_already_sent(reservation: Reservation) -> bool:
    return GuestMessageDraft.objects.filter(
        reservation=reservation,
        hint=HINT_GUEST_PORTAL_LINK,
    ).exists()


def resolve_portal_link_channel(created_from: str) -> str | None:
    """
    Map completed check-in session ``created_from`` to outbound channel.

    WhatsApp only when the guest completed via WhatsApp autocheck-in.
    """
    if created_from == GuestCheckInSessionCreatedFrom.CHANNEX:
        return GuestMessageChannel.BOOKING
    if created_from == GuestCheckInSessionCreatedFrom.EMAIL:
        return GuestMessageChannel.EMAIL
    if created_from == GuestCheckInSessionCreatedFrom.WHATSAPP_AUTOCHECKIN:
        return GuestMessageChannel.WHATSAPP
    if created_from == GuestCheckInSessionCreatedFrom.RECEPTION_MANUAL:
        return GuestMessageChannel.EMAIL
    return None


def default_channel_from_completed_checkin(reservation: Reservation) -> str | None:
    """Channel from the latest completed check-in session, if any."""
    session = (
        GuestCheckInSession.objects.filter(
            reservation=reservation,
            status=GuestCheckInSessionStatus.COMPLETED,
        )
        .order_by("-completed_at", "-id")
        .first()
    )
    if session is None:
        return None
    return resolve_portal_link_channel(session.created_from)


def _portal_created_from(session_created_from: str) -> str:
    return _SESSION_TO_PORTAL_CREATED_FROM.get(
        session_created_from,
        GuestPortalAccessCreatedFrom.SYSTEM,
    )


def _outbound_looks_sent(outbound, draft: GuestMessageDraft) -> bool:
    sent = False
    if hasattr(outbound, "status"):
        sent = outbound.status == GuestOutboundMessageStatus.SENT
        if not sent:
            sent = getattr(outbound, "status", "") == "sent"
    if not sent and hasattr(outbound, "delivery_status"):
        sent = getattr(outbound, "delivery_status", "") in {
            "sent",
            "delivered",
            "read",
        }
    # ChannexMessage has no GuestOutboundMessageStatus — treat successful return as sent
    # when draft.sent_at was set by the booking channel helper.
    if not sent:
        draft.refresh_from_db(fields=["sent_at"])
        sent = draft.sent_at is not None
    return sent


def _create_portal_draft(
    reservation: Reservation,
    *,
    hint: str,
    body: str,
    channel: str,
    ctx,
) -> GuestMessageDraft:
    return GuestMessageDraft.objects.create(
        tenant_id=reservation.tenant_id,
        reservation=reservation,
        intent=GuestMessageIntent.CHECKIN,
        hint=hint,
        llm_body_text=body,
        final_body_text="",
        language=ctx.language[:8],
        language_source=ctx.source.value,
        language_reason=(ctx.reason or "")[:255],
        channel=channel,
    )


def send_guest_portal_link(
    reservation: Reservation,
    *,
    channel: str,
    portal_created_from: str = GuestPortalAccessCreatedFrom.SYSTEM,
    allow_resend: bool = False,
    dry_run: bool = False,
    session_id: int | None = None,
    created_from: str | None = None,
) -> dict:
    """
    Ensure portal access and send the portal URL on ``channel``.

    Dedup: at most one ``GuestMessageDraft`` with hint ``guest_portal_link`` per
    reservation unless ``allow_resend`` is True (reception Share).

    BOOKING / WHATSAPP: two consecutive sends (CTA+sign-off, then URL-only).
    EMAIL: single HTML message.
    """
    base: dict = {
        "reservation_id": reservation.pk,
        "hint": HINT_GUEST_PORTAL_LINK,
        "channel": channel,
    }
    if session_id is not None:
        base["session_id"] = session_id
    if created_from is not None:
        base["created_from"] = created_from

    if channel not in VALID_PORTAL_CHANNELS:
        return {**base, "status": "skipped", "reason": "unknown_channel"}

    if not allow_resend and portal_link_already_sent(reservation):
        return {**base, "status": "already_sent"}

    if channel == GuestMessageChannel.EMAIL and not _guest_recipient(reservation):
        return {**base, "status": "skipped", "reason": "no_email"}

    access = ensure_active_portal_access(
        reservation,
        created_from=portal_created_from,
    )
    portal_url = build_guest_portal_url(access, reservation)
    body = render_guest_portal_link_message(reservation, portal_url=portal_url)
    if not (body or "").strip():
        return {**base, "status": "skipped", "reason": "empty_body"}

    if dry_run:
        return {
            **base,
            "status": "dry_run",
            "portal_url": portal_url,
            "access_id": access.pk,
        }

    ctx = GuestLanguageResolver.resolve(reservation, mode=LanguageMode.PROACTIVE)
    draft = _create_portal_draft(
        reservation,
        hint=HINT_GUEST_PORTAL_LINK,
        body=body,
        channel=channel,
        ctx=ctx,
    )

    if channel == GuestMessageChannel.EMAIL:
        try:
            outbound = send_guest_email_with_timeline_record(
                reservation,
                body,
                subject=guest_portal_link_email_subject(reservation),
                body_html=render_guest_portal_link_email_html(
                    reservation,
                    portal_url=portal_url,
                ),
                draft=draft,
                intent=GuestMessageIntent.CHECKIN,
                hint=HINT_GUEST_PORTAL_LINK,
            )
        except Exception as exc:
            logger.exception(
                "guest portal link send failed reservation_id=%s channel=%s created_from=%s",
                reservation.pk,
                channel,
                created_from,
            )
            return {
                **base,
                "status": "failed",
                "draft_id": draft.pk,
                "error": str(exc),
            }

        sent = _outbound_looks_sent(outbound, draft)
        logger.info(
            "guest portal link sent reservation_id=%s channel=%s created_from=%s",
            reservation.pk,
            channel,
            created_from,
        )
        return {
            **base,
            "status": "sent" if sent else "queued",
            "draft_id": draft.pk,
            "portal_url": portal_url,
            "access_id": access.pk,
        }

    # BOOKING (Channex) or WHATSAPP — CTA first, URL-only second; never cross-route.
    try:
        outbound = send_guest_message(
            reservation=reservation,
            draft=draft,
            channel=channel,
            body_text=body,
            api_application=None,
        )
    except Exception as exc:
        logger.exception(
            "guest portal link send failed reservation_id=%s channel=%s created_from=%s",
            reservation.pk,
            channel,
            created_from,
        )
        return {
            **base,
            "status": "failed",
            "draft_id": draft.pk,
            "error": str(exc),
        }

    sent = _outbound_looks_sent(outbound, draft)
    url_body = render_guest_portal_link_url_only(reservation, portal_url=portal_url)
    url_draft = _create_portal_draft(
        reservation,
        hint=HINT_GUEST_PORTAL_LINK_URL,
        body=url_body,
        channel=channel,
        ctx=ctx,
    )

    try:
        url_outbound = send_guest_message(
            reservation=reservation,
            draft=url_draft,
            channel=channel,
            body_text=url_body,
            api_application=None,
        )
    except Exception as exc:
        logger.exception(
            "guest portal link URL send failed reservation_id=%s channel=%s "
            "created_from=%s draft_id=%s url_draft_id=%s",
            reservation.pk,
            channel,
            created_from,
            draft.pk,
            url_draft.pk,
        )
        return {
            **base,
            "status": "partial",
            "draft_id": draft.pk,
            "url_draft_id": url_draft.pk,
            "portal_url": portal_url,
            "access_id": access.pk,
            "error": str(exc),
        }

    url_sent = _outbound_looks_sent(url_outbound, url_draft)
    both_sent = sent and url_sent
    logger.info(
        "guest portal link sent reservation_id=%s channel=%s created_from=%s",
        reservation.pk,
        channel,
        created_from,
    )
    return {
        **base,
        "status": "sent" if both_sent else ("partial" if sent else "queued"),
        "draft_id": draft.pk,
        "url_draft_id": url_draft.pk,
        "portal_url": portal_url,
        "access_id": access.pk,
    }


def send_guest_portal_link_for_session(
    *,
    reservation_id: int,
    session_id: int,
    dry_run: bool = False,
) -> dict:
    """
    Ensure portal access and send the portal URL on the check-in completion channel.

    Dedup: at most one ``GuestMessageDraft`` with hint ``guest_portal_link`` per reservation.

    BOOKING / WHATSAPP: two consecutive sends (CTA+sign-off, then URL-only).
    EMAIL: single HTML message (unchanged).
    """
    base: dict = {
        "reservation_id": reservation_id,
        "session_id": session_id,
        "hint": HINT_GUEST_PORTAL_LINK,
    }

    reservation = (
        Reservation.objects.filter(pk=reservation_id)
        .select_related("property", "tenant")
        .first()
    )
    if reservation is None:
        return {**base, "status": "skipped", "reason": "reservation_not_found"}

    session = GuestCheckInSession.objects.filter(
        pk=session_id,
        reservation_id=reservation_id,
    ).first()
    if session is None:
        return {**base, "status": "skipped", "reason": "session_not_found"}

    if session.status != GuestCheckInSessionStatus.COMPLETED:
        return {**base, "status": "skipped", "reason": "session_not_completed"}

    created_from = session.created_from
    channel = resolve_portal_link_channel(created_from)
    base["created_from"] = created_from
    base["channel"] = channel

    if channel is None:
        return {**base, "status": "skipped", "reason": "unknown_created_from"}

    return send_guest_portal_link(
        reservation,
        channel=channel,
        portal_created_from=_portal_created_from(created_from),
        allow_resend=False,
        dry_run=dry_run,
        session_id=session_id,
        created_from=created_from,
    )
