"""Booking.com (Channex) provider adapter (ADR 0010 Phase 4)."""

from __future__ import annotations

import logging

from apps.communications.guest_message_send import (
    build_message_channels,
    send_guest_message,
)
from apps.communications.messaging.context import TriggerContext
from apps.communications.messaging.models import MessageDispatch, MessageErrorCategory
from apps.communications.messaging.providers.base import (
    DEFAULT_PROVIDER_TIMEOUTS,
    MessageProvider,
    ProviderCapabilities,
)
from apps.communications.messaging.providers.common import (
    PROVIDER_BOOKING,
    categorize_reason_code,
    categorize_send_exception,
    create_timeline_draft,
    load_reservation,
    provider_message_id_from_outbound,
    snapshot_body,
)
from apps.communications.messaging.results import DeliveryResult
from apps.communications.models import GuestMessageChannel, GuestMessageIntent

logger = logging.getLogger(__name__)


class BookingProvider(MessageProvider):
    """Send via Channex Booking.com messaging using the frozen render snapshot."""

    name = PROVIDER_BOOKING
    channel = GuestMessageChannel.BOOKING
    timeout_seconds = float(DEFAULT_PROVIDER_TIMEOUTS.get(PROVIDER_BOOKING, 15.0))
    capabilities = ProviderCapabilities(
        channels=frozenset({GuestMessageChannel.BOOKING}),
        supports_attachments=False,
        supports_templates=False,
    )

    def send(
        self,
        dispatch: MessageDispatch,
        ctx: TriggerContext,
    ) -> DeliveryResult:
        body = snapshot_body(dispatch)
        if not body:
            return DeliveryResult.fail(
                provider=self.name,
                channel=self.channel,
                error_category=MessageErrorCategory.VALIDATION,
                error_code="empty_body",
                error_message="Dispatch rendered_body is empty",
                retryable=False,
            )

        try:
            reservation = load_reservation(dispatch, ctx)
        except Exception as exc:  # noqa: BLE001
            category, code, retryable = categorize_send_exception(exc)
            return DeliveryResult.fail(
                provider=self.name,
                channel=self.channel,
                error_category=category,
                error_code=code,
                error_message=str(exc)[:500],
                retryable=retryable,
            )

        channels = build_message_channels(
            reservation, intent=GuestMessageIntent.CHECKIN
        )
        booking_block = channels.get(GuestMessageChannel.BOOKING) or {}
        if not booking_block.get("available"):
            category, code, retryable = categorize_reason_code(
                "booking_channel_unavailable"
            )
            return DeliveryResult.fail(
                provider=self.name,
                channel=self.channel,
                error_category=category,
                error_code=code,
                error_message="Booking.com / Channex messaging unavailable for reservation",
                retryable=retryable,
            )

        draft = create_timeline_draft(
            reservation=reservation,
            dispatch=dispatch,
            channel=GuestMessageChannel.BOOKING,
            body_text=body,
        )
        try:
            row = send_guest_message(
                reservation=reservation,
                draft=draft,
                channel=GuestMessageChannel.BOOKING,
                body_text=body,
                api_application=None,
            )
        except Exception as exc:  # noqa: BLE001 — adapter boundary
            logger.warning(
                "messaging_booking_send_failed dispatch_id=%s reservation_id=%s: %s",
                dispatch.pk,
                reservation.pk,
                exc,
            )
            category, code, retryable = categorize_send_exception(exc)
            return DeliveryResult.fail(
                provider=self.name,
                channel=self.channel,
                error_category=category,
                error_code=code,
                error_message=str(exc)[:500],
                retryable=retryable,
            )

        return DeliveryResult.ok(
            provider=self.name,
            channel=self.channel,
            provider_message_id=provider_message_id_from_outbound(row),
        )
