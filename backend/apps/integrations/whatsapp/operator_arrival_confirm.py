"""Toni arrival gate: operator prompts, guest timers, Da/Ne/time handler."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from celery import shared_task
from django.core.cache import cache
from django.db import connection, transaction
from django.utils import timezone

from apps.communications.canonical_store import create_with_canonical
from apps.communications.message_threads_service import _room_name_for_reservation
from apps.core.timezone import effective_guest_stated_arrival_at, property_local_now
from apps.integrations.models import IntegrationConfig, WhatsAppMessage
from apps.integrations.whatsapp.arrival_time_parse import (
    _property_tz,
    format_guest_stated_arrival_for_operator,
    parse_guest_stated_arrival,
    parse_operator_confirmed_arrival_time,
)
from apps.integrations.whatsapp.client import WhatsAppApiError, extract_outbound_wamid, send_interactive_button_message
from apps.integrations.whatsapp.integration_lookup import resolve_whatsapp_integration
from apps.integrations.whatsapp.runtime_config import WhatsAppRuntimeConfig
from apps.integrations.whatsapp.whatsapp_operator import operator_name_for_wa_id, operator_phones_for_tenant
from apps.integrations.whatsapp.whatsapp_post_checkin_reply import (
    guest_message_mentions_arrival,
    send_arrival_thanks_only,
)
from apps.properties.models import Property
from apps.reservations.models import (
    Reservation,
    WhatsAppArrivalConfirmSession,
    WhatsAppArrivalConfirmSessionStatus,
    WhatsAppArrivalConfirmTrigger,
)

logger = logging.getLogger(__name__)

# Toni WhatsApp "Došli svi gosti?" prompts (Celery + operator WA) — disabled; check-in via reception.
WHATSAPP_ARRIVAL_CONFIRM_PROMPTS_ENABLED = False

OPERATOR_ARRIVED_YES_PREFIX = "op_arrived_yes"
OPERATOR_ARRIVED_NO_PREFIX = "op_arrived_no"
OPERATOR_ARRIVED_YES_LABEL = "Da"
OPERATOR_ARRIVED_NO_LABEL = "Ne"

_TIMER_CACHE_PREFIX = "wa-arrival-timer"
_TIMER_CACHE_TTL = 86400

_ACTIVE_SESSION_STATUSES = frozenset(
    {
        WhatsAppArrivalConfirmSessionStatus.AWAITING_ARRIVED,
        WhatsAppArrivalConfirmSessionStatus.AWAITING_TIME,
    }
)


def operator_arrived_yes_button_id(reservation_id: int) -> str:
    return f"{OPERATOR_ARRIVED_YES_PREFIX}_{reservation_id}"


def operator_arrived_no_button_id(reservation_id: int) -> str:
    return f"{OPERATOR_ARRIVED_NO_PREFIX}_{reservation_id}"


def parse_operator_arrived_button(button_id: str) -> tuple[str | None, int | None]:
    raw = (button_id or "").strip()
    for prefix, answer in (
        (OPERATOR_ARRIVED_YES_PREFIX, "yes"),
        (OPERATOR_ARRIVED_NO_PREFIX, "no"),
    ):
        if raw == prefix:
            return answer, None
        if raw.startswith(f"{prefix}_"):
            suffix = raw[len(prefix) + 1 :]
            if suffix.isdigit():
                return answer, int(suffix)
    return None, None


def is_operator_arrived_yes_reply(*, button_id: str = "", text: str = "") -> bool:
    answer, _ = parse_operator_arrived_button(button_id)
    if answer == "yes":
        return True
    return (text or "").strip().lower() in {"da", "yes", "ja"}


def is_operator_arrived_no_reply(*, button_id: str = "", text: str = "") -> bool:
    answer, _ = parse_operator_arrived_button(button_id)
    if answer == "no":
        return True
    return (text or "").strip().lower() in {"ne", "no", "nein"}


def _timer_cache_key(reservation_id: int) -> str:
    return f"{_TIMER_CACHE_PREFIX}:{reservation_id}"


def _revoke_scheduled_timer(reservation_id: int) -> None:
    from config.celery import app

    cache_key = _timer_cache_key(reservation_id)
    task_id = cache.get(cache_key)
    if not task_id:
        return
    app.control.revoke(task_id, terminate=False)
    cache.delete(cache_key)


def _pg_advisory_xact_lock_reservation(reservation_id: int) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [reservation_id & 0x7FFFFFFF])


def _start_of_property_day(reservation: Reservation):
    now = property_local_now(reservation.property)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _is_checkin_day(reservation: Reservation) -> bool:
    """True when reservation.check_in is today in property-local timezone."""
    return reservation.check_in == property_local_now(reservation.property).date()


def _guest_reported_arrival(reservation: Reservation) -> bool:
    return bool(reservation.guest_stated_arrival_text.strip()) or reservation.guest_stated_arrival_at is not None


def _should_skip_arrival_prompt(reservation: Reservation) -> str | None:
    from apps.integrations.whatsapp.apply_reply import is_whatsapp_autocheckin_waived

    if reservation.status in {
        Reservation.Status.CHECKED_IN,
        Reservation.Status.CHECKED_OUT,
        Reservation.Status.CANCELED,
    }:
        return reservation.status
    if is_whatsapp_autocheckin_waived(reservation):
        return "waived"
    if not _is_checkin_day(reservation):
        return "not_checkin_day"
    return None


def _close_obsolete_arrival_sessions(*, tenant_id: int | None = None, reservation_id: int | None = None) -> int:
    """Close open arrival sessions when reservation is no longer expected or not on check-in day."""
    terminal_reservation_statuses = {
        Reservation.Status.CHECKED_IN,
        Reservation.Status.CHECKED_OUT,
        Reservation.Status.CANCELED,
    }
    open_statuses = {
        WhatsAppArrivalConfirmSessionStatus.AWAITING_ARRIVED,
        WhatsAppArrivalConfirmSessionStatus.AWAITING_TIME,
    }
    qs = WhatsAppArrivalConfirmSession.objects.filter(status__in=open_statuses).select_related(
        "reservation",
        "reservation__property",
    )
    if tenant_id is not None:
        qs = qs.filter(tenant_id=tenant_id)
    if reservation_id is not None:
        qs = qs.filter(reservation_id=reservation_id)

    closed = 0
    for session in qs:
        reservation = session.reservation
        if reservation.status in terminal_reservation_statuses or not _is_checkin_day(reservation):
            session.status = WhatsAppArrivalConfirmSessionStatus.DONE
            session.save(update_fields=["status", "updated_at"])
            closed += 1
    return closed


def _default_confirmed_arrival_at(reservation: Reservation) -> datetime:
    return effective_guest_stated_arrival_at(reservation)


def _finish_arrival_checkin(
    *,
    session: WhatsAppArrivalConfirmSession,
    confirmed_at: datetime,
    operator_wa_id: str,
    integration_row: IntegrationConfig,
    runtime: WhatsAppRuntimeConfig,
) -> dict:
    from apps.reservations.reservation_checkin_complete import perform_arrival_confirmed_checkin

    reservation = session.reservation
    time_stay_from = confirmed_at.astimezone(_property_tz(reservation)).strftime("%H:%M")
    outcome = perform_arrival_confirmed_checkin(
        reservation,
        time_stay_from=time_stay_from,
        operator_wa_id=operator_wa_id,
        confirmed_arrival_at=confirmed_at,
        integration_row=integration_row,
        runtime=runtime,
    )
    if outcome.get("status") in {"completed", "already_checked_in"}:
        session.status = WhatsAppArrivalConfirmSessionStatus.DONE
        session.confirmed_arrival_at = confirmed_at
        session.save(update_fields=["status", "confirmed_arrival_at", "updated_at"])
    return outcome


def _awaiting_arrived_prompt_exists_today(
    reservation: Reservation,
    trigger: str,
) -> bool:
    start = _start_of_property_day(reservation)
    return WhatsAppArrivalConfirmSession.objects.filter(
        reservation=reservation,
        trigger=trigger,
        prompted_at__gte=start,
        status=WhatsAppArrivalConfirmSessionStatus.AWAITING_ARRIVED,
    ).exists()


def _build_arrival_confirm_prompt_body(reservation: Reservation) -> str:
    room = _room_name_for_reservation(reservation) or "—"
    code = reservation.booking_code or reservation.external_id or str(reservation.pk)
    guest_plan = format_guest_stated_arrival_for_operator(reservation)
    lines = [
        f"#{reservation.pk} · soba {room} · {reservation.booker_name} · {code}",
    ]
    if guest_plan:
        if _guest_reported_arrival(reservation):
            lines.append(f"Gost javio: {guest_plan}")
        else:
            lines.append(f"Planirani check-in: {guest_plan}")
    lines.append("Došli svi gosti?")
    return "\n".join(lines)


def _build_arrival_confirm_text_body(reservation: Reservation) -> str:
    """Plain-text prompt when interactive buttons may not deliver (outside 24h session)."""
    return (
        f"{_build_arrival_confirm_prompt_body(reservation)}\n\n"
        "Odgovorite tekstom: Da ili Ne"
    )


def _operator_session_open(tenant_id: int, operator_wa_id: str) -> bool:
    """WhatsApp interactive/session messages need inbound from operator within 24h."""
    last_inbound = (
        WhatsAppMessage.objects.filter(
            tenant_id=tenant_id,
            wa_id=operator_wa_id,
            direction=WhatsAppMessage.Direction.INBOUND,
        )
        .order_by("-created_at")
        .first()
    )
    if last_inbound is None:
        return False
    return last_inbound.created_at >= timezone.now() - timedelta(hours=24)


def _notify_arrival_confirm_push(reservation: Reservation) -> dict:
    """Hospira reception push — works outside WhatsApp 24h customer-care window."""
    from apps.core.notifications import send_tenant_reception_push
    from apps.core.push_payload import reception_push_data

    room = _room_name_for_reservation(reservation) or "—"
    plan = format_guest_stated_arrival_for_operator(reservation) or "—"
    body = f"#{reservation.pk} {room} · {reservation.booker_name} · dolazak: {plan}"
    message_ids = send_tenant_reception_push(
        tenant_id=reservation.tenant_id,
        title="Potvrda dolaska",
        body=body,
        data=reception_push_data(
            event_type="arrival.confirm",
            reservation_id=reservation.pk,
            summary="Potvrdite dolazak gostiju (Da/Ne)",
        ),
    )
    return {"sent": len(message_ids)}


def _send_interactive_arrival_prompt(
    *,
    reservation: Reservation,
    integration_row: IntegrationConfig,
    runtime: WhatsAppRuntimeConfig,
    operator_wa_id: str,
) -> dict:
    interactive_body = _build_arrival_confirm_prompt_body(reservation)
    yes_id = operator_arrived_yes_button_id(reservation.pk)
    no_id = operator_arrived_no_button_id(reservation.pk)
    try:
        response = send_interactive_button_message(
            phone_number_id=runtime.phone_number_id,
            access_token=runtime.access_token,
            to_wa_id=operator_wa_id,
            body=interactive_body,
            buttons=[
                (yes_id, OPERATOR_ARRIVED_YES_LABEL),
                (no_id, OPERATOR_ARRIVED_NO_LABEL),
            ],
        )
    except WhatsAppApiError as exc:
        logger.warning(
            "Arrival confirm interactive failed reservation_id=%s wa_id=%s: %s",
            reservation.pk,
            operator_wa_id,
            exc,
        )
        return {"status": "send_failed", "channel": "interactive", "detail": str(exc)}

    outbound_wamid = extract_outbound_wamid(response)
    if outbound_wamid:
        create_with_canonical(WhatsAppMessage,
            tenant_id=reservation.tenant_id,
            integration=integration_row,
            reservation=reservation,
            wamid=outbound_wamid,
            wa_id=operator_wa_id,
            phone_number_id=runtime.phone_number_id,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            message_type="interactive",
            body=interactive_body,
            raw_payload=response,
        )
    return {
        "wa_id": operator_wa_id,
        "status": "sent",
        "channel": "interactive",
        "outbound_wamid": outbound_wamid,
    }


def _send_operator_template_reengagement(
    *,
    reservation: Reservation,
    integration_row: IntegrationConfig,
    runtime: WhatsAppRuntimeConfig,
    operator: dict[str, str],
    operator_wa_id: str,
) -> dict:
    """Approved template delivers outside 24h; opens session when operator replies."""
    from apps.integrations.whatsapp.client import send_template_message
    from apps.integrations.whatsapp.welcome_template import (
        build_welcome_template_parameters,
        resolve_welcome_template,
        welcome_header_image_url,
    )

    config = integration_row.get_config_dict()
    lang, params = build_welcome_template_parameters(reservation)
    operator_name = (operator.get("name") or "").strip()
    if operator_name:
        params[0] = operator_name.split()[0]
    resolved = resolve_welcome_template(language=lang, platform_config=config)
    template_name = resolved.template_name
    try:
        response = send_template_message(
            phone_number_id=runtime.phone_number_id,
            access_token=runtime.access_token,
            to_wa_id=operator_wa_id,
            template_name=template_name,
            language_code=resolved.meta_language,
            body_parameters=params,
            header_image_url=welcome_header_image_url(config),
        )
    except WhatsAppApiError as exc:
        logger.warning(
            "Arrival confirm template reengagement failed reservation_id=%s wa_id=%s: %s",
            reservation.pk,
            operator_wa_id,
            exc,
        )
        return {"status": "send_failed", "channel": "template", "detail": str(exc)}

    outbound_wamid = extract_outbound_wamid(response)
    if outbound_wamid:
        create_with_canonical(WhatsAppMessage,
            tenant_id=reservation.tenant_id,
            integration=integration_row,
            reservation=reservation,
            wamid=outbound_wamid,
            wa_id=operator_wa_id,
            phone_number_id=runtime.phone_number_id,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            message_type="template",
            body=f"template:{template_name}",
            raw_payload=response,
        )
    return {
        "wa_id": operator_wa_id,
        "status": "sent",
        "channel": "template",
        "outbound_wamid": outbound_wamid,
        "wa_session_closed": True,
    }


def send_arrival_confirm_prompt(
    reservation: Reservation,
    *,
    trigger: str,
    integration_row: IntegrationConfig | None = None,
    runtime: WhatsAppRuntimeConfig | None = None,
) -> dict:
    if not WHATSAPP_ARRIVAL_CONFIRM_PROMPTS_ENABLED:
        return {"status": "skipped", "reason": "disabled"}

    reservation = Reservation.objects.select_related("property", "tenant").get(pk=reservation.pk)
    _close_obsolete_arrival_sessions(reservation_id=reservation.pk)
    skip = _should_skip_arrival_prompt(reservation)
    if skip:
        return {"status": "skipped", "reason": skip}

    if _awaiting_arrived_prompt_exists_today(reservation, trigger):
        return {"status": "skipped", "reason": "already_prompted"}

    if integration_row is None or runtime is None:
        integration_row, runtime = resolve_whatsapp_integration(reservation.tenant)
    if integration_row is None or runtime is None:
        return {"status": "skipped", "reason": "no_integration"}

    operators = operator_phones_for_tenant(reservation.tenant_id)
    if not operators:
        return {"status": "skipped", "reason": "no_operators"}

    push_result = _notify_arrival_confirm_push(reservation)
    send_results: list[dict] = []

    from apps.integrations.whatsapp.phone import normalize_phone

    for operator in operators:
        operator_wa_id = normalize_phone(operator["phone"])
        if not operator_wa_id:
            continue

        if _operator_session_open(reservation.tenant_id, operator_wa_id):
            send_results.append(
                _send_interactive_arrival_prompt(
                    reservation=reservation,
                    integration_row=integration_row,
                    runtime=runtime,
                    operator_wa_id=operator_wa_id,
                )
            )
        else:
            send_results.append(
                _send_operator_template_reengagement(
                    reservation=reservation,
                    integration_row=integration_row,
                    runtime=runtime,
                    operator=operator,
                    operator_wa_id=operator_wa_id,
                )
            )

    wa_sent = any(item.get("status") == "sent" for item in send_results)
    if not wa_sent and push_result.get("sent", 0) == 0:
        return {"status": "send_failed", "operators": send_results, "push": push_result}

    now = timezone.now()
    effective_stated_at = effective_guest_stated_arrival_at(reservation)
    session = (
        WhatsAppArrivalConfirmSession.objects.filter(
            reservation=reservation,
            status__in=_ACTIVE_SESSION_STATUSES,
        )
        .order_by("-id")
        .first()
    )
    if session is None:
        session = WhatsAppArrivalConfirmSession.objects.create(
            tenant_id=reservation.tenant_id,
            reservation=reservation,
            status=WhatsAppArrivalConfirmSessionStatus.AWAITING_ARRIVED,
            trigger=trigger,
            guest_stated_arrival_text=reservation.guest_stated_arrival_text,
            guest_stated_arrival_at=effective_stated_at,
            prompted_at=now,
        )
    else:
        session.status = WhatsAppArrivalConfirmSessionStatus.AWAITING_ARRIVED
        session.trigger = trigger
        session.prompted_at = now
        session.responded_operator_wa_id = ""
        session.confirmed_arrival_at = None
        session.guest_stated_arrival_text = reservation.guest_stated_arrival_text
        session.guest_stated_arrival_at = effective_stated_at
        session.save(
            update_fields=[
                "status",
                "trigger",
                "prompted_at",
                "responded_operator_wa_id",
                "confirmed_arrival_at",
                "guest_stated_arrival_text",
                "guest_stated_arrival_at",
                "updated_at",
            ]
        )

    return {
        "status": "prompted",
        "session_id": session.pk,
        "operators": send_results,
        "push": push_result,
    }


def schedule_arrival_confirm_prompt(
    reservation: Reservation,
    *,
    trigger: str = WhatsAppArrivalConfirmTrigger.GUEST_DEADLINE_PLUS_30,
    run_at=None,
) -> dict:
    """Schedule Celery task for Toni prompt; revokes any prior timer for this reservation."""
    if not WHATSAPP_ARRIVAL_CONFIRM_PROMPTS_ENABLED:
        return {"status": "skipped", "reason": "disabled"}

    _revoke_scheduled_timer(reservation.pk)
    now = property_local_now(reservation.property)
    if run_at is None:
        countdown = 0
    else:
        countdown = max(0, int((run_at - now).total_seconds()))

    from config.celery import app

    task_id = f"wa-arrival-{reservation.pk}-{uuid.uuid4().hex[:12]}"
    cache.set(_timer_cache_key(reservation.pk), task_id, timeout=_TIMER_CACHE_TTL)
    arrival_confirm_guest_deadline_elapsed.apply_async(
        args=[reservation.pk, trigger],
        countdown=countdown,
        task_id=task_id,
    )
    return {"status": "scheduled", "countdown": countdown, "task_id": task_id}


def save_guest_stated_arrival(reservation: Reservation, *, text: str) -> datetime | None:
    from apps.communications.guest_arrival_inbound import save_stated_arrival

    return save_stated_arrival(reservation, text=text)


def maybe_handle_guest_arrival_time_inbound(
    *,
    row: WhatsAppMessage,
    reservation: Reservation,
    action_text: str,
) -> dict | None:
    from apps.communications.guest_arrival_inbound import maybe_handle_guest_arrival_inbound

    return maybe_handle_guest_arrival_inbound(
        reservation,
        action_text,
        channel="whatsapp",
    )


@shared_task
def arrival_confirm_guest_deadline_elapsed(
    reservation_id: int,
    trigger: str = WhatsAppArrivalConfirmTrigger.GUEST_DEADLINE_PLUS_30,
) -> dict:
    reservation = (
        Reservation.objects.select_related("property", "tenant")
        .filter(pk=reservation_id)
        .first()
    )
    if reservation is None:
        return {"status": "missing"}
    return send_arrival_confirm_prompt(reservation, trigger=trigger)


@shared_task
def send_nightly_arrival_confirm_prompts() -> dict:
    if not WHATSAPP_ARRIVAL_CONFIRM_PROMPTS_ENABLED:
        return {"status": "skipped", "reason": "disabled", "prompted": 0, "skipped": 0, "failed": 0}

    from apps.integrations.whatsapp.apply_reply import (
        is_document_checkin_complete,
        is_whatsapp_autocheckin_waived,
    )

    result = {"prompted": 0, "skipped": 0, "failed": 0}
    props = Property.objects.filter(whatsapp_autocheckin_enabled=True).select_related("tenant")
    for prop in props:
        now = property_local_now(prop)
        if not (23 <= now.hour <= 23 and now.minute < 15):
            continue
        target_date = now.date()
        qs = Reservation.objects.filter(
            tenant_id=prop.tenant_id,
            property=prop,
            check_in=target_date,
            status=Reservation.Status.EXPECTED,
        ).select_related("property", "tenant")
        _close_obsolete_arrival_sessions(tenant_id=prop.tenant_id)
        for reservation in qs:
            if is_whatsapp_autocheckin_waived(reservation):
                result["skipped"] += 1
                continue
            if not is_document_checkin_complete(reservation):
                result["skipped"] += 1
                continue
            if _should_skip_arrival_prompt(reservation):
                result["skipped"] += 1
                continue
            outcome = send_arrival_confirm_prompt(
                reservation,
                trigger=WhatsAppArrivalConfirmTrigger.NIGHTLY_23H,
            )
            status = outcome.get("status")
            if status == "prompted":
                result["prompted"] += 1
            elif status == "send_failed":
                result["failed"] += 1
            else:
                result["skipped"] += 1
    return result


def _get_session_for_operator_response(
    *,
    tenant_id: int,
    reservation_id: int | None,
    operator_wa_id: str,
    status: str,
) -> WhatsAppArrivalConfirmSession | None:
    qs = WhatsAppArrivalConfirmSession.objects.select_related(
        "reservation",
        "reservation__property",
        "reservation__tenant",
    ).filter(
        tenant_id=tenant_id,
        status=status,
    )
    if reservation_id is not None:
        qs = qs.filter(reservation_id=reservation_id)
    if status == WhatsAppArrivalConfirmSessionStatus.AWAITING_TIME:
        qs = qs.filter(responded_operator_wa_id=operator_wa_id)
    return qs.order_by("-prompted_at", "-id").first()


def _already_handled_message(reservation: Reservation) -> str:
    return f"Već obrađeno za #{reservation.pk}."


def handle_operator_arrival_confirm_inbound(
    *,
    row: WhatsAppMessage,
    integration_row: IntegrationConfig,
    runtime: WhatsAppRuntimeConfig,
    action_text: str,
    button_id: str = "",
) -> dict | None:
    from apps.integrations.whatsapp.whatsapp_operator_service import _send_operator_text

    answer, reservation_id = parse_operator_arrived_button(button_id)

    if answer in {"yes", "no"} and reservation_id is not None:
        with transaction.atomic():
            _pg_advisory_xact_lock_reservation(reservation_id)
            session = _get_session_for_operator_response(
                tenant_id=row.tenant_id,
                reservation_id=reservation_id,
                operator_wa_id=row.wa_id,
                status=WhatsAppArrivalConfirmSessionStatus.AWAITING_ARRIVED,
            )
            if session is None:
                reservation = Reservation.objects.filter(pk=reservation_id).first()
                if reservation is not None:
                    _send_operator_text(
                        integration_row=integration_row,
                        runtime=runtime,
                        operator_wa_id=row.wa_id,
                        body=_already_handled_message(reservation),
                        reservation=reservation,
                    )
                return {"status": "already_handled", "reservation_id": reservation_id}

            reservation = session.reservation
            if answer == "no":
                session.status = WhatsAppArrivalConfirmSessionStatus.DECLINED
                session.responded_operator_wa_id = row.wa_id
                session.save(update_fields=["status", "responded_operator_wa_id", "updated_at"])
                _send_operator_text(
                    integration_row=integration_row,
                    runtime=runtime,
                    operator_wa_id=row.wa_id,
                    body="OK, bez check-ina.",
                    reservation=reservation,
                )
                return {"status": "declined", "session_id": session.pk}

            session.responded_operator_wa_id = row.wa_id
            session.save(update_fields=["responded_operator_wa_id", "updated_at"])
            confirmed_at = _default_confirmed_arrival_at(reservation)
            with transaction.atomic():
                _pg_advisory_xact_lock_reservation(reservation_id)
                session.refresh_from_db()
                outcome = _finish_arrival_checkin(
                    session=session,
                    confirmed_at=confirmed_at,
                    operator_wa_id=row.wa_id,
                    integration_row=integration_row,
                    runtime=runtime,
                )
            return {**outcome, "session_id": session.pk}

    from apps.integrations.whatsapp.whatsapp_operator_service import (
        is_operator_docs_all_no_reply,
        is_operator_docs_all_yes_reply,
    )

    if is_operator_docs_all_yes_reply(button_id=button_id, text=action_text) or is_operator_docs_all_no_reply(
        button_id=button_id, text=action_text
    ):
        return None

    session = _get_session_for_operator_response(
        tenant_id=row.tenant_id,
        reservation_id=None,
        operator_wa_id=row.wa_id,
        status=WhatsAppArrivalConfirmSessionStatus.AWAITING_TIME,
    )
    if session is not None:
        parsed = parse_operator_confirmed_arrival_time(action_text, session.reservation)
        if parsed is None:
            _send_operator_text(
                integration_row=integration_row,
                runtime=runtime,
                operator_wa_id=row.wa_id,
                body="Nisam razumio vrijeme. Molim format HH:MM (npr. 19:30).",
                reservation=session.reservation,
            )
            return {"status": "time_parse_failed", "session_id": session.pk}

        with transaction.atomic():
            _pg_advisory_xact_lock_reservation(session.reservation_id)
            session.refresh_from_db()
            if session.status != WhatsAppArrivalConfirmSessionStatus.AWAITING_TIME:
                _send_operator_text(
                    integration_row=integration_row,
                    runtime=runtime,
                    operator_wa_id=row.wa_id,
                    body=_already_handled_message(session.reservation),
                    reservation=session.reservation,
                )
                return {"status": "already_handled", "session_id": session.pk}

            outcome = _finish_arrival_checkin(
                session=session,
                confirmed_at=parsed,
                operator_wa_id=row.wa_id,
                integration_row=integration_row,
                runtime=runtime,
            )
            return {**outcome, "session_id": session.pk}

    return None
