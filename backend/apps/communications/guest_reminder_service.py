"""Pre-arrival / D0 guest web check-in reminders (email / Booking / WhatsApp).

Phase 7: when Messaging Engine owns live allowlisted scope, sends are skipped
via ``suppress_legacy_automated_outbound`` (CHECKIN_INFO/LINK + day-of).
"""

from __future__ import annotations

import logging

from django.utils import timezone

from apps.communications.guest_compose import (
    guest_web_checkin_reminder_email_subject,
    guest_web_checkin_reminder_hint,
    render_channex_guest_checkin_link_message,
    render_guest_web_checkin_reminder_message,
)
from apps.communications.guest_language_context import LanguageMode
from apps.communications.guest_language_resolver import GuestLanguageResolver
from apps.communications.guest_message_send import (
    build_message_channels,
    send_guest_message,
)
from apps.communications.models import GuestMessageChannel, GuestMessageDraft, GuestMessageIntent
from apps.reservations.checkin_readiness import all_required_slots_ready
from apps.reservations.guest_checkin_orchestrator import GuestCheckInOrchestrator
from apps.reservations.guest_checkin_session import (
    evaluate_session_access,
    get_active_session,
    mark_checkin_link_distributed,
)
from apps.reservations.models import GuestCheckInSessionCreatedFrom, Reservation

logger = logging.getLogger(__name__)

_CHANNEL_PRIORITY_PRE_ARRIVAL = (
    GuestMessageChannel.BOOKING,
    GuestMessageChannel.EMAIL,
    GuestMessageChannel.WHATSAPP,
)
_CHANNEL_PRIORITY_ARRIVAL_DAY = (
    GuestMessageChannel.WHATSAPP,
    GuestMessageChannel.EMAIL,
    GuestMessageChannel.BOOKING,
)

_REMINDER_CHANNEL_TO_DISTRIBUTED_FROM = {
    GuestMessageChannel.BOOKING: GuestCheckInSessionCreatedFrom.CHANNEX,
    GuestMessageChannel.EMAIL: GuestCheckInSessionCreatedFrom.EMAIL,
    GuestMessageChannel.WHATSAPP: GuestCheckInSessionCreatedFrom.WHATSAPP_AUTOCHECKIN,
}


def _pick_delivery_channel(channels: dict, *, days_before: int) -> str:
    priority = (
        _CHANNEL_PRIORITY_ARRIVAL_DAY
        if int(days_before) == 0
        else _CHANNEL_PRIORITY_PRE_ARRIVAL
    )
    for channel in priority:
        block = channels.get(channel) or {}
        if block.get("available"):
            return channel
    return ""


