from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import timedelta

from django.utils import timezone

from apps.communications.canonical_store import (
    create_with_canonical,
    link_raw_reservation,
    record_canonical_source,
)
from apps.communications.guest_language_context import LanguageMode
from apps.communications.guest_language_resolver import GuestLanguageResolver
from apps.core.timezone import property_local_now
from apps.integrations.models import IntegrationConfig, WhatsAppMessage
from apps.integrations.whatsapp.client import (
    WhatsAppApiError,
    extract_outbound_wamid,
    send_interactive_button_message,
    send_text_message,
)
from apps.integrations.whatsapp.reservation_lookup import (
    ACTIVE_STATUSES,
    extract_booking_code_from_text,
    find_reservation_by_booking_code,
    find_reservation_for_wa_id,
)
from apps.integrations.whatsapp.runtime_config import WhatsAppRuntimeConfig
from apps.reservations.models import (
    Reservation,
    WhatsAppGuestAutocheckinSession,
    WhatsAppGuestAutocheckinSessionStatus,
)

logger = logging.getLogger(__name__)

SESSION_TTL = timedelta(hours=24)


def _skip_auto_reply(*, reason: str) -> dict:
    """No WhatsApp auto-reply when we cannot answer the guest's message."""
    return {"status": "auto_reply_skipped", "reason": reason}
GUEST_AUTO_CHECKIN_BUTTON_ID = "guest_auto_checkin"

_GUEST_AUTO_CHECKIN_BUTTON_TITLE = {
    "hr": "Auto check-in",
    "en": "Auto check-in",
    "de": "Autocheck-in",
    "es": "Auto check-in",
    "fr": "Auto check-in",
}

_ASK_BOOKING_CODE = {
    "hr": (
        "Bok! Ne mogu pronaći rezervaciju za ovaj broj.\n\n"
        "Ako pišete s drugog telefona, pošaljite booking kod s Booking.com potvrde "
        "(obično 10 znamenaka).\n\n"
        "Molimo pošaljite booking kod."
    ),
    "en": (
        "Hi! I could not find a reservation for this number.\n\n"
        "If you are messaging from a different phone, send the booking code from your "
        "Booking.com confirmation (usually 10 digits).\n\n"
        "Please send your booking code."
    ),
    "de": (
        "Guten Tag! Für diese Nummer habe ich keine Reservierung gefunden.\n\n"
        "Wenn Sie von einer anderen Nummer schreiben, senden Sie die Buchungsnummer "
        "aus der Booking.com-Bestätigung (meist 10 Ziffern).\n\n"
        "Bitte senden Sie Ihre Buchungsnummer."
    ),
    "es": (
        "¡Hola! No encontramos una reserva para este número.\n\n"
        "Si escribe desde otro teléfono, envíe el código de reserva de la confirmación "
        "de Booking.com (normalmente 10 dígitos).\n\n"
        "Envíe su código de reserva."
    ),
    "fr": (
        "Bonjour ! Aucune réservation trouvée pour ce numéro.\n\n"
        "Si vous écrivez depuis un autre téléphone, envoyez le code de réservation "
        "de la confirmation Booking.com (généralement 10 chiffres).\n\n"
        "Veuillez envoyer votre code de réservation."
    ),
}

_BOOKING_CODE_NOT_FOUND = {
    "hr": (
        "Ne prepoznajem taj booking kod.\n\n"
        "Provjerite Booking.com potvrdu (10 znamenaka) i pošaljite kod ponovo, "
        "ili kontaktirajte recepciju."
    ),
    "en": (
        "I could not find a reservation with that booking code.\n\n"
        "Please check your Booking.com confirmation (10 digits) and try again, "
        "or contact reception."
    ),
    "de": (
        "Ich habe keine Reservierung mit dieser Buchungsnummer gefunden.\n\n"
        "Bitte prüfen Sie die Booking.com-Bestätigung (10 Ziffern) oder kontaktieren "
        "Sie die Rezeption."
    ),
    "es": (
        "No encontramos una reserva con ese código.\n\n"
        "Revise la confirmación de Booking.com (10 dígitos) o contacte con recepción."
    ),
    "fr": (
        "Aucune réservation avec ce code.\n\n"
        "Vérifiez la confirmation Booking.com (10 chiffres) ou contactez la réception."
    ),
}

