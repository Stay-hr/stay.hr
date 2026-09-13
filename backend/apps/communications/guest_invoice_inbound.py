"""Guest invoice inbound: ask for usable email, capture, confirm (all channels)."""

from __future__ import annotations

import logging

from django.utils import timezone

from apps.billing.exceptions import BillingRecipientError
from apps.billing.services.billing_recipient import RecipientSource
from apps.billing.services.billing_recipient_service import (
    get_open_recipient,
    update_open_recipient,
    upsert_open_recipient,
)
from apps.communications.guest_compose import (
    FOOTER,
    GREETING,
    HINT_GUEST_INVOICE_DETAILS_LINK,
    HINT_INVOICE_AMBIGUOUS,
    HINT_INVOICE_ASK_EMAIL,
    HINT_INVOICE_CONFIRM,
    SIGN_OFF,
)
from apps.communications.guest_invoice_details_distribute import send_guest_invoice_details_link
from apps.communications.guest_invoice_intent import classify_invoice_request
from apps.communications.guest_email_quality import extract_usable_invoice_emails
from apps.communications.guest_invoice_patterns import guest_message_requests_invoice
from apps.communications.guest_language_context import LanguageMode
from apps.communications.guest_language_resolver import GuestLanguageResolver
from apps.communications.guest_message_send import send_guest_message
from apps.communications.invoice_email_capture import (
    InvoiceEmailCaptureService,
    has_usable_invoice_recipient,
    is_waiting_for_invoice_email,
    maybe_timeout_invoice_email_waiting,
    start_waiting_for_invoice_email,
)
from apps.communications.models import GuestMessageChannel, GuestMessageDraft, GuestMessageIntent
from apps.core.timezone import property_local_now
from apps.reservations.models import Reservation

logger = logging.getLogger(__name__)

_CHANNEL_MAP = {
    "whatsapp": GuestMessageChannel.WHATSAPP,
    "email": GuestMessageChannel.EMAIL,
    "booking": GuestMessageChannel.BOOKING,
}

_ASK_BODY = {
    "hr": (
        "Račun možemo poslati e-mailom nakon odjave (checkout). "
        "Molimo pošaljite nam svoju e-mail adresu (ne Booking adresu) "
        "kako bismo je ažurirali u sustavu."
    ),
    "en": (
        "We can send the invoice by email after checkout. "
        "Please send us your email address (not a Booking.com address) "
        "so we can update it in our system."
    ),
    "fr": (
        "Nous pouvons vous envoyer la facture par e-mail après votre départ. "
        "Pourriez-vous nous fournir votre adresse e-mail (pas l’adresse Booking.com) "
        "afin que nous puissions la mettre à jour dans notre système ?"
    ),
    "de": (
        "Die Rechnung können wir per E-Mail nach dem Check-out senden. "
        "Bitte senden Sie uns Ihre E-Mail-Adresse (keine Booking.com-Adresse), "
        "damit wir sie in unserem System aktualisieren können."
    ),
    "es": (
        "Podemos enviar la factura por correo electrónico después del check-out. "
        "Por favor envíenos su dirección de correo (no la de Booking.com) "
        "para actualizarla en nuestro sistema."
    ),
}

_CONFIRM_BODY = {
    "hr": (
        "Hvala — e-mail adresa je ažurirana. "
        "Račun ćemo poslati na tu adresu nakon odjave."
    ),
    "en": (
        "Thank you — your email address has been updated. "
        "We will send the invoice to that address after checkout."
    ),
    "fr": (
        "Merci — votre adresse e-mail a été mise à jour. "
        "Nous enverrons la facture à cette adresse après votre départ."
    ),
    "de": (
        "Danke — Ihre E-Mail-Adresse wurde aktualisiert. "
        "Die Rechnung senden wir nach dem Check-out an diese Adresse."
    ),
    "es": (
        "Gracias — su correo electrónico ha sido actualizado. "
        "Enviaremos la factura a esa dirección después del check-out."
    ),
}

_AMBIGUOUS_BODY = {
    "hr": (
        "Primili smo više e-mail adresa. "
        "Molimo pošaljite samo jednu adresu na koju želite primiti račun."
    ),
    "en": (
        "We found more than one email address in your message. "
        "Please reply with only the single address where you want the invoice sent."
    ),
    "fr": (
        "Nous avons trouvé plusieurs adresses e-mail dans votre message. "
        "Merci de répondre avec une seule adresse pour l’envoi de la facture."
    ),
    "de": (
        "In Ihrer Nachricht haben wir mehrere E-Mail-Adressen gefunden. "
        "Bitte antworten Sie mit nur einer Adresse für den Rechnungsversand."
    ),
    "es": (
        "Encontramos más de una dirección de correo en su mensaje. "
        "Por favor responda solo con la dirección a la que desea la factura."
    ),
}

