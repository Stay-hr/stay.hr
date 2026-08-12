"""Send guest portal link after web check-in completes — same channel as check-in."""

from __future__ import annotations

import logging

from apps.communications.guest_compose import (
    HINT_ASK_ARRIVAL_TIME,
    HINT_GUEST_PORTAL_LINK,
    guest_portal_link_email_subject,
    render_ask_arrival_time_message,
    render_guest_portal_link_email_html,
    render_guest_portal_link_message,
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
    GuestOutboundMessage,
    GuestOutboundMessageStatus,
    PostCheckinSendClaimStatus,
)
from apps.communications.post_checkin_claims import (
    arrival_ask_claim_key,
    mark_claim_failed,
    mark_claim_sent,
    portal_claim_key,
    try_acquire_claim,
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
    """Legacy helper: any portal draft exists (not success-only).

    Prefer ``portal_link_successfully_sent`` for G3 dedup decisions.
    """
    return GuestMessageDraft.objects.filter(
        reservation=reservation,
        hint=HINT_GUEST_PORTAL_LINK,
    ).exists()


def _portal_correlation_marker(*, portal_token, session_id: int | None) -> str:
    return f"[portal_token={portal_token};session_id={session_id or 0}]"


def portal_link_successfully_sent(
    reservation: Reservation,
    *,
    channel: str,
    portal_token,
    session_id: int | None = None,
) -> bool:
    """G3: True when a successful portal outbound exists for session+token+channel.

    Failed / unsent drafts do **not** count. ``allow_resend`` callers skip this check.
    """
    return (
        _latest_successful_portal_outbound(
            reservation,
            portal_token=portal_token,
            session_id=session_id,
            channel=channel,
        )
        is not None
    )


def _latest_successful_portal_outbound(
    reservation: Reservation,
    *,
    portal_token,
    session_id: int | None = None,
    channel: str | None = None,
) -> GuestOutboundMessage | None:
    """Newest successful portal outbound for current token (+ optional filters)."""
    token = str(portal_token)
    drafts = GuestMessageDraft.objects.filter(
        reservation=reservation,
        hint=HINT_GUEST_PORTAL_LINK,
        llm_body_text__contains=f"portal_token={token}",
    )
    if session_id is not None:
        drafts = drafts.filter(
            llm_body_text__contains=f"session_id={session_id}",
        )
    if channel is not None:
        drafts = drafts.filter(channel=channel)

    draft_ids: list[int] = []
    for draft in drafts.order_by("-id").iterator():
        if draft.sent_at is not None:
            draft_ids.append(draft.pk)
            continue
        if GuestOutboundMessage.objects.filter(
            draft_id=draft.pk,
            status=GuestOutboundMessageStatus.SENT,
        ).exists():
            draft_ids.append(draft.pk)

    if not draft_ids:
        return None

    return (
        GuestOutboundMessage.objects.filter(
            reservation=reservation,
            draft_id__in=draft_ids,
            status=GuestOutboundMessageStatus.SENT,
        )
        .order_by("-created_at", "-id")
        .first()
    )


def resolve_sticky_arrival_ask_channel(
    reservation: Reservation,
    *,
    portal_token,
    session_id: int | None,
    fallback_channel: str,
) -> str:
    """G6: ask channel = latest successful portal outbound for session+token.

    Does **not** re-resolve from ``session.last_distributed_from``. Falls back to
    the channel of the current portal attempt when no successful portal exists yet.
    """
    outbound = _latest_successful_portal_outbound(
        reservation,
        portal_token=portal_token,
        session_id=session_id,
    )
    if outbound is not None and outbound.channel:
        return outbound.channel

    token = str(portal_token)
    draft_qs = GuestMessageDraft.objects.filter(
        reservation=reservation,
        hint=HINT_GUEST_PORTAL_LINK,
        sent_at__isnull=False,
        llm_body_text__contains=f"portal_token={token}",
    )
    if session_id is not None:
        draft_qs = draft_qs.filter(
            llm_body_text__contains=f"session_id={session_id}",
        )
    draft = draft_qs.order_by("-sent_at", "-id").first()
    if draft is not None and draft.channel:
        return draft.channel
    return fallback_channel


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
    portal_token=None,
    session_id: int | None = None,
) -> GuestMessageDraft:
    llm_body = body
    if hint == HINT_GUEST_PORTAL_LINK and portal_token is not None:
        marker = _portal_correlation_marker(
            portal_token=portal_token,
            session_id=session_id,
        )
        llm_body = f"{body}\n\n{marker}"
    return GuestMessageDraft.objects.create(
        tenant_id=reservation.tenant_id,
        reservation=reservation,
        intent=GuestMessageIntent.CHECKIN,
        hint=hint,
        llm_body_text=llm_body,
        final_body_text="",
        language=ctx.language[:8],
        language_source=ctx.source.value,
        language_reason=(ctx.reason or "")[:255],
        channel=channel,
    )