_BOOKING_MATCHED_NOT_CHECKIN_DAY = {
    "hr": (
        "Pronašli smo rezervaciju {booking_code} ({check_in}–{check_out}) "
        "u {property_name}.\n\n"
        "Online check-in (Auto check-in) bit će dostupan na dan dolaska ({check_in})."
    ),
    "en": (
        "We found booking {booking_code} ({check_in}–{check_out}) at {property_name}.\n\n"
        "Online check-in (Auto check-in) will be available on your arrival day ({check_in})."
    ),
    "de": (
        "Wir haben Buchung {booking_code} ({check_in}–{check_out}) in {property_name} gefunden.\n\n"
        "Online-Check-in (Autocheck-in) ist am Ankunftstag ({check_in}) verfügbar."
    ),
    "es": (
        "Encontramos la reserva {booking_code} ({check_in}–{check_out}) en {property_name}.\n\n"
        "El check-in online (Auto check-in) estará disponible el día de llegada ({check_in})."
    ),
    "fr": (
        "Nous avons trouvé la réservation {booking_code} ({check_in}–{check_out}) "
        "à {property_name}.\n\n"
        "L’enregistrement en ligne (Auto check-in) sera disponible le jour d’arrivée ({check_in})."
    ),
}

_ALREADY_CHECKED_IN = {
    "hr": (
        "Bok {name}! Već ste prijavljeni — check-in je gotov.\n\n"
        "Ne trebate ponovno slati dokumente niti pritiskati Auto check-in. "
        "Uživajte u boravku!"
    ),
    "en": (
        "Hi {name}! You are already checked in.\n\n"
        "You do not need to send documents again or tap Auto check-in. "
        "Enjoy your stay!"
    ),
    "de": (
        "Guten Tag {name}! Sie sind bereits eingecheckt.\n\n"
        "Bitte senden Sie keine Dokumente erneut und tippen Sie nicht erneut auf Autocheck-in. "
        "Genießen Sie Ihren Aufenthalt!"
    ),
    "es": (
        "¡Hola {name}! Ya está registrado — el check-in está completado.\n\n"
        "No hace falta enviar documentos de nuevo ni pulsar Auto check-in. "
        "¡Disfrute de su estancia!"
    ),
    "fr": (
        "Bonjour {name} ! Vous êtes déjà enregistré(e) — le check-in est terminé.\n\n"
        "Inutile de renvoyer des documents ou d’appuyer à nouveau sur Auto check-in. "
        "Bon séjour !"
    ),
}

_AUTOCHECKIN_GREETING = {
    "hr": (
        "Bok {name}! Vidim rezervaciju {booking_code} "
        "({check_in}–{check_out}) u {property_name}.\n\n"
        "Pritisnite Auto check-in — dobit ćete link na web obrazac za unos podataka i dokumenata."
    ),
    "en": (
        "Hi {name}! I see booking {booking_code} "
        "({check_in}–{check_out}) at {property_name}.\n\n"
        "Tap Auto check-in — you'll receive a link to our web form for guest details and documents."
    ),
    "de": (
        "Guten Tag {name}! Ich sehe Buchung {booking_code} "
        "({check_in}–{check_out}) in {property_name}.\n\n"
        "Tippen Sie auf Autocheck-in — Sie erhalten einen Link zum Webformular für Gästedaten und Dokumente."
    ),
    "es": (
        "¡Hola {name}! Veo la reserva {booking_code} "
        "({check_in}–{check_out}) en {property_name}.\n\n"
        "Pulse Auto check-in — recibirá un enlace al formulario web para datos y documentos."
    ),
    "fr": (
        "Bonjour {name} ! Je vois la réservation {booking_code} "
        "({check_in}–{check_out}) à {property_name}.\n\n"
        "Appuyez sur Auto check-in — vous recevrez un lien vers le formulaire web pour les données et documents."
    ),
}


