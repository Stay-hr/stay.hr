from __future__ import annotations

import logging
import re

from celery import shared_task
from django.utils import timezone

from apps.communications.guest_compose import (
    render_autocheckin_web_checkin_message,
    render_documents_message,
)
from apps.communications.whatsapp_autocheckin_tasks import mark_autocheckin_engaged
from apps.communications.models import (
    GuestMessageChannel,
    GuestMessageDraft,
    GuestMessageIntent,
    GuestOutboundMessage,
    GuestOutboundMessageStatus,
)
from apps.integrations.models import IntegrationConfig, WhatsAppMessage
from apps.integrations.whatsapp.client import WhatsAppApiError, extract_outbound_wamid, send_text_message
from apps.integrations.whatsapp.whatsapp_document_batch import (
    handle_whatsapp_document_batch_reply,
    inbound_interactive_button_id,
    is_documents_all_no_reply,
    is_documents_all_yes_reply,
    on_whatsapp_document_received,
)
from apps.integrations.whatsapp import operator_arrival_confirm  # noqa: F401 — register Celery beat tasks
from apps.integrations.whatsapp import whatsapp_operator_batch  # noqa: F401 — register operator quiet timer task
from apps.integrations.whatsapp import guest_document_batch_reconcile  # noqa: F401 — register reconcile beat task
from apps.integrations.whatsapp.reply import build_greeting
from apps.integrations.whatsapp.reservation_lookup import find_reservation_for_wa_id
from apps.integrations.whatsapp.runtime_config import WhatsAppRuntimeConfig
from apps.integrations.whatsapp.whatsapp_guest_autocheckin import (
    handle_guest_autocheckin_inbound,
    is_guest_auto_checkin_button,
    reply_already_checked_in_autocheckin,
    resolve_guest_reservation,
)
from apps.integrations.whatsapp.whatsapp_operator import (
    is_operator_wa_id,
    is_operator_wa_id_any_tenant,
)
from apps.integrations.whatsapp.whatsapp_operator_service import handle_operator_inbound
from apps.integrations.whatsapp.whatsapp_session import resolved_tenant_id_for_message
from apps.reservations.models import ReservationVersionScope
from apps.reservations.reservation_version import touch_reservation_version

logger = logging.getLogger(__name__)

_NON_ACTIONABLE_MESSAGE_TYPES = frozenset(
    {"unsupported", "unknown", "reaction", "sticker", "location", "contacts"},
)

_WHATSAPP_NON_TEXT_PREVIEW = "Poruka (WhatsApp)"

_AUTO_CHECKIN_REPLY_TEXTS = frozenset(
    {
        "auto check in",
        "auto checkin",
        "autocheck in",
        "autocheckin",
        "auto check-in",
        "automatischer check-in",
        "automatischer check in",
        "check-in automatique",
        "check in automatico",
        "check-in automático",
        "check in",
    }
)


