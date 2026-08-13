"""WhatsApp guest message send — Meta Cloud API, templates, pending_send."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.communications.canonical_store import create_with_canonical, record_canonical_source
from apps.communications.guest_message_logging import (
    GUEST_MESSAGE_SEND_ERROR,
    GUEST_MESSAGE_WHATSAPP_API,
    GUEST_MESSAGE_WHATSAPP_BLOCKED,
    GUEST_MESSAGE_WHATSAPP_HANDOFF,
    GUEST_MESSAGE_WHATSAPP_TEMPLATE,
    MetaApiTimer,
    body_meta,
    log_guest_message_event,
)
from apps.communications.models import (
    GuestMessageChannel,
    GuestMessageDraft,
    GuestMessageIntent,
    GuestOutboundDeliveryStatus,
    GuestOutboundMessage,
    GuestOutboundMessageStatus,
)
from apps.integrations.models import IntegrationConfig, WhatsAppMessage
from apps.integrations.whatsapp.client import (
    WhatsAppApiError,
    extract_outbound_wamid,
    send_template_message,
    send_text_message,
)
from apps.integrations.whatsapp.integration_lookup import resolve_whatsapp_integration
from apps.integrations.whatsapp.meta_templates import MetaTemplateApiError, find_message_template
from apps.integrations.whatsapp.phone import normalize_phone
from apps.integrations.whatsapp.welcome_template import (
    build_welcome_template_parameters,
    resolve_welcome_template,
    welcome_header_image_url,
)
from apps.integrations.whatsapp.whatsapp_errors import (
    is_transient_whatsapp_error,
    is_whatsapp_session_api_error,
    parse_meta_api_error,
)
from apps.integrations.whatsapp.whatsapp_session import is_customer_service_window_open
from apps.reservations.models import Reservation, ReservationVersionScope
from apps.reservations.reservation_version import touch_reservation_version
from apps.tenants.models import ApiApplication

from .guest_message_send import (
    _send_whatsapp_handoff,
    guest_phone_number,
)

logger = logging.getLogger(__name__)

WHATSAPP_TEMPLATE_REQUIRED = "whatsapp_template_required"
WHATSAPP_SEND_PENDING = "whatsapp_send_pending"
WHATSAPP_PROVIDER = "meta"

_SYNC_RETRY_BACKOFF_SECONDS = 2
_MAX_SYNC_RETRIES = 1


class WhatsAppSendPendingError(Exception):
    """Graph API transient failure — outbound remains pending_send."""

    def __init__(self, outbound: GuestOutboundMessage):
        self.outbound = outbound
        super().__init__(WHATSAPP_SEND_PENDING)


def _log_whatsapp_event(
    event: str,
    *,
    request_id: str,
    reservation: Reservation,
    draft: GuestMessageDraft,
    **more,
) -> None:
    log_guest_message_event(
        event,
        request_id=request_id,
        reservation_id=reservation.pk,
        tenant_slug=reservation.tenant.slug,
        draft_id=draft.pk,
        channel=GuestMessageChannel.WHATSAPP,
        intent=draft.intent,
        **more,
    )


def _can_send_welcome_template(
    *,
    draft: GuestMessageDraft,
    integration: IntegrationConfig,
    runtime,
) -> tuple[str, str] | None:
    if draft.intent not in (
        GuestMessageIntent.CHECKIN,
        GuestMessageIntent.WELCOME_TEMPLATE,
    ):
        return None
    config = integration.get_config_dict()
    lang, _params = build_welcome_template_parameters(draft.reservation)
    resolved = resolve_welcome_template(language=lang, platform_config=config)
    template_name = resolved.template_name
    meta_lang = resolved.meta_language
    waba_id = runtime.effective_waba_id()
    if not waba_id or not runtime.access_token:
        return None
    try:
        existing = find_message_template(
            waba_id=waba_id,
            access_token=runtime.access_token,
            name=template_name,
            language=meta_lang,
        )
    except MetaTemplateApiError as exc:
        logger.warning("welcome template lookup failed: %s", exc)
        return None
    if not existing or str(existing.get("status") or "").upper() != "APPROVED":
        return None
    return template_name, meta_lang


def _mark_outbound_sent(
    *,
    outbound: GuestOutboundMessage,
    draft: GuestMessageDraft,
    body_text: str,
    provider_message_id: str,
) -> GuestOutboundMessage:
    now = timezone.now()
    outbound.status = GuestOutboundMessageStatus.SENT
    outbound.provider = WHATSAPP_PROVIDER
    outbound.provider_message_id = provider_message_id
    outbound.delivery_status = GuestOutboundDeliveryStatus.SENT
    outbound.error_message = ""
    outbound.next_retry_at = None
    outbound.save(
        update_fields=[
            "status",
            "provider",
            "provider_message_id",
            "delivery_status",
            "error_message",
            "next_retry_at",
        ]
    )
    draft.final_body_text = body_text
    draft.channel = GuestMessageChannel.WHATSAPP
    draft.sent_at = now
    draft.save(update_fields=["final_body_text", "channel", "sent_at"])
    touch_reservation_version(
        draft.reservation_id,
        ReservationVersionScope.MESSAGES,
        reason="whatsapp_outbound",
    )
    return outbound


def _record_whatsapp_message_row(
    *,
    integration: IntegrationConfig,
    reservation: Reservation,
    phone_wa: str,
    runtime,
    wamid: str,
    body: str,
    message_type: str,
    raw_payload: dict,
) -> None:
    if not wamid:
        return
    with transaction.atomic():
        row, _created = WhatsAppMessage.objects.get_or_create(
            wamid=wamid,
            defaults={
                "tenant_id": reservation.tenant_id,
                "integration": integration,
                "reservation": reservation,
                "wa_id": phone_wa,
                "phone_number_id": runtime.phone_number_id,
                "direction": WhatsAppMessage.Direction.OUTBOUND,
                "source": WhatsAppMessage.Source.CLOUD_API,
                "message_type": message_type,
                "body": body,
                "raw_payload": raw_payload,
            },
        )
        record_canonical_source(row)


def _call_graph_with_retry(send_fn):
    last_exc: BaseException | None = None
    for attempt in range(_MAX_SYNC_RETRIES + 1):
        try:
            return send_fn()
        except WhatsAppApiError as exc:
            last_exc = exc
            if is_transient_whatsapp_error(exc) and attempt < _MAX_SYNC_RETRIES:
                continue
            raise
    if last_exc:
        raise last_exc
    raise WhatsAppApiError("send failed")


def _handle_transient_failure(
    *,
    outbound: GuestOutboundMessage,
    exc: BaseException,
) -> None:
    outbound.retry_count += 1
    outbound.next_retry_at = timezone.now() + timedelta(seconds=_SYNC_RETRY_BACKOFF_SECONDS * outbound.retry_count)
    outbound.error_message = str(exc)[:500]
    outbound.save(update_fields=["retry_count", "next_retry_at", "error_message"])
    raise WhatsAppSendPendingError(outbound) from exc


def _send_whatsapp_template_api(
    *,
    reservation: Reservation,
    draft: GuestMessageDraft,
    body_text: str,
    api_application: ApiApplication | None,
    integration: IntegrationConfig,
    runtime,
    template_name: str,
    language_code: str,
    outbound: GuestOutboundMessage | None = None,
    request_id: str = "",
) -> GuestOutboundMessage:
    phone_raw = guest_phone_number(reservation)
    phone_wa = normalize_phone(phone_raw)
    if not phone_wa:
        raise ValueError("no_phone")

    config = integration.get_config_dict()
    _, body_params = build_welcome_template_parameters(reservation)
    header_url = welcome_header_image_url(config)

    if outbound is None:
        outbound = create_with_canonical(GuestOutboundMessage,
            tenant_id=reservation.tenant_id,
            reservation=reservation,
            draft=draft,
            channel=GuestMessageChannel.WHATSAPP,
            body_text=body_text,
            status=GuestOutboundMessageStatus.PENDING_SEND,
            to_phone=phone_raw,
            provider=WHATSAPP_PROVIDER,
            api_application=api_application,
        )

    try:
        with MetaApiTimer() as timer:
            response = _call_graph_with_retry(
                lambda: send_template_message(
                    phone_number_id=runtime.phone_number_id,
                    access_token=runtime.access_token,
                    to_wa_id=phone_wa,
                    template_name=template_name,
                    language_code=language_code,
                    body_parameters=body_params,
                    header_image_url=header_url,
                )
            )
    except WhatsAppApiError as exc:
        error_fields = parse_meta_api_error(exc)
        _log_whatsapp_event(
            GUEST_MESSAGE_SEND_ERROR,
            request_id=request_id,
            reservation=reservation,
            draft=draft,
            template_name=template_name,
            meta_api_ms=timer.elapsed_ms,
            **body_meta(body_text),
            **error_fields,
        )
        if is_transient_whatsapp_error(exc):
            _handle_transient_failure(outbound=outbound, exc=exc)
        outbound.status = GuestOutboundMessageStatus.FAILED
        outbound.error_message = str(exc)[:500]
        outbound.save(update_fields=["status", "error_message"])
        raise ValueError(str(exc)) from exc

    wamid = extract_outbound_wamid(response)
    _log_whatsapp_event(
        GUEST_MESSAGE_WHATSAPP_TEMPLATE,
        request_id=request_id,
        reservation=reservation,
        draft=draft,
        template_name=template_name,
        provider_message_id=wamid,
        meta_api_ms=timer.elapsed_ms,
        **body_meta(body_text),
    )
    _record_whatsapp_message_row(
        integration=integration,
        reservation=reservation,
        phone_wa=phone_wa,
        runtime=runtime,
        wamid=wamid,
        body=body_text,
        message_type="template",
        raw_payload=response,
    )
    return _mark_outbound_sent(
        outbound=outbound,
        draft=draft,
        body_text=body_text,
        provider_message_id=wamid,
    )


def _send_whatsapp_text_api(
    *,
    reservation: Reservation,
    draft: GuestMessageDraft,
    body_text: str,
    api_application: ApiApplication | None,
    integration: IntegrationConfig,
    runtime,
    outbound: GuestOutboundMessage | None = None,
    request_id: str = "",
) -> GuestOutboundMessage:
    phone_raw = guest_phone_number(reservation)
    phone_wa = normalize_phone(phone_raw)
    if not phone_wa:
        raise ValueError("no_phone")

    if outbound is None:
        outbound = create_with_canonical(GuestOutboundMessage,
            tenant_id=reservation.tenant_id,
            reservation=reservation,
            draft=draft,
            channel=GuestMessageChannel.WHATSAPP,
            body_text=body_text,
            status=GuestOutboundMessageStatus.PENDING_SEND,
            to_phone=phone_raw,
            provider=WHATSAPP_PROVIDER,
            api_application=api_application,
        )

    timer = MetaApiTimer()
    try:
        with timer:
            response = _call_graph_with_retry(
                lambda: send_text_message(
                    phone_number_id=runtime.phone_number_id,
                    access_token=runtime.access_token,
                    to_wa_id=phone_wa,
                    body=body_text,
                )
            )
    except WhatsAppApiError as exc:
        error_fields = parse_meta_api_error(exc)
        if is_whatsapp_session_api_error(exc):
            template_info = _can_send_welcome_template(
                draft=draft, integration=integration, runtime=runtime
            )
            if template_info:
                return _send_whatsapp_template_api(
                    reservation=reservation,
                    draft=draft,
                    body_text=body_text,
                    api_application=api_application,
                    integration=integration,
                    runtime=runtime,
                    template_name=template_info[0],
                    language_code=template_info[1],
                    outbound=outbound,
                    request_id=request_id,
                )
            _log_whatsapp_event(
                GUEST_MESSAGE_WHATSAPP_BLOCKED,
                request_id=request_id,
                reservation=reservation,
                draft=draft,
                meta_api_ms=timer.elapsed_ms,
                **body_meta(body_text),
                **error_fields,
            )
            outbound.delete()
            raise ValueError(WHATSAPP_TEMPLATE_REQUIRED) from exc
        _log_whatsapp_event(
            GUEST_MESSAGE_SEND_ERROR,
            request_id=request_id,
            reservation=reservation,
            draft=draft,
            meta_api_ms=timer.elapsed_ms,
            **body_meta(body_text),
            **error_fields,
        )
        if is_transient_whatsapp_error(exc):
            _handle_transient_failure(outbound=outbound, exc=exc)
        outbound.status = GuestOutboundMessageStatus.FAILED
        outbound.error_message = str(exc)[:500]
        outbound.save(update_fields=["status", "error_message"])
        raise ValueError(str(exc)) from exc

    wamid = extract_outbound_wamid(response)
    _log_whatsapp_event(
        GUEST_MESSAGE_WHATSAPP_API,
        request_id=request_id,
        reservation=reservation,
        draft=draft,
        provider_message_id=wamid,
        meta_api_ms=timer.elapsed_ms,
        **body_meta(body_text),
    )
    _record_whatsapp_message_row(
        integration=integration,
        reservation=reservation,
        phone_wa=phone_wa,
        runtime=runtime,
        wamid=wamid,
        body=body_text,
        message_type="text",
        raw_payload=response,
    )
    return _mark_outbound_sent(
        outbound=outbound,
        draft=draft,
        body_text=body_text,
        provider_message_id=wamid,
    )


def send_whatsapp_channel_v2(
    *,
    reservation: Reservation,
    draft: GuestMessageDraft,
    body_text: str,
    api_application: ApiApplication | None,
    existing_outbound: GuestOutboundMessage | None = None,
    request_id: str = "",
) -> GuestOutboundMessage:
    integration, runtime = resolve_whatsapp_integration(reservation.tenant)
    if integration is None or runtime is None or not runtime.can_send_messages():
        _log_whatsapp_event(
            GUEST_MESSAGE_WHATSAPP_HANDOFF,
            request_id=request_id,
            reservation=reservation,
            draft=draft,
            handoff_reason="integration_not_configured",
            **body_meta(body_text),
        )
        return _send_whatsapp_handoff(
            reservation=reservation,
            draft=draft,
            body_text=body_text,
            api_application=api_application,
            handoff_reason="integration_not_configured",
        )

    if existing_outbound is not None:
        if existing_outbound.status == GuestOutboundMessageStatus.SENT:
            return existing_outbound
        if existing_outbound.status == GuestOutboundMessageStatus.PENDING_SEND:
            body_text = existing_outbound.body_text

    session_open = is_customer_service_window_open(
        tenant_id=reservation.tenant_id,
        reservation=reservation,
    )

    if session_open:
        return _send_whatsapp_text_api(
            reservation=reservation,
            draft=draft,
            body_text=body_text,
            api_application=api_application,
            integration=integration,
            runtime=runtime,
            outbound=existing_outbound,
            request_id=request_id,
        )

    template_info = _can_send_welcome_template(
        draft=draft, integration=integration, runtime=runtime
    )
    if template_info:
        return _send_whatsapp_template_api(
            reservation=reservation,
            draft=draft,
            body_text=body_text,
            api_application=api_application,
            integration=integration,
            runtime=runtime,
            template_name=template_info[0],
            language_code=template_info[1],
            outbound=existing_outbound,
            request_id=request_id,
        )

    _log_whatsapp_event(
        GUEST_MESSAGE_WHATSAPP_BLOCKED,
        request_id=request_id,
        reservation=reservation,
        draft=draft,
        **body_meta(body_text),
    )
    raise ValueError(WHATSAPP_TEMPLATE_REQUIRED)