def _arrival_ask_correlation_marker(*, session_id: int | None) -> str:
    return f"[arrival_ask_session_id={session_id or 0}]"


def _arrival_ask_already_sent_successfully(
    reservation: Reservation,
    *,
    session_id: int | None = None,
) -> bool:
    """G4: True if a successful ask exists for this reservation (any channel).

    Dedup is **not** per channel: a WhatsApp ask blocks a later email ask.
    Failed / unsent drafts do not count. ``allow_resend`` never bypasses this.
    Reservation-wide success also covers regenerate (G8): do not re-ask.
    """
    del session_id  # reserved for G5 claim keys; success is reservation-scoped
    if GuestOutboundMessage.objects.filter(
        reservation=reservation,
        draft__hint=HINT_ASK_ARRIVAL_TIME,
        status=GuestOutboundMessageStatus.SENT,
    ).exists():
        return True
    return GuestMessageDraft.objects.filter(
        reservation=reservation,
        hint=HINT_ASK_ARRIVAL_TIME,
        sent_at__isnull=False,
    ).exists()


def _maybe_send_arrival_ask_after_portal(
    reservation: Reservation,
    *,
    channel: str,
    base: dict,
    session_id: int | None = None,
    portal_token=None,
) -> dict:
    """G2: send arrival-time ask only after a successful portal send.

    G4: success-only, channel-agnostic dedup; ``allow_resend`` does not apply.
    G6: ask uses sticky channel from successful portal outbound for session+token.
    """
    ask_base = {**base, "arrival_ask_hint": HINT_ASK_ARRIVAL_TIME}
    if session_id is not None:
        ask_base["session_id"] = session_id

    if (reservation.guest_stated_arrival_text or "").strip():
        return {
            **ask_base,
            "arrival_ask_status": "skipped",
            "arrival_ask_reason": "already_stated",
        }

    if _arrival_ask_already_sent_successfully(
        reservation,
        session_id=session_id,
    ):
        return {
            **ask_base,
            "arrival_ask_status": "skipped",
            "arrival_ask_reason": "already_sent",
        }

    if portal_token is None:
        access = ensure_active_portal_access(reservation)
        portal_token = access.token

    channel = resolve_sticky_arrival_ask_channel(
        reservation,
        portal_token=portal_token,
        session_id=session_id,
        fallback_channel=channel,
    )
    ask_base["arrival_ask_channel"] = channel

    ask_claim = None
    acquire = try_acquire_claim(
        claim_key=arrival_ask_claim_key(session_id=session_id),
        reservation=reservation,
    )
    if acquire.claim is None:
        if acquire.blocked_status == PostCheckinSendClaimStatus.SENT:
            return {
                **ask_base,
                "arrival_ask_status": "skipped",
                "arrival_ask_reason": "already_sent",
            }
        return {
            **ask_base,
            "arrival_ask_status": "skipped",
            "arrival_ask_reason": "claim_pending",
        }
    ask_claim = acquire.claim

    body = render_ask_arrival_time_message(reservation)
    if not (body or "").strip():
        mark_claim_failed(ask_claim)
        return {
            **ask_base,
            "arrival_ask_status": "skipped",
            "arrival_ask_reason": "empty_body",
        }

    ctx = GuestLanguageResolver.resolve(reservation, mode=LanguageMode.PROACTIVE)
    marker = _arrival_ask_correlation_marker(session_id=session_id)
    draft = GuestMessageDraft.objects.create(
        tenant_id=reservation.tenant_id,
        reservation=reservation,
        intent=GuestMessageIntent.CHECKIN,
        hint=HINT_ASK_ARRIVAL_TIME,
        llm_body_text=f"{body}\n\n{marker}",
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
                draft=draft,
                intent=GuestMessageIntent.CHECKIN,
                hint=HINT_ASK_ARRIVAL_TIME,
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
        mark_claim_failed(ask_claim)
        logger.exception(
            "arrival ask after portal failed reservation_id=%s channel=%s",
            reservation.pk,
            channel,
        )
        return {
            **ask_base,
            "arrival_ask_status": "failed",
            "arrival_ask_draft_id": draft.pk,
            "arrival_ask_error": str(exc),
        }

    sent = _outbound_looks_sent(outbound, draft)
    if sent:
        mark_claim_sent(ask_claim)
    else:
        mark_claim_failed(ask_claim)
    return {
        **ask_base,
        "arrival_ask_status": "sent" if sent else "queued",
        "arrival_ask_draft_id": draft.pk,
    }


def _with_arrival_ask_if_portal_sent(
    reservation: Reservation,
    *,
    channel: str,
    result: dict,
    session_id: int | None = None,
    portal_token=None,
) -> dict:
    """Attach arrival ask only when portal result status is ``sent`` (G2)."""
    if result.get("status") != "sent":
        return result
    ask_result = _maybe_send_arrival_ask_after_portal(
        reservation,
        channel=channel,
        base=result,
        session_id=session_id if session_id is not None else result.get("session_id"),
        portal_token=portal_token,
    )
    return {**result, **{
        k: v for k, v in ask_result.items() if k.startswith("arrival_ask")
    }}


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

    G3 dedup: at most one **successful** portal outbound per
    session + current portal token + channel, unless ``allow_resend`` is True.
    Failed attempts do not block retry. On success-dedup hit, continues to the
    arrival-ask gate (G2).

    BOOKING / WHATSAPP / EMAIL: one outbound with CTA + portal URL in plain
    ``body_text``. EMAIL also includes an HTML button. On success-dedup hit,
    continues to the arrival-ask gate (G2).
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

    if not allow_resend and portal_link_successfully_sent(
        reservation,
        channel=channel,
        portal_token=access.token,
        session_id=session_id,
    ):
        result = {
            **base,
            "status": "already_sent",
            "portal_url": portal_url,
            "access_id": access.pk,
        }
        # G3: successful portal already delivered — still run arrival-ask gate.
        ask_result = _maybe_send_arrival_ask_after_portal(
            reservation,
            channel=channel,
            base=result,
            session_id=session_id,
            portal_token=access.token,
        )
        return {
            **result,
            **{k: v for k, v in ask_result.items() if k.startswith("arrival_ask")},
        }

    portal_claim = None
    if not allow_resend:
        acquire = try_acquire_claim(
            claim_key=portal_claim_key(
                session_id=session_id,
                portal_token=access.token,
                channel=channel,
            ),
            reservation=reservation,
        )
        if acquire.claim is None:
            if (
                acquire.blocked_status == PostCheckinSendClaimStatus.SENT
                or portal_link_successfully_sent(
                    reservation,
                    channel=channel,
                    portal_token=access.token,
                    session_id=session_id,
                )
            ):
                result = {
                    **base,
                    "status": "already_sent",
                    "portal_url": portal_url,
                    "access_id": access.pk,
                }
                ask_result = _maybe_send_arrival_ask_after_portal(
                    reservation,
                    channel=channel,
                    base=result,
                    session_id=session_id,
                    portal_token=access.token,
                )
                return {
                    **result,
                    **{
                        k: v
                        for k, v in ask_result.items()
                        if k.startswith("arrival_ask")
                    },
                }
            return {
                **base,
                "status": "in_progress",
                "reason": "claim_pending",
                "portal_url": portal_url,
                "access_id": access.pk,
            }
        portal_claim = acquire.claim

    ctx = GuestLanguageResolver.resolve(reservation, mode=LanguageMode.PROACTIVE)
    draft = _create_portal_draft(
        reservation,
        hint=HINT_GUEST_PORTAL_LINK,
        body=body,
        channel=channel,
        ctx=ctx,
        portal_token=access.token,
        session_id=session_id,
    )

    try:
        if channel == GuestMessageChannel.EMAIL:
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
        else:
            # BOOKING (Channex) or WHATSAPP — single CTA+URL message.
            outbound = send_guest_message(
                reservation=reservation,
                draft=draft,
                channel=channel,
                body_text=body,
                api_application=None,
            )
    except Exception as exc:
        if portal_claim is not None:
            mark_claim_failed(portal_claim)
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
    if portal_claim is not None:
        if sent:
            mark_claim_sent(portal_claim)
        else:
            mark_claim_failed(portal_claim)
    logger.info(
        "guest portal link sent reservation_id=%s channel=%s created_from=%s",
        reservation.pk,
        channel,
        created_from,
    )
    result = {
        **base,
        "status": "sent" if sent else "queued",
        "draft_id": draft.pk,
        "portal_url": portal_url,
        "access_id": access.pk,
    }
    return _with_arrival_ask_if_portal_sent(
        reservation,
        channel=channel,
        result=result,
        session_id=session_id,
        portal_token=access.token,
    )


def send_guest_portal_link_for_session(
    *,
    reservation_id: int,
    session_id: int,
    dry_run: bool = False,
) -> dict:
    """
    Ensure portal access and send the portal URL on the check-in completion channel.

    Dedup: at most one successful ``guest_portal_link`` outbound per
    session + current portal token + channel (unless allow_resend).

    All channels: single message with CTA + portal URL in plain body_text.
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

    # G1: route via successful check-in link distribution when present.
    created_from = session.created_from
    route_from = session.last_distributed_from or created_from
    channel = resolve_portal_link_channel(route_from)
    base["created_from"] = created_from
    base["channel"] = channel
    if session.last_distributed_from:
        base["last_distributed_from"] = session.last_distributed_from

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