def _normalize_quick_reply_text(text: str) -> str:
    lowered = (text or "").strip().lower()
    lowered = re.sub(r"[_\-]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def is_auto_checkin_quick_reply(body: str) -> bool:
    return _normalize_quick_reply_text(body) in _AUTO_CHECKIN_REPLY_TEXTS


def _inbound_action_text(row: WhatsAppMessage) -> str:
    """Text from body or template/quick-reply button payload (type button/interactive)."""
    body = (row.body or "").strip()
    if body:
        return body

    payload = row.raw_payload or {}
    message_type = (row.message_type or payload.get("type") or "").strip()
    if message_type == "button":
        button = payload.get("button") or {}
        return str(button.get("text") or button.get("payload") or "").strip()
    if message_type == "interactive":
        interactive = payload.get("interactive") or {}
        interactive_type = str(interactive.get("type") or "").strip()
        if interactive_type == "button_reply":
            return str((interactive.get("button_reply") or {}).get("title") or "").strip()
        if interactive_type == "list_reply":
            return str((interactive.get("list_reply") or {}).get("title") or "").strip()
    return ""


def _inbound_body_preview(row: WhatsAppMessage) -> str:
    if row.message_type == "text":
        return row.body or ""
    action = _inbound_action_text(row)
    if action:
        return action
    return _WHATSAPP_NON_TEXT_PREVIEW


def _link_inbound_to_reservation(row: WhatsAppMessage, *, tenant_id: int) -> None:
    if row.reservation_id is not None:
        return
    reservation = find_reservation_for_wa_id(tenant_id=tenant_id, wa_id=row.wa_id)
    if reservation is None:
        return
    row.reservation = reservation
    row.save(update_fields=["reservation"])


def _maybe_send_auto_reply(
    *,
    row: WhatsAppMessage,
    integration_row: IntegrationConfig,
    runtime: WhatsAppRuntimeConfig,
    reservation,
    profile_name: str,
) -> dict:
    if not runtime.auto_reply:
        return {"status": "auto_reply_disabled"}

    if not runtime.send_credentials_ok():
        logger.warning("WhatsApp auto-reply skipped: missing credentials tenant=%s", row.tenant_id)
        return {"status": "missing_credentials"}

    greeting = build_greeting(
        integration_row=integration_row,
        reservation=reservation,
        profile_name=profile_name,
    )

    try:
        response = send_text_message(
            phone_number_id=runtime.phone_number_id,
            access_token=runtime.access_token,
            to_wa_id=row.wa_id,
            body=greeting,
        )
    except WhatsAppApiError as exc:
        logger.warning("WhatsApp reply failed message_id=%s: %s", row.pk, exc)
        return {"status": "send_failed", "detail": str(exc)}

    outbound_wamid = extract_outbound_wamid(response)
    if outbound_wamid:
        WhatsAppMessage.objects.create(
            tenant_id=row.tenant_id,
            integration=integration_row,
            reservation=reservation,
            wamid=outbound_wamid,
            wa_id=row.wa_id,
            phone_number_id=runtime.phone_number_id,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            message_type="text",
            body=greeting,
            raw_payload=response,
        )

    return {
        "status": "replied",
        "outbound_wamid": outbound_wamid,
    }


def _touch_whatsapp_autocheckin_reply(
    reservation,
    result: dict,
) -> dict:
    reservation_id = getattr(reservation, "pk", None)
    if reservation_id and result.get("outbound_wamid"):
        touch_reservation_version(
            reservation_id,
            ReservationVersionScope.MESSAGES,
            reason="whatsapp_autocheckin_reply",
        )
    return result


def _maybe_send_autocheckin_documents_reply(
    *,
    row: WhatsAppMessage,
    integration_row: IntegrationConfig,
    runtime: WhatsAppRuntimeConfig,
    reservation,
) -> dict:
    if reservation is None:
        return {"status": "skipped", "reason": "no_reservation"}

    from apps.integrations.whatsapp.apply_reply import (
        is_guest_checkin_acknowledged,
        is_whatsapp_autocheckin_waived,
    )

    if is_guest_checkin_acknowledged(reservation):
        return _touch_whatsapp_autocheckin_reply(
            reservation,
            reply_already_checked_in_autocheckin(
                integration_row=integration_row,
                runtime=runtime,
                row=row,
                reservation=reservation,
            ),
        )

    if is_whatsapp_autocheckin_waived(reservation):
        return {"status": "skipped", "reason": "autocheckin_waived"}

    from apps.integrations.whatsapp.guest_document_lifecycle import guest_checkin_web_only_enabled
    from apps.reservations.guest_checkin_orchestrator import GuestCheckInOrchestrator
    from apps.reservations.models import GuestCheckInSessionCreatedFrom

    if not runtime.send_credentials_ok():
        return {"status": "missing_credentials"}

    session_result = GuestCheckInOrchestrator.ensure_session_and_link(
        reservation,
        created_from=GuestCheckInSessionCreatedFrom.WHATSAPP_AUTOCHECKIN,
        wa_id=row.wa_id,
    )
    if guest_checkin_web_only_enabled():
        body = render_autocheckin_web_checkin_message(
            reservation,
            checkin_url=session_result.url,
        )
        hint = "autocheckin web check-in"
        success_status = "web_checkin_sent"
    else:
        body = render_documents_message(reservation)
        hint = "autocheckin documents"
        success_status = "documents_sent"
    try:
        response = send_text_message(
            phone_number_id=runtime.phone_number_id,
            access_token=runtime.access_token,
            to_wa_id=row.wa_id,
            body=body,
        )
    except WhatsAppApiError as exc:
        logger.warning("WhatsApp documents reply failed message_id=%s: %s", row.pk, exc)
        return {"status": "send_failed", "detail": str(exc)}

    outbound_wamid = extract_outbound_wamid(response)
    if outbound_wamid:
        WhatsAppMessage.objects.create(
            tenant_id=row.tenant_id,
            integration=integration_row,
            reservation=reservation,
            wamid=outbound_wamid,
            wa_id=row.wa_id,
            phone_number_id=runtime.phone_number_id,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            message_type="text",
            body=body,
            raw_payload=response,
        )

    draft = GuestMessageDraft.objects.create(
        tenant_id=row.tenant_id,
        reservation=reservation,
        intent=GuestMessageIntent.CHECKIN,
        hint=hint,
        language="",
        llm_body_text=body,
        final_body_text=body,
        channel=GuestMessageChannel.WHATSAPP,
        sent_at=timezone.now(),
    )
    GuestOutboundMessage.objects.create(
        tenant_id=row.tenant_id,
        reservation=reservation,
        draft=draft,
        channel=GuestMessageChannel.WHATSAPP,
        body_text=body,
        status=GuestOutboundMessageStatus.SENT,
        to_phone=reservation.booker_phone or row.wa_id,
    )

    return _touch_whatsapp_autocheckin_reply(
        reservation,
        {"status": success_status, "outbound_wamid": outbound_wamid},
    )


def _maybe_notify_guest_message_inbound(row: WhatsAppMessage) -> None:
    if row.reservation_id is None:
        return

    from apps.core.tasks import notify_guest_message_inbound

    notify_guest_message_inbound.delay(
        row.reservation_id,
        channel="whatsapp",
        body_preview=_inbound_body_preview(row),
    )


@shared_task
def process_inbound_message(message_id: int, *, profile_name: str = "") -> dict:
    row = (
        WhatsAppMessage.objects.select_related(
            "integration",
            "tenant",
            "reservation",
            "inbound_routing",
            "inbound_routing__resolved_tenant",
            "inbound_routing__resolved_reservation",
        )
        .filter(pk=message_id, direction=WhatsAppMessage.Direction.INBOUND)
        .first()
    )
    if row is None:
        return {"status": "missing"}

    integration_row = row.integration
    if integration_row is None or not integration_row.is_active:
        return {"status": "no_integration"}

    runtime = WhatsAppRuntimeConfig.from_integration_dict(integration_row.get_config_dict())
    resolved_tenant_id = resolved_tenant_id_for_message(row)

    routing = getattr(row, "inbound_routing", None)

    # Operator flow before guest routing gate: platform WABA marks operator
    # images as unrouted (no guest phone match), which must not block Toni/Ante.
    if is_operator_wa_id(tenant_id=resolved_tenant_id, wa_id=row.wa_id) or is_operator_wa_id_any_tenant(
        wa_id=row.wa_id
    ):
        result = handle_operator_inbound(
            row=row,
            integration_row=integration_row,
            runtime=runtime,
            action_text=_inbound_action_text(row),
            button_id=inbound_interactive_button_id(row),
        )
        return {**result, "reservation_id": None, "operator_flow": True}

    if routing is not None and routing.status in (
        "unrouted",
        "ambiguous",
        "dismissed",
    ):
        return {
            "status": "routing_blocked",
            "routing_status": routing.status,
        }

    if routing is not None and routing.resolved_reservation_id:
        reservation = routing.resolved_reservation
        if row.reservation_id != reservation.pk:
            row.reservation = reservation
            row.save(update_fields=["reservation"])
    else:
        _link_inbound_to_reservation(row, tenant_id=resolved_tenant_id)
        reservation = row.reservation

    button_id = inbound_interactive_button_id(row)
    action_text = _inbound_action_text(row)

    if reservation is not None and action_text and row.message_type in ("text", "interactive"):
        from apps.communications.guest_language_inbound import on_guest_inbound_message

        on_guest_inbound_message(
            reservation,
            body=action_text,
            channel="whatsapp",
            received_at=row.created_at,
        )

    reply_result: dict | None = None

    from apps.integrations.whatsapp.autocheckin_maintenance import (
        send_autocheckin_maintenance_reply,
        whatsapp_autocheckin_maintenance_enabled,
    )

    maintenance_active = whatsapp_autocheckin_maintenance_enabled()

    if row.message_type in ("image", "document"):
        if maintenance_active and reservation is not None:
            reply_result = send_autocheckin_maintenance_reply(
                row=row,
                integration_row=integration_row,
                runtime=runtime,
                reservation=reservation,
            )
        elif not maintenance_active:
            from apps.integrations.whatsapp.guest_document_lifecycle import (
                guest_checkin_web_only_enabled,
            )
            from apps.integrations.whatsapp.whatsapp_web_checkin_redirect import (
                send_guest_web_checkin_link_reply,
            )

            if guest_checkin_web_only_enabled() and reservation is not None:
                reply_result = send_guest_web_checkin_link_reply(
                    row=row,
                    integration_row=integration_row,
                    runtime=runtime,
                    reservation=reservation,
                    hint="wa document redirect web check-in",
                )
            else:
                on_whatsapp_document_received.delay(row.pk)
                reply_result = {"status": "auto_reply_skipped", "reason": "media"}
        else:
            reply_result = {"status": "maintenance", "reason": "no_reservation"}
    elif row.message_type in _NON_ACTIONABLE_MESSAGE_TYPES:
        reply_result = {"status": "auto_reply_skipped", "reason": row.message_type}
    elif is_documents_all_yes_reply(button_id=button_id, text=action_text) or is_documents_all_no_reply(
        button_id=button_id, text=action_text
    ):
        reply_result = handle_whatsapp_document_batch_reply(row.pk)
    elif is_guest_auto_checkin_button(button_id=button_id, text=action_text):
        if maintenance_active and reservation is not None:
            reply_result = send_autocheckin_maintenance_reply(
                row=row,
                integration_row=integration_row,
                runtime=runtime,
                reservation=reservation,
            )
        elif maintenance_active:
            reply_result = {"status": "maintenance", "reason": "no_reservation"}
        elif reservation is None:
            reservation = resolve_guest_reservation(row=row, action_text=action_text)
            row.refresh_from_db()
        if reservation is not None:
            from apps.integrations.whatsapp.apply_reply import is_guest_checkin_acknowledged
            from apps.integrations.whatsapp.whatsapp_document_batch import (
                handle_autocheckin_during_document_batch,
            )

            if is_guest_checkin_acknowledged(reservation):
                reply_result = _touch_whatsapp_autocheckin_reply(
                    reservation,
                    reply_already_checked_in_autocheckin(
                        integration_row=integration_row,
                        runtime=runtime,
                        row=row,
                        reservation=reservation,
                    ),
                )
            else:
                batch_guard = handle_autocheckin_during_document_batch(
                    reservation=reservation,
                    integration_row=integration_row,
                    runtime=runtime,
                    row=row,
                )
                if batch_guard is not None:
                    reply_result = batch_guard
                else:
                    mark_autocheckin_engaged(reservation)
                    reply_result = _maybe_send_autocheckin_documents_reply(
                        row=row,
                        integration_row=integration_row,
                        runtime=runtime,
                        reservation=reservation,
                    )
                    from apps.integrations.whatsapp.autocheckin_docs_deadline import (
                        schedule_autocheckin_docs_deadline,
                    )
                    from apps.integrations.whatsapp.guest_document_lifecycle import (
                        guest_checkin_web_only_enabled,
                    )

                    if not guest_checkin_web_only_enabled():
                        schedule_autocheckin_docs_deadline(reservation)
        else:
            reply_result = handle_guest_autocheckin_inbound(
                row=row,
                integration_row=integration_row,
                runtime=runtime,
                action_text=action_text,
                reservation=None,
            )
    else:
        if reservation is not None and action_text:
            from apps.communications.guest_arrival_inbound import maybe_handle_guest_arrival_inbound
            from apps.reservations.models import Reservation as ReservationModel

            reservation = ReservationModel.objects.select_related("property", "tenant").get(
                pk=reservation.pk,
            )
            arrival_result = maybe_handle_guest_arrival_inbound(
                reservation,
                action_text,
                channel="whatsapp",
                reference_at=row.created_at,
            )
            if arrival_result is not None:
                reply_result = arrival_result
            else:
                from apps.communications.guest_invoice_inbound import (
                    maybe_handle_guest_invoice_inbound,
                )

                invoice_result = maybe_handle_guest_invoice_inbound(
                    reservation,
                    action_text,
                    channel="whatsapp",
                )
                if invoice_result is not None:
                    reply_result = invoice_result
                else:
                    from apps.communications.guest_parking_inbound import (
                        maybe_handle_guest_parking_inbound,
                    )

                    parking_result = maybe_handle_guest_parking_inbound(
                        reservation,
                        action_text,
                        channel="whatsapp",
                    )
                    if parking_result is not None:
                        reply_result = parking_result

        if reply_result is None:
            if maintenance_active and reservation is not None:
                reply_result = send_autocheckin_maintenance_reply(
                    row=row,
                    integration_row=integration_row,
                    runtime=runtime,
                    reservation=reservation,
                )
            elif not maintenance_active:
                reply_result = handle_guest_autocheckin_inbound(
                    row=row,
                    integration_row=integration_row,
                    runtime=runtime,
                    action_text=action_text,
                    reservation=reservation,
                )
        if reservation is None and row.reservation_id is not None:
            reservation = row.reservation

    _maybe_notify_guest_message_inbound(row)

    if row.reservation_id:
        touch_reservation_version(
            row.reservation_id,
            ReservationVersionScope.MESSAGES,
            reason="whatsapp_inbound",
        )

    result = {
        **reply_result,
        "reservation_id": reservation.pk if reservation else None,
    }
    logger.info(
        "WhatsApp inbound auto-reply message_id=%s reservation_id=%s status=%s reason=%s",
        message_id,
        result.get("reservation_id"),
        result.get("status"),
        result.get("reason"),
    )
    return result