_ALREADY_HAVE_BODY = {
    "hr": (
        "Račun ćemo poslati e-mailom nakon odjave na adresu koju imamo u sustavu."
    ),
    "en": (
        "We will send the invoice by email after checkout to the address we have on file."
    ),
    "fr": (
        "Nous enverrons la facture par e-mail après votre départ "
        "à l’adresse que nous avons dans notre système."
    ),
    "de": (
        "Die Rechnung senden wir nach dem Check-out per E-Mail "
        "an die Adresse, die wir in unserem System haben."
    ),
    "es": (
        "Enviaremos la factura por correo después del check-out "
        "a la dirección que tenemos en el sistema."
    ),
}


def _text_for_lang(texts: dict[str, str], lang: str) -> str:
    base = (lang or "en").split("-")[0].lower()
    if base in texts and texts[base]:
        return texts[base]
    return texts.get("en") or next(iter(texts.values()), "")


def _format_reply(reservation: Reservation, reply_body: str, language: str) -> str:
    raw_name = (reservation.booker_name or "").strip()
    first_name = raw_name.split()[0] if raw_name else _text_for_lang(
        {"hr": "gost", "en": "guest"}, language
    )
    greeting = _text_for_lang(GREETING, language).format(name=first_name)
    sign_off = _text_for_lang(SIGN_OFF, language)
    property_name = reservation.property.name
    return "\n".join([greeting, "", reply_body, "", sign_off, property_name, "", FOOTER])


def _reply_sent_today(reservation: Reservation, hint: str) -> bool:
    now = property_local_now(reservation.property)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return GuestMessageDraft.objects.filter(
        reservation=reservation,
        hint=hint,
        sent_at__gte=start_of_day,
    ).exists()


def _send_invoice_auto_reply(
    reservation: Reservation,
    *,
    channel: str,
    body: str,
    reply_body: str,
    hint: str,
    language: str | None = None,
) -> dict:
    ctx = GuestLanguageResolver.resolve(
        reservation,
        mode=LanguageMode.REACTIVE,
        reply_language=language,
        message_text=body,
    )
    lang = ctx.language
    channel_enum = _CHANNEL_MAP.get(channel, GuestMessageChannel.EMAIL)
    full_body = _format_reply(reservation, reply_body, lang)

    draft = GuestMessageDraft.objects.create(
        tenant_id=reservation.tenant_id,
        reservation=reservation,
        intent=GuestMessageIntent.REPLY,
        hint=hint,
        llm_body_text=reply_body,
        final_body_text=full_body,
        language=lang,
        language_source=ctx.source.value,
        language_reason=(ctx.reason or "")[:255],
        channel=channel_enum,
    )

    try:
        send_guest_message(
            reservation=reservation,
            draft=draft,
            channel=channel_enum,
            body_text=full_body,
            api_application=None,
        )
    except ValueError as exc:
        logger.warning(
            "invoice auto-reply send failed reservation_id=%s channel=%s: %s",
            reservation.pk,
            channel,
            exc,
        )
        return {"status": "send_failed", "detail": str(exc)}

    draft.sent_at = timezone.now()
    draft.save(update_fields=["sent_at"])
    return {"status": "sent", "channel": channel, "hint": hint}


def _recipient_source_for_channel(channel: str) -> str:
    if channel == "whatsapp":
        return RecipientSource.WHATSAPP.value
    return RecipientSource.BOOKING_MESSAGE.value


def _apply_email_to_open_recipient(reservation: Reservation, email: str) -> None:
    row = get_open_recipient(reservation)
    if row is None or not (email or "").strip():
        return
    try:
        update_open_recipient(row, {"email": email})
    except BillingRecipientError:
        logger.info(
            "invoice inbound could not copy email onto billing recipient reservation_id=%s",
            reservation.pk,
        )


def _handle_business_invoice_request(
    reservation: Reservation,
    *,
    channel: str,
    body: str,
) -> dict:
    classification = classify_invoice_request(body)
    open_row = get_open_recipient(reservation)
    if not classification.is_business and open_row is None:
        return {"status": "not_business"}

    fields = classification.extracted_fields()
    excerpt = fields.pop("source_excerpt", "") or body[:240]
    try:
        upsert_open_recipient(
            reservation,
            fields,
            source=_recipient_source_for_channel(channel),
            source_excerpt=excerpt,
        )
    except BillingRecipientError:
        logger.info(
            "invoice inbound billing recipient upsert skipped reservation_id=%s",
            reservation.pk,
        )

    if _reply_sent_today(reservation, HINT_GUEST_INVOICE_DETAILS_LINK):
        return {
            "status": "guest_invoice_handled",
            "reply": {"status": "dedup_skipped"},
            "buyer_kind": "business",
        }

    reply = send_guest_invoice_details_link(
        reservation,
        channel=channel,
        created_from="invoice_inbound",
    )
    return {
        "status": "guest_invoice_handled",
        "reply": reply,
        "buyer_kind": "business",
        "waiting": False,
    }