class GuestResolveOutcome:
    MATCHED_PHONE = "matched_phone"
    MATCHED_CODE = "matched_code"
    CODE_NOT_FOUND = "code_not_found"
    AWAITING_CODE = "awaiting_code"


@dataclass
class GuestReservationResolveResult:
    reservation: Reservation | None
    outcome: str
    attempted_code: str | None = None


def is_guest_auto_checkin_button(*, button_id: str = "", text: str = "") -> bool:
    if (button_id or "").strip() == GUEST_AUTO_CHECKIN_BUTTON_ID:
        return True
    from apps.integrations.whatsapp.tasks import is_auto_checkin_quick_reply

    return is_auto_checkin_quick_reply(text)


def _extract_code_attempt(action_text: str, session: WhatsAppGuestAutocheckinSession | None) -> str | None:
    code = extract_booking_code_from_text(action_text)
    if code:
        return code
    if session is not None:
        stripped = (action_text or "").strip()
        if stripped:
            return extract_booking_code_from_text(stripped) or stripped
    return None


def _text_for_lang(texts: dict[str, str], lang: str) -> str:
    base = (lang or "en").split("-")[0].lower()
    return texts.get(base) or texts.get("en") or next(iter(texts.values()))


def _button_title(lang: str) -> str:
    return _text_for_lang(_GUEST_AUTO_CHECKIN_BUTTON_TITLE, lang)


def _reservation_date_fmt(reservation: Reservation, lang: str) -> tuple[str, str]:
    fmt = "%d.%m.%Y" if lang == "hr" else "%Y-%m-%d"
    return reservation.check_in.strftime(fmt), reservation.check_out.strftime(fmt)


def _booking_matched_not_checkin_body(reservation: Reservation, lang: str) -> str:
    check_in, check_out = _reservation_date_fmt(reservation, lang)
    booking_code = reservation.booking_code or reservation.external_id or str(reservation.pk)
    return _text_for_lang(_BOOKING_MATCHED_NOT_CHECKIN_DAY, lang).format(
        booking_code=booking_code,
        check_in=check_in,
        check_out=check_out,
        property_name=reservation.property.name,
    )


def _session_expired(session: WhatsAppGuestAutocheckinSession) -> bool:
    return session.last_activity_at + SESSION_TTL < timezone.now()


def _get_awaiting_session(*, tenant_id: int, wa_id: str) -> WhatsAppGuestAutocheckinSession | None:
    session = (
        WhatsAppGuestAutocheckinSession.objects.filter(
            tenant_id=tenant_id,
            wa_id=wa_id,
            status=WhatsAppGuestAutocheckinSessionStatus.AWAITING_BOOKING_CODE,
        )
        .order_by("-last_activity_at", "id")
        .first()
    )
    if session is None:
        return None
    if _session_expired(session):
        session.delete()
        return None
    return session


def _clear_session(*, tenant_id: int, wa_id: str) -> None:
    WhatsAppGuestAutocheckinSession.objects.filter(
        tenant_id=tenant_id,
        wa_id=wa_id,
    ).delete()


def _ensure_awaiting_session(*, tenant_id: int, wa_id: str) -> WhatsAppGuestAutocheckinSession:
    session = _get_awaiting_session(tenant_id=tenant_id, wa_id=wa_id)
    if session is not None:
        session.last_activity_at = timezone.now()
        session.save(update_fields=["last_activity_at", "updated_at"])
        return session
    return WhatsAppGuestAutocheckinSession.objects.create(
        tenant_id=tenant_id,
        wa_id=wa_id,
        status=WhatsAppGuestAutocheckinSessionStatus.AWAITING_BOOKING_CODE,
    )


def _link_message_to_reservation(row: WhatsAppMessage, reservation: Reservation) -> None:
    if row.reservation_id == reservation.pk:
        record_canonical_source(row)
        return
    link_raw_reservation(row, reservation)


