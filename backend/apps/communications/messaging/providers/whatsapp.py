"""WhatsApp (Meta Cloud) provider adapter (ADR 0010 Phase 4).

v1 live path: WELCOME uses the existing Meta welcome template via
``send_welcome_template_for_reservation`` (copy unchanged).

Non-WELCOME definitions fall back to text send through ``send_guest_message``
using the frozen render snapshot (reserved for later cutovers).
"""

from __future__ import annotations

import logging

from apps.communications.guest_message_send import send_guest_message
from apps.communications.messaging.context import TriggerContext
from apps.communications.messaging.intents import MessageDefinitionKey
from apps.communications.messaging.models import MessageDispatch, MessageErrorCategory
from apps.communications.messaging.providers.base import (
    DEFAULT_PROVIDER_TIMEOUTS,
    MessageProvider,
    ProviderCapabilities,
)
from apps.communications.messaging.providers.common import (
    PROVIDER_WHATSAPP,
    categorize_reason_code,
    categorize_send_exception,
    create_timeline_draft,
    load_reservation,
    outbound_succeeded,
    provider_message_id_from_outbound,
    snapshot_body,
)
from apps.communications.messaging.results import DeliveryResult
from apps.communications.models import GuestMessageChannel, GuestOutboundMessage
from apps.communications.whatsapp_autocheckin_tasks import (
    send_welcome_template_for_reservation,
)
from apps.integrations.whatsapp.client import WhatsAppApiError

logger = logging.getLogger(__name__)


class WhatsAppProvider(MessageProvider):
    """Send via Meta WhatsApp Cloud API / welcome template primitives."""

    name = PROVIDER_WHATSAPP
    channel = GuestMessageChannel.WHATSAPP
    timeout_seconds = float(DEFAULT_PROVIDER_TIMEOUTS.get(PROVIDER_WHATSAPP, 20.0))
    capabilities = ProviderCapabilities(
        channels=frozenset({GuestMessageChannel.WHATSAPP}),
        supports_attachments=False,
        supports_templates=True,
    )

    def send(
        self,
        dispatch: MessageDispatch,
        ctx: TriggerContext,
    ) -> DeliveryResult:
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

        if dispatch.definition_key == MessageDefinitionKey.WELCOME:
            return self._send_welcome_template(dispatch, reservation)
        return self._send_text_snapshot(dispatch, reservation)

    def _send_welcome_template(
        self,
        dispatch: MessageDispatch,
        reservation,
    ) -> DeliveryResult:
        """Wrap existing welcome Meta template send (behavior unchanged)."""
        try:
            result = send_welcome_template_for_reservation(reservation)
        except WhatsAppApiError as exc:
            logger.warning(
                "messaging_whatsapp_welcome_api_error dispatch_id=%s reservation_id=%s: %s",
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
        except Exception as exc:  # noqa: BLE001 — adapter boundary
            logger.warning(
                "messaging_whatsapp_welcome_failed dispatch_id=%s reservation_id=%s: %s",
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

        status = str(result.get("status") or "")
        if status in {"sent", "already_sent"}:
            # already_sent is idempotent success (legacy flag already set).
            outbound_id = self._latest_welcome_outbound_id(reservation)
            return DeliveryResult.ok(
                provider=self.name,
                channel=self.channel,
                provider_message_id=str(result.get("wamid") or ""),
                outbound_message_id=outbound_id,
            )

        if status == "send_failed":
            detail = str(result.get("detail") or "send_failed")
            # Re-wrap detail string as WhatsAppApiError-shaped categorization when possible.
            category, code, retryable = categorize_reason_code("send_failed")
            if "WhatsApp" in detail or "whatsapp" in detail.lower():
                category, code, retryable = categorize_send_exception(
                    WhatsAppApiError(detail)
                )
            return DeliveryResult.fail(
                provider=self.name,
                channel=self.channel,
                error_category=category,
                error_code=code,
                error_message=detail[:500],
                retryable=retryable,
            )

        reason = str(result.get("reason") or status or "skipped")
        category, code, retryable = categorize_reason_code(reason)
        return DeliveryResult.fail(
            provider=self.name,
            channel=self.channel,
            error_category=category,
            error_code=code,
            error_message=reason[:500],
            retryable=retryable,
        )

    def _send_text_snapshot(
        self,
        dispatch: MessageDispatch,
        reservation,
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

        draft = create_timeline_draft(
            reservation=reservation,
            dispatch=dispatch,
            channel=GuestMessageChannel.WHATSAPP,
            body_text=body,
        )
        try:
            outbound = send_guest_message(
                reservation=reservation,
                draft=draft,
                channel=GuestMessageChannel.WHATSAPP,
                body_text=body,
                api_application=None,
            )
        except Exception as exc:  # noqa: BLE001 — adapter boundary
            logger.warning(
                "messaging_whatsapp_text_failed dispatch_id=%s reservation_id=%s: %s",
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

    @staticmethod
    def _latest_welcome_outbound_id(reservation) -> int | None:
        from apps.communications.models import GuestMessageIntent

        row = (
            GuestOutboundMessage.objects.filter(
                reservation=reservation,
                channel=GuestMessageChannel.WHATSAPP,
                draft__intent=GuestMessageIntent.WELCOME_TEMPLATE,
            )
            .order_by("-id")
            .values_list("id", flat=True)
            .first()
        )
        return int(row) if row else None