class GuestReminderService:
    """Send pre-arrival check-in reminders without mutating session lifecycle."""

    @staticmethod
    def reminder_already_sent(reservation: Reservation, *, days_before: int) -> bool:
        hint = guest_web_checkin_reminder_hint(days_before=days_before)
        return GuestMessageDraft.objects.filter(
            reservation=reservation,
            hint=hint,
        ).exists()

    @staticmethod
    def send_pre_arrival_reminder(
        reservation: Reservation,
        *,
        days_before: int,
        dry_run: bool = False,
    ) -> dict:
        """
        Ensure active session + send reminder on first available channel.

        Returns sent | already_sent | skipped with reason.
        """
        hint = guest_web_checkin_reminder_hint(days_before=days_before)
        base = {
            "reservation_id": reservation.pk,
            "days_before": days_before,
            "hint": hint,
        }

        # Phase 7: engine owns CHECKIN_INFO/LINK (+ D0) for live allowlisted scope.
        from apps.communications.messaging.flags import (
            suppress_legacy_automated_outbound,
        )

        if suppress_legacy_automated_outbound(reservation=reservation):
            logger.info(
                "guest checkin reminder suppressed_by_orchestration "
                "reservation_id=%s days_before=%s",
                reservation.pk,
                days_before,
            )
            return {
                **base,
                "status": "skipped",
                "reason": "orchestration_owns_outbound",
            }

        if GuestReminderService.reminder_already_sent(reservation, days_before=days_before):
            return {**base, "status": "already_sent"}

        if reservation.status != Reservation.Status.EXPECTED:
            return {**base, "status": "skipped", "reason": "reservation_not_expected"}

        if all_required_slots_ready(reservation):
            return {**base, "status": "skipped", "reason": "checkin_complete"}

        session = get_active_session(reservation)
        if session is None:
            if dry_run:
                return {**base, "status": "skipped", "reason": "no_active_session"}
            ensured = GuestCheckInOrchestrator.ensure_session_and_link(
                reservation,
                created_from=GuestCheckInSessionCreatedFrom.EMAIL,
            )
            session = ensured.session
            checkin_url = ensured.url
        else:
            checkin_url = GuestCheckInOrchestrator.ensure_session_and_link(
                reservation,
                created_from=GuestCheckInSessionCreatedFrom.EMAIL,
            ).url

        access = evaluate_session_access(session, reservation, now=timezone.now())
        if not access.allowed:
            return {
                **base,
                "status": "skipped",
                "reason": f"session_{access.gate_status}",
            }

        channels = build_message_channels(reservation, intent=GuestMessageIntent.CHECKIN)
        channel = _pick_delivery_channel(channels, days_before=days_before)
        if channel == GuestMessageChannel.BOOKING:
            body = render_channex_guest_checkin_link_message(
                reservation,
                checkin_url=checkin_url,
            )
        else:
            body = render_guest_web_checkin_reminder_message(
                reservation,
                checkin_url=checkin_url,
            )
        if not (body or "").strip():
            return {**base, "status": "skipped", "reason": "empty_body"}

        if dry_run:
            return {
                **base,
                "status": "dry_run",
                "channel": channel or None,
            }

        if not channel:
            ctx = GuestLanguageResolver.resolve(reservation, mode=LanguageMode.PROACTIVE)
            draft = GuestMessageDraft.objects.create(
                tenant_id=reservation.tenant_id,
                reservation=reservation,
                intent=GuestMessageIntent.CHECKIN,
                hint=hint,
                llm_body_text=body,
                final_body_text="",
                language=ctx.language[:8],
                language_source=ctx.source.value,
                language_reason=(ctx.reason or "")[:255],
                channel="",
            )
            logger.info(
                "guest checkin reminder manual_required reservation_id=%s draft_id=%s",
                reservation.pk,
                draft.pk,
            )
            return {
                **base,
                "status": "manual_required",
                "draft_id": draft.pk,
            }

        ctx = GuestLanguageResolver.resolve(reservation, mode=LanguageMode.PROACTIVE)
        draft = GuestMessageDraft.objects.create(
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

        subject = None
        if channel == GuestMessageChannel.EMAIL:
            subject = guest_web_checkin_reminder_email_subject(reservation)

        try:
            outbound = send_guest_message(
                reservation=reservation,
                draft=draft,
                channel=channel,
                body_text=body,
                api_application=None,
                subject=subject,
            )
        except Exception as exc:
            logger.exception(
                "guest checkin reminder send failed reservation_id=%s channel=%s",
                reservation.pk,
                channel,
            )
            return {
                **base,
                "status": "failed",
                "channel": channel,
                "error": str(exc),
            }

        sent = getattr(outbound, "status", "") == "sent" or getattr(
            outbound,
            "delivery_status",
            "",
        ) in {"sent", "delivered", "read"}
        if not sent and hasattr(outbound, "status"):
            from apps.communications.models import GuestOutboundMessageStatus

            sent = outbound.status == GuestOutboundMessageStatus.SENT

        if sent:
            distributed_from = _REMINDER_CHANNEL_TO_DISTRIBUTED_FROM.get(channel)
            if distributed_from:
                mark_checkin_link_distributed(
                    session,
                    distributed_from=distributed_from,
                )

        logger.info(
            "guest checkin reminder sent reservation_id=%s channel=%s days_before=%s",
            reservation.pk,
            channel,
            days_before,
        )
        return {
            **base,
            "status": "sent" if sent else "queued",
            "channel": channel,
            "draft_id": draft.pk,
        }
