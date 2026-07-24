"""SMTP email provider adapter (ADR 0010 Phase 4)."""

from __future__ import annotations

import logging

from apps.communications.guest_message_send import send_guest_message
from apps.communications.messaging.context import TriggerContext
from apps.communications.messaging.models import MessageDispatch, MessageErrorCategory
from apps.communications.messaging.providers.base import (
    DEFAULT_PROVIDER_TIMEOUTS,
    MessageProvider,
    ProviderCapabilities,
)
from apps.communications.messaging.providers.common import (
    PROVIDER_EMAIL,
    categorize_reason_code,
    categorize_send_exception,
    create_timeline_draft,
    load_reservation,
    outbound_succeeded,
    provider_message_id_from_outbound,
    snapshot_body,
    snapshot_subject,
)
from apps.communications.messaging.results import DeliveryResult
from apps.communications.models import GuestMessageChannel, GuestOutboundMessage

logger = logging.getLogger(__name__)


class EmailProvider(MessageProvider):
    """Send via SMTP using the frozen render snapshot (subject + body)."""

    name = PROVIDER_EMAIL
    channel = GuestMessageChannel.EMAIL
    timeout_seconds = float(DEFAULT_PROVIDER_TIMEOUTS.get(PROVIDER_EMAIL, 30.0))
    capabilities = ProviderCapabilities(
        channels=frozenset({GuestMessageChannel.EMAIL}),
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

        subject = snapshot_subject(dispatch) or None
        draft = create_timeline_draft(
            reservation=reservation,
            dispatch=dispatch,
            channel=GuestMessageChannel.EMAIL,
            body_text=body,
        )
        try:
            outbound = send_guest_message(
                reservation=reservation,
                draft=draft,
                channel=GuestMessageChannel.EMAIL,
                body_text=body,
                api_application=None,
                subject=subject,
            )
        except Exception as exc:  # noqa: BLE001 — adapter boundary
            logger.warning(
                "messaging_email_send_failed dispatch_id=%s reservation_id=%s: %s",
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

        if not isinstance(outbound, GuestOutboundMessage):
            return DeliveryResult.fail(
                provider=self.name,
                channel=self.channel,
                error_category=MessageErrorCategory.PROVIDER,
                error_code="unexpected_outbound_type",
                error_message=f"Expected GuestOutboundMessage, got {type(outbound).__name__}",
                retryable=False,
            )

        if outbound_succeeded(outbound):
            return DeliveryResult.ok(
                provider=self.name,
                channel=self.channel,
                provider_message_id=provider_message_id_from_outbound(outbound),
                outbound_message_id=outbound.pk,
            )

        reason = (outbound.error_message or "send_failed").strip() or "send_failed"
        category, code, retryable = categorize_reason_code(reason)
        return DeliveryResult.fail(
            provider=self.name,
            channel=self.channel,
            error_category=category,
            error_code=code,
            error_message=reason[:500],
            retryable=retryable,
            outbound_message_id=outbound.pk,
        )