def _handle_capture_result(
    reservation: Reservation,
    *,
    channel: str,
    body: str,
    capture: dict,
) -> dict:
    ctx = GuestLanguageResolver.resolve(
        reservation, mode=LanguageMode.REACTIVE, message_text=body
    )
    lang = ctx.language
    if capture.get("status") == "ambiguous":
        if not _reply_sent_today(reservation, HINT_INVOICE_AMBIGUOUS):
            reply = _send_invoice_auto_reply(
                reservation,
                channel=channel,
                body=body,
                reply_body=_text_for_lang(_AMBIGUOUS_BODY, lang),
                hint=HINT_INVOICE_AMBIGUOUS,
                language=lang,
            )
        else:
            reply = {"status": "dedup_skipped"}
        return {"status": "guest_invoice_handled", "capture": capture, "reply": reply}

    if capture.get("status") == "updated":
        email = (capture.get("email") or "").strip()
        _apply_email_to_open_recipient(reservation, email)
        if get_open_recipient(reservation) is not None:
            if _reply_sent_today(reservation, HINT_GUEST_INVOICE_DETAILS_LINK):
                reply = {"status": "dedup_skipped"}
            else:
                reply = send_guest_invoice_details_link(
                    reservation,
                    channel=channel,
                    created_from="invoice_inbound",
                )
            return {"status": "guest_invoice_handled", "capture": capture, "reply": reply}
        if not _reply_sent_today(reservation, HINT_INVOICE_CONFIRM):
            reply = _send_invoice_auto_reply(
                reservation,
                channel=channel,
                body=body,
                reply_body=_text_for_lang(_CONFIRM_BODY, lang),
                hint=HINT_INVOICE_CONFIRM,
                language=lang,
            )
        else:
            reply = {"status": "dedup_skipped"}
        return {"status": "guest_invoice_handled", "capture": capture, "reply": reply}

    return {"status": "guest_invoice_handled", "capture": capture}


def maybe_handle_guest_invoice_inbound(
    reservation: Reservation,
    body: str,
    *,
    channel: str,
) -> dict | None:
    """Ask for / capture usable invoice email; checkout remains the only issuer."""
    if reservation.status not in (
        Reservation.Status.EXPECTED,
        Reservation.Status.CHECKED_IN,
    ):
        return None

    if not reservation.property.guest_invoice_auto_reply_enabled:
        return None

    text = (body or "").strip()
    if not text:
        return None

    maybe_timeout_invoice_email_waiting(reservation)
    reservation.refresh_from_db(fields=["invoice_email_waiting_at", "booker_email"])

    # Active waiting cycle: try capture on every inbound.
    if is_waiting_for_invoice_email(reservation):
        capture = InvoiceEmailCaptureService.try_capture_while_waiting(reservation, text)
        if capture is not None:
            return _handle_capture_result(
                reservation, channel=channel, body=text, capture=capture
            )

    if guest_message_requests_invoice(text):
        business = _handle_business_invoice_request(
            reservation, channel=channel, body=text
        )
        if business.get("status") == "guest_invoice_handled":
            return business

        ctx = GuestLanguageResolver.resolve(
            reservation, mode=LanguageMode.REACTIVE, message_text=text
        )
        lang = ctx.language

        if has_usable_invoice_recipient(reservation):
            if _reply_sent_today(reservation, HINT_INVOICE_CONFIRM):
                return {
                    "status": "guest_invoice_handled",
                    "reply": {"status": "dedup_skipped"},
                }
            reply = _send_invoice_auto_reply(
                reservation,
                channel=channel,
                body=text,
                reply_body=_text_for_lang(_ALREADY_HAVE_BODY, lang),
                hint=HINT_INVOICE_CONFIRM,
                language=lang,
            )
            return {
                "status": "guest_invoice_handled",
                "reply": reply,
                "already_usable": True,
            }

        # Enter waiting, then capture email from the same message if unambiguous.
        start_waiting_for_invoice_email(reservation)
        reservation.refresh_from_db(fields=["invoice_email_waiting_at"])
        capture = InvoiceEmailCaptureService.try_capture_while_waiting(reservation, text)
        if capture is not None:
            return _handle_capture_result(
                reservation, channel=channel, body=text, capture=capture
            )

        if _reply_sent_today(reservation, HINT_INVOICE_ASK_EMAIL):
            return {"status": "guest_invoice_handled", "reply": {"status": "dedup_skipped"}}
        reply = _send_invoice_auto_reply(
            reservation,
            channel=channel,
            body=text,
            reply_body=_text_for_lang(_ASK_BODY, lang),
            hint=HINT_INVOICE_ASK_EMAIL,
            language=lang,
        )
        return {"status": "guest_invoice_handled", "reply": reply, "waiting": True}

    # Unrelated inbound with email(s) while not waiting — never overwrite.
    if extract_usable_invoice_emails(text) and not is_waiting_for_invoice_email(reservation):
        InvoiceEmailCaptureService.log_email_not_requested(reservation, text)

    return None