def _send_whatsapp_text(
    *,
    integration_row: IntegrationConfig,
    runtime: WhatsAppRuntimeConfig,
    row: WhatsAppMessage,
    reservation: Reservation | None,
    body: str,
) -> dict:
    try:
        response = send_text_message(
            phone_number_id=runtime.phone_number_id,
            access_token=runtime.access_token,
            to_wa_id=row.wa_id,
            body=body,
        )
    except WhatsAppApiError as exc:
        logger.warning("Guest autocheckin text failed message_id=%s: %s", row.pk, exc)
        return {"status": "send_failed", "detail": str(exc)}

    outbound_wamid = extract_outbound_wamid(response)
    if outbound_wamid:
        create_with_canonical(WhatsAppMessage,
            tenant_id=integration_row.tenant_id,
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
    return {"status": "sent", "outbound_wamid": outbound_wamid}


def _send_autocheckin_prompt(
    *,
    integration_row: IntegrationConfig,
    runtime: WhatsAppRuntimeConfig,
    row: WhatsAppMessage,
    reservation: Reservation,
) -> dict:
    ctx = GuestLanguageResolver.resolve(reservation, mode=LanguageMode.PROACTIVE)
    lang = ctx.language
    name = (reservation.booker_name or "").strip() or ("gost" if lang == "hr" else "guest")
    check_in, check_out = _reservation_date_fmt(reservation, lang)
    body = _text_for_lang(_AUTOCHECKIN_GREETING, lang).format(
        name=name.split()[0] if name else name,
        booking_code=reservation.booking_code or reservation.external_id or str(reservation.pk),
        check_in=check_in,
        check_out=check_out,
        property_name=reservation.property.name,
    )
    try:
        response = send_interactive_button_message(
            phone_number_id=runtime.phone_number_id,
            access_token=runtime.access_token,
            to_wa_id=row.wa_id,
            body=body,
            buttons=[(GUEST_AUTO_CHECKIN_BUTTON_ID, _button_title(lang))],
        )
    except WhatsAppApiError as exc:
        logger.warning("Guest autocheckin prompt failed message_id=%s: %s", row.pk, exc)
        return {"status": "send_failed", "detail": str(exc)}

    outbound_wamid = extract_outbound_wamid(response)
    if outbound_wamid:
        create_with_canonical(WhatsAppMessage,
            tenant_id=integration_row.tenant_id,
            integration=integration_row,
            reservation=reservation,
            wamid=outbound_wamid,
            wa_id=row.wa_id,
            phone_number_id=runtime.phone_number_id,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            message_type="interactive",
            body=body,
            raw_payload=response,
        )
    return {"status": "autocheckin_prompt_sent", "outbound_wamid": outbound_wamid}


def reply_already_checked_in_autocheckin(
    *,
    integration_row: IntegrationConfig,
    runtime: WhatsAppRuntimeConfig,
    row: WhatsAppMessage,
    reservation: Reservation,
) -> dict:
    """Guest tapped Auto check-in but reservation is already checked in."""
    from apps.integrations.whatsapp.apply_reply import waive_whatsapp_autocheckin

    waive_whatsapp_autocheckin(reservation)
    ctx = GuestLanguageResolver.resolve(reservation, mode=LanguageMode.PROACTIVE)
    lang = ctx.language
    raw_name = (reservation.booker_name or "").strip()
    first_name = raw_name.split()[0] if raw_name else ("gost" if lang == "hr" else "guest")
    body = _text_for_lang(_ALREADY_CHECKED_IN, lang).format(name=first_name)
    return _send_whatsapp_text(
        integration_row=integration_row,
        runtime=runtime,
        row=row,
        reservation=reservation,
        body=body,
    )


def _is_autocheckin_day(reservation: Reservation) -> bool:
    prop = reservation.property
    if not prop.whatsapp_autocheckin_enabled:
        return False
    now = property_local_now(prop)
    return reservation.check_in == now.date()


def try_resolve_guest_reservation(
    *,
    row: WhatsAppMessage,
    action_text: str,
) -> GuestReservationResolveResult:
    if row.reservation_id is not None:
        reservation = row.reservation
        if reservation is None:
            reservation = Reservation.objects.select_related("property", "tenant").get(
                pk=row.reservation_id,
            )
        return GuestReservationResolveResult(
            reservation=reservation,
            outcome=GuestResolveOutcome.MATCHED_PHONE,
        )

    reservation = find_reservation_for_wa_id(tenant_id=row.tenant_id, wa_id=row.wa_id)
    if reservation is not None:
        _link_message_to_reservation(row, reservation)
        _clear_session(tenant_id=row.tenant_id, wa_id=row.wa_id)
        return GuestReservationResolveResult(
            reservation=reservation,
            outcome=GuestResolveOutcome.MATCHED_PHONE,
        )

    session = _get_awaiting_session(tenant_id=row.tenant_id, wa_id=row.wa_id)
    code_attempt = _extract_code_attempt(action_text, session)

    if code_attempt:
        reservation = find_reservation_by_booking_code(tenant_id=row.tenant_id, code=code_attempt)
        if reservation is not None:
            _link_message_to_reservation(row, reservation)
            _clear_session(tenant_id=row.tenant_id, wa_id=row.wa_id)
            return GuestReservationResolveResult(
                reservation=reservation,
                outcome=GuestResolveOutcome.MATCHED_CODE,
                attempted_code=code_attempt,
            )
        return GuestReservationResolveResult(
            reservation=None,
            outcome=GuestResolveOutcome.CODE_NOT_FOUND,
            attempted_code=code_attempt,
        )

    return GuestReservationResolveResult(
        reservation=None,
        outcome=GuestResolveOutcome.AWAITING_CODE,
    )


def resolve_guest_reservation(
    *,
    row: WhatsAppMessage,
    action_text: str,
) -> Reservation | None:
    return try_resolve_guest_reservation(row=row, action_text=action_text).reservation


def _tenant_lang(integration_row: IntegrationConfig) -> str:
    return (integration_row.tenant.default_language or "hr").split("-")[0].lower()


def _handle_matched_reservation(
    *,
    row: WhatsAppMessage,
    integration_row: IntegrationConfig,
    runtime: WhatsAppRuntimeConfig,
    action_text: str,
    reservation: Reservation,
    resolve_outcome: str,
) -> dict:
    from apps.communications.whatsapp_autocheckin_tasks import mark_autocheckin_engaged
    from apps.integrations.whatsapp.apply_reply import (
        is_document_checkin_complete,
        is_guest_checkin_acknowledged,
        is_whatsapp_autocheckin_waived,
    )
    from apps.integrations.whatsapp.guest_docs_awaiting_arrival import docs_awaiting_arrival_already_sent
    from apps.integrations.whatsapp.reply import build_greeting
    from apps.integrations.whatsapp.whatsapp_post_checkin_reply import (
        arrival_thanks_sent_today,
        guest_message_mentions_arrival,
        guest_message_needs_post_checkin_reply,
        parse_post_checkin_message_hints,
        post_checkin_auto_reply_already_sent_today,
        send_arrival_thanks_only,
        send_post_checkin_whatsapp_auto_reply,
    )

    ctx = GuestLanguageResolver.resolve(
        reservation,
        mode=LanguageMode.REACTIVE,
        message_text=action_text,
    )
    lang = ctx.language

    from apps.integrations.whatsapp.autocheckin_docs_deadline import (
        maybe_reply_autocheckin_period_ended_inbound,
    )

    period_reply = maybe_reply_autocheckin_period_ended_inbound(
        reservation=reservation,
        action_text=action_text,
    )
    if period_reply is not None:
        return period_reply

    if is_whatsapp_autocheckin_waived(reservation):
        if guest_message_mentions_arrival(action_text) and not arrival_thanks_sent_today(reservation):
            return send_arrival_thanks_only(row=row, reservation=reservation)
        return {"status": "skipped", "reason": "autocheckin_waived"}

    if (
        reservation.status == Reservation.Status.EXPECTED
        and is_document_checkin_complete(reservation)
    ):
        if docs_awaiting_arrival_already_sent(reservation):
            return _skip_auto_reply(reason="no_matching_handler")

    if is_guest_checkin_acknowledged(reservation):
        if is_guest_auto_checkin_button(text=action_text):
            return reply_already_checked_in_autocheckin(
                integration_row=integration_row,
                runtime=runtime,
                row=row,
                reservation=reservation,
            )
        if (
            guest_message_needs_post_checkin_reply(action_text)
            and not post_checkin_auto_reply_already_sent_today(reservation)
        ):
            hints = parse_post_checkin_message_hints(action_text, reservation=reservation)
            return send_post_checkin_whatsapp_auto_reply(
                integration_row=integration_row,
                runtime=runtime,
                row=row,
                reservation=reservation,
                **hints,
            )
        return _skip_auto_reply(reason="no_matching_handler")

    from apps.integrations.whatsapp.whatsapp_document_batch import (
        handle_autocheckin_during_document_batch,
    )

    batch_guard = handle_autocheckin_during_document_batch(
        reservation=reservation,
        integration_row=integration_row,
        runtime=runtime,
        row=row,
    )
    if batch_guard is not None:
        return batch_guard

    if (
        reservation.whatsapp_autocheckin_engaged_at is not None
        and reservation.status == Reservation.Status.EXPECTED
        and not is_document_checkin_complete(reservation)
    ):
        return _skip_auto_reply(reason="autocheckin_awaiting_documents")

    mark_autocheckin_engaged(reservation)
    if _is_autocheckin_day(reservation):
        return _send_autocheckin_prompt(
            integration_row=integration_row,
            runtime=runtime,
            row=row,
            reservation=reservation,
        )

    if resolve_outcome == GuestResolveOutcome.MATCHED_CODE:
        body = _booking_matched_not_checkin_body(reservation, lang)
        return _send_whatsapp_text(
            integration_row=integration_row,
            runtime=runtime,
            row=row,
            reservation=reservation,
            body=body,
        )

    if runtime.auto_reply:
        body = build_greeting(
            integration_row=integration_row,
            reservation=reservation,
            profile_name="",
        )
        return _send_whatsapp_text(
            integration_row=integration_row,
            runtime=runtime,
            row=row,
            reservation=reservation,
            body=body,
        )

    body = _booking_matched_not_checkin_body(reservation, lang)
    return _send_whatsapp_text(
        integration_row=integration_row,
        runtime=runtime,
        row=row,
        reservation=reservation,
        body=body,
    )


def handle_guest_autocheckin_inbound(
    *,
    row: WhatsAppMessage,
    integration_row: IntegrationConfig,
    runtime: WhatsAppRuntimeConfig,
    action_text: str,
    reservation: Reservation | None,
) -> dict:
    from apps.integrations.whatsapp.autocheckin_maintenance import (
        send_autocheckin_maintenance_reply,
        whatsapp_autocheckin_maintenance_enabled,
    )

    if whatsapp_autocheckin_maintenance_enabled() and reservation is not None:
        return send_autocheckin_maintenance_reply(
            row=row,
            integration_row=integration_row,
            runtime=runtime,
            reservation=reservation,
        )

    if reservation is not None:
        resolve_result = GuestReservationResolveResult(
            reservation=reservation,
            outcome=GuestResolveOutcome.MATCHED_PHONE,
        )
    else:
        resolve_result = try_resolve_guest_reservation(row=row, action_text=action_text)

    if resolve_result.outcome == GuestResolveOutcome.CODE_NOT_FOUND:
        _ensure_awaiting_session(tenant_id=row.tenant_id, wa_id=row.wa_id)
        lang = _tenant_lang(integration_row)
        body = _text_for_lang(_BOOKING_CODE_NOT_FOUND, lang)
        return _send_whatsapp_text(
            integration_row=integration_row,
            runtime=runtime,
            row=row,
            reservation=None,
            body=body,
        )

    if resolve_result.reservation is not None:
        return _handle_matched_reservation(
            row=row,
            integration_row=integration_row,
            runtime=runtime,
            action_text=action_text,
            reservation=resolve_result.reservation,
            resolve_outcome=resolve_result.outcome,
        )

    _ensure_awaiting_session(tenant_id=row.tenant_id, wa_id=row.wa_id)
    lang = _tenant_lang(integration_row)
    body = _text_for_lang(_ASK_BOOKING_CODE, lang)
    return _send_whatsapp_text(
        integration_row=integration_row,
        runtime=runtime,
        row=row,
        reservation=None,
        body=body,
    )
