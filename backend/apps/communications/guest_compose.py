"""Guest message compose with deterministic check-in templates and optional LLM."""

from __future__ import annotations

import html
import json
import logging
from decimal import Decimal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from apps.ai.provider import (
    GuestComposeError,
    complete_chat,
    llm_configured,
    llm_model,
    prompt_version,
)
from apps.ai.translate import translate_text, translation_available
from apps.api.language import normalize_app_language
from apps.billing.models import Invoice
from apps.billing.services.payment import resolve_payment_method
from apps.communications.guest_arrival_policy import (
    after_hours_contact_phone,
    format_time_hm,
    is_after_hours_not_allowed,
    is_late_arrival,
)
from apps.communications.guest_language_constants import (
    LLM_REPLY_LANGS,
    TEMPLATE_LANGS,
    normalize_iso639_1,
)
from apps.communications.guest_language_context import GuestLanguageContext, LanguageMode
from apps.communications.guest_language_resolver import (
    GuestLanguageResolver,
    canonical_language_for_property,
)
from apps.communications.guest_email import _email_context
from apps.communications.guest_message_send import build_message_channels, channels_with_reply_default
from apps.communications.models import GuestMessageDraft, GuestMessageIntent
from apps.integrations.models import ChannexMessage, WhatsAppMessage
from apps.reservations.document_expectations import expected_document_count
from apps.reservations.document_intake_sides import MissingIdSide
from apps.reservations.models import Reservation
from apps.tenants.models import ApiApplication

logger = logging.getLogger(__name__)

HINT_CHECKIN_READY = "checkin ready"
HINT_OPERATOR_CHECKIN_COMPLETE = "operator checkin complete"
HINT_CHECKIN_COMPLETE_SUPPLEMENT = "checkin complete supplement"
HINT_ASK_ARRIVAL_TIME = "ask arrival time"
HINT_AUTO_CHECKIN_DOCS_EXPIRED = "autocheckin docs expired"
HINT_AUTO_CHECKIN_PERIOD_ENDED = "autocheckin period ended"
HINT_AUTOCHECKIN_WHATSAPP_INTRO = "whatsapp autocheckin intro"
HINT_EVISITOR_REGISTERED = "evisitor registered"
HINT_ID_MISSING_SIDES = "id missing sides"
HINT_DOCUMENTS_BATCH_ADDITIONAL_PHOTO = "documents batch additional photo"
HINT_DOCUMENTS_BATCH_COMPLETE_REPROMPT = "documents batch complete reprompt"
HINT_POST_CHECKIN_AUTO_REPLY = "post_checkin_auto_reply"
HINT_AUTOCHECKIN_WAIVED = "autocheckin waived"
HINT_AUTOCHECKIN_ARRIVAL_THANKS = "autocheckin arrival thanks"
HINT_DOCS_AWAITING_ARRIVAL = "docs awaiting arrival"
HINT_ARRIVAL_AUTO_REPLY = "arrival auto reply"
HINT_PARKING_AUTO_REPLY = "parking auto reply"
HINT_INVOICE_ASK_EMAIL = "invoice auto reply:ask_email"
HINT_INVOICE_CONFIRM = "invoice auto reply:confirm"
HINT_INVOICE_AMBIGUOUS = "invoice auto reply:ambiguous"
HINT_WHATSAPP_AUTOCHECKIN_MAINTENANCE = "whatsapp autocheckin maintenance"
HINT_GUEST_WEB_CHECKIN_REMINDER = "guest web checkin reminder"
HINT_GUEST_PORTAL_LINK = "guest_portal_link"
HINT_GUEST_PORTAL_LINK_URL = "guest_portal_link url"
HINT_GUEST_PAYMENT_LINK = "guest_payment_link"

FOOTER = "Managed by stay.hr — https://stay.hr/"

PAYMENT_TEXTS: dict[str, dict[str, str]] = {
    Invoice.PaymentMethod.BOOKING: {
        "hr": "Ukupna cijena: {amount} € — plaćeno u cijelosti putem Booking.com (Payments by Booking.com). Boravak ne plaćate na check-inu.",
        "en": "Total price: €{amount} — paid in full via Booking.com (Payments by Booking.com). No payment due for your stay at check-in.",
        "de": "Gesamtpreis: {amount} € — vollständig über Booking.com (Payments by Booking.com) bezahlt. Keine Zahlung für den Aufenthalt beim Check-in.",
        "es": "Precio total: {amount} € — pagado íntegramente a través de Booking.com (Payments by Booking.com). No hay que pagar la estancia en el check-in.",
        "fr": "Prix total : {amount} € — entièrement réglé via Booking.com (Payments by Booking.com). Aucun paiement du séjour à l’arrivée.",
    },
    Invoice.PaymentMethod.CASH: {
        "hr": "Ukupna cijena: {amount} € — plaćanje gotovinom na check-inu.",
        "en": "Total price: €{amount} — payment in cash at check-in.",
        "de": "Gesamtpreis: {amount} € — Zahlung bar beim Check-in.",
        "es": "Precio total: {amount} € — pago en efectivo en el check-in.",
        "fr": "Prix total : {amount} € — paiement en espèces à l’arrivée.",
    },
    Invoice.PaymentMethod.CARD: {
        "hr": "Ukupna cijena: {amount} € — plaćanje karticom na check-inu.",
        "en": "Total price: €{amount} — payment by card at check-in.",
        "de": "Gesamtpreis: {amount} € — Zahlung mit Karte beim Check-in.",
        "es": "Precio total: {amount} € — pago con tarjeta en el check-in.",
        "fr": "Prix total : {amount} € — paiement par carte à l’arrivée.",
    },
    Invoice.PaymentMethod.TRANSFER: {
        "hr": "Ukupna cijena: {amount} € — plaćanje transakcijskim računom (prema dogovoru).",
        "en": "Total price: €{amount} — payment by bank transfer (as agreed).",
        "de": "Gesamtpreis: {amount} € — Zahlung per Banküberweisung (wie vereinbart).",
        "es": "Precio total: {amount} € — pago por transferencia bancaria (según acuerdo).",
        "fr": "Prix total : {amount} € — paiement par virement bancaire (selon accord).",
    },
    Invoice.PaymentMethod.OTHER: {
        "hr": "Ukupna cijena: {amount} €.",
        "en": "Total price: €{amount}.",
        "de": "Gesamtpreis: {amount} €.",
        "es": "Precio total: {amount} €.",
        "fr": "Prix total : {amount} €.",
    },
}

from apps.communications.guest_compose_defaults import (
    ADDRESS_LABEL,
    AUTOCHECKIN_WA_ME_PREFILL,
    AUTOCHECKIN_WHATSAPP_INTRO_HEAD,
    AUTOCHECKIN_WHATSAPP_INTRO_TAIL,
    CHECKIN_AUTOMATION_FAILED,
    CHECKIN_LINE,
    CHECKIN_PARTIAL_DOCUMENTS,
    CHECKIN_READY_BODY,
    DEFAULT_ADDRESS,
    DEFAULT_GUEST_NAME,
    DOCUMENTS_BATCH_CONFIRM,
    DOCUMENTS_BATCH_CONFIRM_NO,
    DOCUMENTS_BATCH_CONFIRM_YES,
    DOCUMENTS_BATCH_ADDITIONAL_PHOTO,
    DOCUMENTS_BATCH_COMPLETE_REPROMPT,
    DOCUMENTS_TEXTS,
    ENTRANCE_IMAGE_CAPTION,
    ENTRANCE_TEXTS,
    EVISITOR_REGISTERED,
    GREETING,
    MAPS_LINK,
    MISSING_ID_SIDE_LABEL_NATIONAL_BACK,
    MISSING_ID_SIDE_LABEL_NATIONAL_FRONT,
    MISSING_GUEST_DOCUMENT_LINE,
    MISSING_ID_SIDE_LABEL_PASSPORT,
    MISSING_ID_SIDE_LINE,
    MISSING_ID_SIDES_INTRO,
    UNMATCHED_PERSON_LINE,
    UNREAD_PHOTOS_INTRO,
    MISSING_GUEST_HINT_OTHER_ADULT,
    OPERATOR_CHECKIN_COMPLETE_BODY,
    ARRIVAL_LATE_CONTACT,
    ARRIVAL_LATE_NOT_ALLOWED,
    ARRIVAL_TIME_SAVED_THANKS,
    ARRIVAL_WINDOW_FROM_ONLY,
    ARRIVAL_WINDOW_INFO,
    PARKING_TEXTS,
    POST_CHECKIN_ARRIVAL_THANKS,
    POST_CHECKIN_PARKING,
    POST_CHECKIN_WELCOME_EVENING,
    POST_CHECKIN_WELCOME_TODAY,
    RESERVATION_HEADER,
    ROOM_LABEL,
    SIGN_OFF,
    THANKS,
)
from apps.properties.guest_info import (
    build_guest_facts_for_llm,
    format_wifi_block,
    guest_maps_url,
    guest_text,
    render_parking_reply_text,
)


def append_guest_checkin_lang(url: str, lang: str | None) -> str:
    """Append or replace ``lang`` query param with a valid ISO 639-1 code."""
    code = normalize_iso639_1(lang)
    if not code or code not in LLM_REPLY_LANGS:
        return url
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "lang"]
    query.append(("lang", code))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _text_for_lang(texts: dict[str, str], lang: str) -> str:
    return texts.get(lang) or texts["en"]


def _property_guest_text(
    reservation: Reservation,
    key: str,
    lang: str,
    **fmt: object,
) -> str:
    return guest_text(reservation.property, key, lang, **fmt)


def _wifi_section(reservation: Reservation, lang: str) -> str:
    return format_wifi_block(reservation.property, lang)


def _message_lines_before_signoff(
    reservation: Reservation,
    context: dict,
    *,
    body_lines: list[str],
    include_wifi: bool = True,
) -> str:
    """Body lines, optional WiFi block, sign-off, property name, footer."""
    lang = context["language"]
    lines = list(body_lines)
    if include_wifi:
        wifi = _wifi_section(reservation, lang)
        if wifi:
            if lines and lines[-1] != "":
                lines.append("")
            lines.append(wifi)
    lines.extend(
        [
            "",
            _text_for_lang(SIGN_OFF, lang),
            context["property_name"],
            "",
            FOOTER,
        ]
    )
    return "\n".join(line for line in lines if line is not None)


def _normalize_hint(hint: str) -> str:
    return " ".join((hint or "").strip().lower().split())


def _draft_language_fields(ctx: GuestLanguageContext) -> dict[str, str]:
    return {
        "language": ctx.language[:8],
        "language_source": ctx.source.value,
        "language_reason": (ctx.reason or "")[:255],
    }


def _language_context_for_llm(ctx: GuestLanguageContext) -> dict[str, str | float]:
    return {
        "language": ctx.language,
        "source": ctx.source.value,
        "confidence": ctx.confidence,
        "mode": ctx.mode.value,
        "reason": ctx.reason,
    }


def _compose_context_for_llm(context: dict) -> dict:
    """Copy compose context with JSON-serializable values for LLM prompts."""
    payload = dict(context)
    language_context = payload.get("language_context")
    if isinstance(language_context, GuestLanguageContext):
        payload["language_context"] = _language_context_for_llm(language_context)
    return payload


def _resolve_language(
    reservation: Reservation,
    *,
    override: str | None = None,
    mode: LanguageMode = LanguageMode.PROACTIVE,
    message_text: str = "",
    reply_language: str | None = None,
) -> GuestLanguageContext:
    return GuestLanguageResolver.resolve(
        reservation,
        mode=mode,
        override=override,
        reply_language=reply_language,
        message_text=message_text,
    )


def render_guest_template(
    reservation: Reservation,
    render_fn,
    ctx: GuestLanguageContext,
) -> str:
    """
    Render guest template text for ctx.language.

    Fast path when localized guest_info exists; otherwise render canonical then translate.
    """
    lang = ctx.language
    if lang in TEMPLATE_LANGS:
        text = render_fn(lang)
        if (text or "").strip():
            return text
    canonical = canonical_language_for_property(reservation.property)
    body = render_fn(canonical)
    if lang != canonical and translation_available():
        return translate_text(body, lang)
    return body


def _lang_key(reservation: Reservation, override: str | None = None) -> str:
    return _resolve_language(reservation, override=override).language


def tenant_language_for_reservation(reservation: Reservation) -> str:
    return normalize_app_language(
        getattr(reservation.tenant, "default_language", None) or "hr"
    )


def body_text_for_tenant_preview(
    reservation: Reservation,
    guest_body: str,
    guest_lang: str,
) -> tuple[str, str]:
    """Return (tenant_language, body_text_tenant) for compose UI preview."""
    tenant_lang = tenant_language_for_reservation(reservation)
    body = (guest_body or "").strip()
    if not body:
        return tenant_lang, ""
    guest_base = (guest_lang or "").split("-")[0].lower()
    if guest_base == tenant_lang:
        return tenant_lang, body
    if not translation_available():
        return tenant_lang, body
    return tenant_lang, translate_text(body, tenant_lang)


def build_compose_response_fields(
    reservation: Reservation,
    *,
    body_text: str,
    guest_language: str,
) -> dict[str, str]:
    tenant_lang, body_tenant = body_text_for_tenant_preview(
        reservation,
        body_text,
        guest_language,
    )
    return {
        "body_text_tenant": body_tenant,
        "tenant_language": tenant_lang,
    }


def _format_amount(amount: Decimal | None) -> str:
    if amount is None:
        return "0,00"
    return f"{amount:.2f}".replace(".", ",")


def _payment_text(reservation: Reservation, lang: str) -> str:
    method = resolve_payment_method(reservation)
    templates = PAYMENT_TEXTS.get(method, PAYMENT_TEXTS[Invoice.PaymentMethod.OTHER])
    template = _text_for_lang(templates, lang)
    return template.format(amount=_format_amount(reservation.amount))


def _property_address(reservation: Reservation) -> str:
    addr = (reservation.property.address or "").strip()
    return addr or DEFAULT_ADDRESS


def _message_history(reservation: Reservation, limit: int = 10) -> list[dict]:
    rows: list[tuple[str, str, str, str]] = []

    for msg in WhatsAppMessage.objects.filter(reservation=reservation).order_by("-created_at")[:limit]:
        rows.append(
            (
                msg.created_at.isoformat(),
                "whatsapp",
                msg.direction,
                (msg.body or "").strip(),
            )
        )

    for msg in ChannexMessage.objects.filter(reservation=reservation).order_by("-created_at")[:limit]:
        direction = "inbound" if msg.sender == ChannexMessage.Sender.GUEST else "outbound"
        rows.append(
            (
                msg.created_at.isoformat(),
                "booking",
                direction,
                (msg.body or "").strip(),
            )
        )

    rows.sort(key=lambda r: r[0], reverse=True)
    return [
        {"at": at, "channel": channel, "direction": direction, "body": body}
        for at, channel, direction, body in rows[:limit]
        if body
    ]


def build_compose_context(
    reservation: Reservation,
    *,
    language: str | None = None,
    mode: LanguageMode = LanguageMode.PROACTIVE,
) -> dict:
    email_ctx = _email_context(reservation)
    ctx = _resolve_language(reservation, override=language, mode=mode)
    lang = ctx.language
    prop = reservation.property
    contact = prop.contact if isinstance(prop.contact, dict) else {}
    canonical = canonical_language_for_property(prop)
    facts_lang = lang if lang in TEMPLATE_LANGS else canonical

    return {
        "language": lang,
        "language_context": ctx,
        "guest_name": reservation.booker_name or "",
        "booking_code": reservation.booking_code or reservation.external_id or "",
        "property_name": reservation.property.name,
        "room_label": email_ctx.get("room_label") or "",
        "check_in_date": reservation.check_in.isoformat(),
        "check_in": email_ctx["check_in_display"],
        "check_out": email_ctx["check_out_display"],
        "check_in_time": email_ctx["check_in_time"],
        "check_out_time": email_ctx["check_out_time"],
        "adults_count": expected_document_count(reservation),
        "amount": _format_amount(reservation.amount),
        "currency": reservation.currency or "EUR",
        "payment_text": _payment_text(reservation, lang),
        "address": _property_address(reservation),
        "maps_link": guest_maps_url(prop),
        "guest_facts": build_guest_facts_for_llm(prop, facts_lang),
        "notes": (reservation.notes or "").strip(),
        "contact_phone": (contact.get("phone") or "").strip(),
        "message_history": _message_history(reservation),
    }


def _render_checkin_fallback(reservation: Reservation, context: dict) -> str:
    lang = context["language"]
    name = context["guest_name"] or _text_for_lang(DEFAULT_GUEST_NAME, lang)
    adults = context["adults_count"]
    entrance = _property_guest_text(reservation, "entrance", lang)
    parking = _property_guest_text(reservation, "parking", lang)
    documents = _property_guest_text(reservation, "documents", lang, adults=adults)
    checkin_line = _property_guest_text(
        reservation,
        "checkin_line",
        lang,
        check_in=context["check_in_date"],
        check_in_time=context["check_in_time"],
    )

    lines = [
        _text_for_lang(GREETING, lang).format(name=name),
        "",
        _text_for_lang(THANKS, lang).format(property_name=context["property_name"]),
        "",
        _text_for_lang(RESERVATION_HEADER, lang),
        f"• {context['booking_code']}" if context["booking_code"] else "",
        f"• {context['property_name']}",
    ]
    if context["room_label"]:
        lines.append(f"• {_text_for_lang(ROOM_LABEL, lang)}: {context['room_label']}")
    lines.extend(
        [
            f"• {context['check_in']} – {context['check_out']}",
            f"• {context['payment_text']}",
            "",
            checkin_line,
            "",
            _text_for_lang(ADDRESS_LABEL, lang),
            context["address"],
            context["maps_link"],
            "",
            entrance,
            "",
            parking,
            "",
            documents,
            "",
            _text_for_lang(SIGN_OFF, lang),
            context["property_name"],
            "",
            FOOTER,
        ]
    )
    return "\n".join(line for line in lines if line is not None)


def _render_checkin_ready_fallback(reservation: Reservation, context: dict) -> str:
    lang = context["language"]
    body = _property_guest_text(reservation, "checkin_ready", lang)
    return "\n".join(
        [
            body,
            "",
            _text_for_lang(SIGN_OFF, lang),
            context["property_name"],
            "",
            FOOTER,
        ]
    )


def render_checkin_ready_message(reservation: Reservation) -> str:
    """Deterministic post-apply thank-you message (same as compose checkin ready)."""
    context = build_compose_context(reservation)
    return _render_checkin_ready_fallback(reservation, context)


def render_post_checkin_guest_reply(
    reservation: Reservation,
    *,
    mentions_arrival: bool,
    mentions_parking: bool,
    evening_welcome: bool,
) -> str:
    """Auto-reply after documents are complete (parking / arrival questions)."""
    context = build_compose_context(reservation)
    lang = context["language"]
    raw_name = (context["guest_name"] or "").strip()
    first_name = raw_name.split()[0] if raw_name else _text_for_lang(DEFAULT_GUEST_NAME, lang)
    property_name = context["property_name"]

    lines = [_text_for_lang(GREETING, lang).format(name=first_name), ""]
    if mentions_arrival:
        lines.append(_property_guest_text(reservation, "post_checkin_arrival_thanks", lang))
        lines.append("")
    if mentions_parking:
        parking_line = render_parking_reply_text(
            reservation.property,
            lang,
            variant="post_checkin",
            reservation_notes=reservation.notes or "",
        )
        if parking_line:
            lines.append(parking_line)
            lines.append("")
    welcome_key = "post_checkin_welcome_evening" if evening_welcome else "post_checkin_welcome_today"
    lines.append(_property_guest_text(reservation, welcome_key, lang, property_name=property_name))
    wifi = _wifi_section(reservation, lang)
    if wifi:
        lines.extend(["", wifi])
    lines.extend(
        [
            "",
            _text_for_lang(SIGN_OFF, lang),
            property_name,
            "",
            FOOTER,
        ]
    )
    return "\n".join(lines)


def render_entrance_image_caption(reservation: Reservation) -> str:
    lang = _lang_key(reservation)
    return _property_guest_text(reservation, "entrance_image_caption", lang)


def _checkin_arrival_detail_lines(reservation: Reservation, context: dict) -> list[str]:
    lang = context["language"]
    checkin_line = _property_guest_text(
        reservation,
        "checkin_line",
        lang,
        check_in=context["check_in_date"],
        check_in_time=context["check_in_time"],
    )
    entrance = _property_guest_text(reservation, "entrance", lang)
    parking = render_parking_reply_text(
        reservation.property,
        lang,
        variant="standard",
        reservation_notes=reservation.notes or "",
    )
    return [checkin_line, "", entrance, "", parking]


def _checkin_ask_arrival_line(reservation: Reservation, lang: str) -> str:
    return _property_guest_text(reservation, "checkin_complete_ask_arrival", lang)


def _render_operator_checkin_complete_fallback(reservation: Reservation, context: dict) -> str:
    lang = context["language"]
    body = _property_guest_text(reservation, "operator_checkin_complete", lang)
    return _message_lines_before_signoff(
        reservation,
        context,
        body_lines=[body, "", *_checkin_arrival_detail_lines(reservation, context)],
    )


def render_operator_checkin_complete_message(reservation: Reservation) -> str:
    """Complete WA/email after guest or operator WhatsApp check-in (check-in time, entrance, parking, WiFi)."""
    context = build_compose_context(reservation)
    return _render_operator_checkin_complete_fallback(reservation, context)


def render_docs_awaiting_arrival_message(reservation: Reservation) -> str:
    """After guest docs apply on arrival day — saved docs, entrance/parking/WiFi (ask arrival sent separately)."""
    context = build_compose_context(reservation)
    lang = context["language"]
    body = _property_guest_text(reservation, "docs_awaiting_arrival", lang)
    return _message_lines_before_signoff(
        reservation,
        context,
        body_lines=[body, "", *_checkin_arrival_detail_lines(reservation, context)],
    )


def render_checkin_complete_supplement_message(reservation: Reservation) -> str:
    """Follow-up with arrival details only (no WiFi duplicate)."""
    context = build_compose_context(reservation)
    lang = context["language"]
    intro = _property_guest_text(reservation, "checkin_complete_supplement_intro", lang)
    return _message_lines_before_signoff(
        reservation,
        context,
        body_lines=[intro, "", *_checkin_arrival_detail_lines(reservation, context)],
        include_wifi=False,
    )


def render_ask_arrival_time_message(reservation: Reservation) -> str:
    """Short follow-up asking for expected arrival time."""
    context = build_compose_context(reservation)
    lang = context["language"]
    ask_arrival = _checkin_ask_arrival_line(reservation, lang)
    return _message_lines_before_signoff(
        reservation,
        context,
        body_lines=[ask_arrival],
        include_wifi=False,
    )


def render_autocheckin_period_ended_message(reservation: Reservation) -> str:
    """Guest engaged but did not send docs before property check-in time."""
    context = build_compose_context(reservation)
    lang = context["language"]
    body = _property_guest_text(reservation, "autocheckin_period_ended", lang)
    return _message_lines_before_signoff(
        reservation,
        context,
        body_lines=[body],
        include_wifi=False,
    )


def render_autocheckin_expired_short_message(reservation: Reservation) -> str:
    """Online auto check-in window closed; reception check-in on arrival."""
    context = build_compose_context(reservation)
    lang = context["language"]
    body = _property_guest_text(reservation, "autocheckin_expired_short", lang)
    return _message_lines_before_signoff(
        reservation,
        context,
        body_lines=[body],
        include_wifi=False,
    )


def autocheckin_wa_me_prefill(language: str, reservation: Reservation | None = None) -> str:
    lang = _lang_key_from_code(language)
    if reservation is not None:
        return _property_guest_text(reservation, "autocheckin_wa_me_prefill", lang)
    return AUTOCHECKIN_WA_ME_PREFILL.get(lang, AUTOCHECKIN_WA_ME_PREFILL["en"])


def _autocheckin_intro_booking_code(reservation: Reservation, context: dict) -> str:
    return context["booking_code"] or str(reservation.pk)


def _autocheckin_intro_cta_label(reservation: Reservation, lang: str) -> str:
    return autocheckin_wa_me_prefill(lang, reservation=reservation)


def _autocheckin_intro_plain_cta_line(*, reservation: Reservation, lang: str, wa_link: str) -> str:
    label = _autocheckin_intro_cta_label(reservation, lang)
    link = (wa_link or "").strip()
    return f"{label}: {link}" if link else label


def _whatsapp_cta_button_html(*, href: str, label: str) -> str:
    href_esc = html.escape(href, quote=True)
    label_esc = html.escape(label)
    return (
        '<table role="presentation" cellspacing="0" cellpadding="0" border="0">'
        "<tr><td>"
        f'<a href="{href_esc}" '
        'style="display:inline-block;padding:12px 24px;background-color:#25D366;'
        'color:#ffffff;text-decoration:none;font-weight:bold;">'
        f"{label_esc}</a>"
        "</td></tr></table>"
    )


def _render_autocheckin_intro_core(
    *,
    reservation: Reservation,
    lang: str,
    display_phone: str,
    booking_code: str,
    cta_line: str,
) -> str:
    head = _property_guest_text(
        reservation,
        "autocheckin_whatsapp_intro_head",
        lang,
        display_phone=display_phone or "",
    )
    tail = _property_guest_text(
        reservation,
        "autocheckin_whatsapp_intro_tail",
        lang,
        booking_code=booking_code,
    )
    return "\n\n".join(part for part in (head, cta_line, tail) if part)


def render_autocheckin_whatsapp_intro_email(
    reservation: Reservation,
    *,
    wa_link: str,
    display_phone: str,
) -> str:
    context = build_compose_context(reservation)
    lang = context["language"]
    body = _render_autocheckin_intro_core(
        reservation=reservation,
        lang=lang,
        display_phone=display_phone or "",
        booking_code=_autocheckin_intro_booking_code(reservation, context),
        cta_line=_autocheckin_intro_plain_cta_line(reservation=reservation, lang=lang, wa_link=wa_link),
    )
    return "\n".join(
        [
            body,
            "",
            _text_for_lang(SIGN_OFF, lang),
            context["property_name"],
            "",
            FOOTER,
        ]
    )


def render_autocheckin_whatsapp_intro_email_html(
    reservation: Reservation,
    *,
    wa_link: str,
    display_phone: str,
) -> str:
    context = build_compose_context(reservation)
    lang = context["language"]
    booking_code = _autocheckin_intro_booking_code(reservation, context)
    phone = html.escape(display_phone or "")
    code = html.escape(booking_code)
    property_name = html.escape(context["property_name"] or "")
    sign_off = html.escape(_text_for_lang(SIGN_OFF, lang))
    footer = html.escape(FOOTER)

    head = html.escape(
        _property_guest_text(
            reservation,
            "autocheckin_whatsapp_intro_head",
            lang,
            display_phone=display_phone or "",
        )
    ).replace("\n\n", "<br><br>")
    tail_raw = _property_guest_text(
        reservation,
        "autocheckin_whatsapp_intro_tail",
        lang,
        booking_code=booking_code,
    )
    tail_parts = tail_raw.split("\n\n", 1)
    utility_note = html.escape(tail_parts[0])
    booking_line = html.escape(tail_parts[1]) if len(tail_parts) > 1 else ""
    if booking_code and booking_line:
        booking_line = booking_line.replace(
            html.escape(booking_code),
            f"<strong>{html.escape(booking_code)}</strong>",
            1,
        )
    cta = _whatsapp_cta_button_html(
        href=wa_link or "",
        label=_autocheckin_intro_cta_label(reservation, lang),
    )

    return "\n".join(
        [
            f"<p>{head}</p>",
            f"<p>{cta}</p>",
            f"<p>{utility_note}</p>",
            f"<p>{booking_line}</p>" if booking_line else "",
            f"<p>{sign_off}<br>{property_name}</p>",
            f'<p style="color:#666;font-size:12px;">{footer}</p>',
        ]
    )


def _lang_key_from_code(language: str) -> str:
    base = (language or "en").split("-")[0].lower()
    if base in TEMPLATE_LANGS:
        return base
    return "en"


def _render_evisitor_registered_fallback(reservation: Reservation, context: dict) -> str:
    lang = context["language"]
    body = _property_guest_text(reservation, "evisitor_registered", lang)
    return _message_lines_before_signoff(reservation, context, body_lines=[body])


def render_evisitor_registered_message(reservation: Reservation) -> str:
    """WhatsApp message after all required guests are registered in eVisitor."""
    context = build_compose_context(reservation)
    return _render_evisitor_registered_fallback(reservation, context)


def _render_documents_fallback(reservation: Reservation, context: dict) -> str:
    lang = context["language"]
    adults = context["adults_count"]
    documents = _property_guest_text(reservation, "documents", lang, adults=adults)
    return "\n".join(
        [
            documents,
            "",
            _text_for_lang(SIGN_OFF, lang),
            context["property_name"],
            "",
            FOOTER,
        ]
    )


def render_documents_message(reservation: Reservation) -> str:
    """Document upload instructions after Auto check-in quick reply (legacy WA OCR)."""
    context = build_compose_context(reservation)
    return _render_documents_fallback(reservation, context)


def render_autocheckin_web_checkin_message(
    reservation: Reservation,
    *,
    checkin_url: str,
) -> str:
    """Web check-in link after Auto check-in (documents via booking.uzorita.hr/check-in/)."""
    context = build_compose_context(reservation)
    lang = context["language"]
    adults = context["adults_count"]
    localized_url = append_guest_checkin_lang(checkin_url, lang)
    body = _property_guest_text(
        reservation,
        "autocheckin_web_checkin",
        lang,
        adults=adults,
        checkin_url=localized_url,
    )
    return "\n".join(
        [
            body,
            "",
            _text_for_lang(SIGN_OFF, lang),
            context["property_name"],
            "",
            FOOTER,
        ]
    )


_GUEST_WEB_CHECKIN_REMINDER = {
    "hr": (
        "Podsjetnik: molimo unesite podatke gostiju prije dolaska ({check_in}) "
        "kako bi check-in na recepciji prošao brzo.\n\n"
        "Sigurni obrazac:\n{checkin_url}\n\n"
        "Rezervacija: {booking_code}"
    ),
    "en": (
        "Reminder: please submit your guest details before arrival ({check_in}) "
        "so check-in at reception is quick and smooth.\n\n"
        "Secure form:\n{checkin_url}\n\n"
        "Booking: {booking_code}"
    ),
    "de": (
        "Erinnerung: Bitte geben Sie Ihre Gästedaten vor der Anreise ein ({check_in}), "
        "damit der Check-in an der Rezeption schnell verläuft.\n\n"
        "Sicheres Formular:\n{checkin_url}\n\n"
        "Buchung: {booking_code}"
    ),
    "es": (
        "Recordatorio: envíe los datos de los huéspedes antes de la llegada ({check_in}) "
        "para agilizar el check-in en recepción.\n\n"
        "Formulario seguro:\n{checkin_url}\n\n"
        "Reserva: {booking_code}"
    ),
    "fr": (
        "Rappel : veuillez saisir les données des voyageurs avant l'arrivée ({check_in}) "
        "pour un enregistrement rapide à la réception.\n\n"
        "Formulaire sécurisé :\n{checkin_url}\n\n"
        "Réservation : {booking_code}"
    ),
}

_GUEST_WEB_CHECKIN_REMINDER_SUBJECT = {
    "hr": "Unesite podatke gostiju — {property_name}",
    "en": "Submit your guest details — {property_name}",
    "de": "Gästedaten eingeben — {property_name}",
    "es": "Envíe los datos de los huéspedes — {property_name}",
    "fr": "Saisissez les données des voyageurs — {property_name}",
}

_CHANNEX_GUEST_CHECKIN_LINK = {
    "hr": (
        "Hvala na rezervaciji!\n\n"
        "Podatke gostiju možete unijeti putem našeg web obrasca prije dolaska "
        "kako bi check-in na recepciji prošao brzo:\n"
        "{checkin_url}\n\n"
        "Rezervacija: {booking_code}"
    ),
    "en": (
        "Thank you for your booking!\n\n"
        "You can submit guest details via our secure web form before arrival "
        "so check-in at reception is quick and smooth:\n"
        "{checkin_url}\n\n"
        "Booking: {booking_code}"
    ),
    "de": (
        "Vielen Dank für Ihre Buchung!\n\n"
        "Sie können Gästedaten vor der Anreise über unser Webformular eingeben, "
        "damit der Check-in an der Rezeption schnell verläuft:\n"
        "{checkin_url}\n\n"
        "Buchung: {booking_code}"
    ),
    "es": (
        "¡Gracias por su reserva!\n\n"
        "Puede enviar los datos de los huéspedes a través de nuestro formulario web "
        "antes de la llegada para agilizar el check-in en recepción:\n"
        "{checkin_url}\n\n"
        "Reserva: {booking_code}"
    ),
    "fr": (
        "Merci pour votre réservation !\n\n"
        "Vous pouvez saisir les données des voyageurs via notre formulaire web "
        "avant l'arrivée pour un enregistrement rapide à la réception :\n"
        "{checkin_url}\n\n"
        "Réservation : {booking_code}"
    ),
}

_GUEST_PORTAL_LINK_CTA = {
    "hr": "Sve informacije o dolasku:",
    "en": "All arrival information:",
    "de": "Alle Anreiseinformationen:",
    "es": "Toda la información de llegada:",
    "fr": "Toutes les informations d'arrivée :",
    "it": "Tutte le informazioni sull'arrivo:",
}

_GUEST_PORTAL_LINK_CTA_LABEL = {
    "hr": "Otvori info portal",
    "en": "Open guest portal",
    "de": "Gästeportal öffnen",
    "es": "Abrir portal del huésped",
    "fr": "Ouvrir le portail invité",
    "it": "Apri portale ospite",
}

_GUEST_PORTAL_LINK_EMAIL_SUBJECT = {
    "hr": "Informacije o dolasku — {property_name}",
    "en": "Arrival information — {property_name}",
    "de": "Anreiseinformationen — {property_name}",
    "es": "Información de llegada — {property_name}",
    "fr": "Informations d'arrivée — {property_name}",
    "it": "Informazioni di arrivo — {property_name}",
}

_GUEST_PAYMENT_LINK_CTA = {
    "hr": "Upute za uplatu boravka:",
    "en": "Payment instructions for your stay:",
    "de": "Zahlungsanweisungen für Ihren Aufenthalt:",
    "es": "Instrucciones de pago para su estancia:",
    "fr": "Instructions de paiement pour votre séjour :",
    "it": "Istruzioni di pagamento per il soggiorno:",
}

_GUEST_PAYMENT_LINK_EMAIL_SUBJECT = {
    "hr": "Upute za uplatu — {property_name}",
    "en": "Payment instructions — {property_name}",
    "de": "Zahlungsanweisungen — {property_name}",
    "es": "Instrucciones de pago — {property_name}",
    "fr": "Instructions de paiement — {property_name}",
    "it": "Istruzioni di pagamento — {property_name}",
}


def guest_web_checkin_reminder_hint(*, days_before: int) -> str:
    return f"{HINT_GUEST_WEB_CHECKIN_REMINDER} d{max(int(days_before), 0)}"


def render_guest_web_checkin_reminder_message(
    reservation: Reservation,
    *,
    checkin_url: str,
) -> str:
    """Pre-arrival reminder with guest web check-in link."""
    context = build_compose_context(reservation)
    lang = context["language"]
    localized_url = append_guest_checkin_lang(checkin_url, lang)
    body = _text_for_lang(_GUEST_WEB_CHECKIN_REMINDER, lang).format(
        checkin_url=localized_url,
        check_in=context["check_in"],
        booking_code=context["booking_code"] or str(reservation.pk),
    )
    return "\n".join(
        [
            body,
            "",
            _text_for_lang(SIGN_OFF, lang),
            context["property_name"],
            "",
            FOOTER,
        ]
    )


def guest_web_checkin_reminder_email_subject(reservation: Reservation) -> str:
    context = build_compose_context(reservation)
    lang = context["language"]
    template = _text_for_lang(_GUEST_WEB_CHECKIN_REMINDER_SUBJECT, lang)
    return template.format(property_name=context["property_name"])


def render_channex_guest_checkin_link_message(
    reservation: Reservation,
    *,
    checkin_url: str,
) -> str:
    """OTA inbox message with guest web check-in link (Channex channel)."""
    context = build_compose_context(reservation)
    lang = context["language"]
    localized_url = append_guest_checkin_lang(checkin_url, lang)
    body = _text_for_lang(_CHANNEX_GUEST_CHECKIN_LINK, lang).format(
        checkin_url=localized_url,
        booking_code=context["booking_code"] or str(reservation.pk),
    )
    return "\n".join(
        [
            body,
            "",
            _text_for_lang(SIGN_OFF, lang),
            context["property_name"],
            "",
            FOOTER,
        ]
    )


def render_guest_portal_link_message(
    reservation: Reservation,
    *,
    portal_url: str = "",
) -> str:
    """CTA + localized portal URL + sign-off (plain text for all channels).

    Email keeps a separate HTML button; this plain body must still include
    ``/g/{token}?lang=…`` so reception timeline shows the link.
    """
    context = build_compose_context(reservation)
    lang = context["language"]
    body = _text_for_lang(_GUEST_PORTAL_LINK_CTA, lang)
    parts = [body, ""]
    if (portal_url or "").strip():
        parts.extend(
            [
                append_guest_checkin_lang(portal_url.strip(), lang),
                "",
            ]
        )
    parts.extend(
        [
            _text_for_lang(SIGN_OFF, lang),
            context["property_name"],
            "",
            FOOTER,
        ]
    )
    return "\n".join(parts)


def render_guest_portal_link_url_only(
    reservation: Reservation,
    *,
    portal_url: str,
) -> str:
    """Localized guest portal URL only (legacy helper; prefer single CTA+URL message)."""
    context = build_compose_context(reservation)
    return append_guest_checkin_lang(portal_url, context["language"])


def guest_portal_link_email_subject(reservation: Reservation) -> str:
    context = build_compose_context(reservation)
    lang = context["language"]
    template = _text_for_lang(_GUEST_PORTAL_LINK_EMAIL_SUBJECT, lang)
    return template.format(property_name=context["property_name"])


def render_guest_portal_link_email_html(
    reservation: Reservation,
    *,
    portal_url: str,
) -> str:
    """HTML email with portal CTA button (same button pattern as autocheck-in intro)."""
    context = build_compose_context(reservation)
    lang = context["language"]
    localized_url = append_guest_checkin_lang(portal_url, lang)
    property_name = html.escape(context["property_name"] or "")
    sign_off = html.escape(_text_for_lang(SIGN_OFF, lang))
    footer = html.escape(FOOTER)
    plain_cta = html.escape(_text_for_lang(_GUEST_PORTAL_LINK_CTA, lang))
    cta = _whatsapp_cta_button_html(
        href=localized_url or "",
        label=_text_for_lang(_GUEST_PORTAL_LINK_CTA_LABEL, lang),
    )
    return "\n".join(
        [
            f"<p>{plain_cta}</p>",
            f"<p>{cta}</p>",
            f"<p>{sign_off}<br>{property_name}</p>",
            f'<p style="color:#666;font-size:12px;">{footer}</p>',
        ]
    )


def render_guest_payment_link_message(
    reservation: Reservation,
    *,
    payment_url: str,
    payment_amount: str,
) -> str:
    """CTA + payment URL + amount (plain text for WhatsApp/email)."""
    context = build_compose_context(reservation)
    lang = context["language"]
    body = _text_for_lang(_GUEST_PAYMENT_LINK_CTA, lang)
    amount_line = {
        "hr": f"Ukupno za uplatu: {payment_amount} € (uključuje boravišnu pristojbu).",
        "en": f"Total payment: €{payment_amount} (includes tourist tax).",
        "de": f"Gesamtbetrag: {payment_amount} € (inkl. Kurtaxe).",
        "es": f"Importe total: {payment_amount} € (incluye tasa turística).",
        "fr": f"Montant total : {payment_amount} € (taxe de séjour incluse).",
        "it": f"Importo totale: {payment_amount} € (tassa di soggiorno inclusa).",
    }
    parts = [body, _text_for_lang(amount_line, lang), ""]
    if (payment_url or "").strip():
        parts.extend(
            [
                append_guest_checkin_lang(payment_url.strip(), lang),
                "",
            ]
        )
    parts.extend(
        [
            _text_for_lang(SIGN_OFF, lang),
            context["property_name"],
            "",
            FOOTER,
        ]
    )
    return "\n".join(parts)


def guest_payment_link_email_subject(reservation: Reservation) -> str:
    context = build_compose_context(reservation)
    lang = context["language"]
    template = _text_for_lang(_GUEST_PAYMENT_LINK_EMAIL_SUBJECT, lang)
    return template.format(property_name=context["property_name"])


def render_guest_payment_link_email_html(
    reservation: Reservation,
    *,
    payment_url: str,
    payment_amount: str,
) -> str:
    context = build_compose_context(reservation)
    lang = context["language"]
    localized_url = append_guest_checkin_lang(payment_url, lang)
    property_name = html.escape(context["property_name"] or "")
    sign_off = html.escape(_text_for_lang(SIGN_OFF, lang))
    footer = html.escape(FOOTER)
    plain_cta = html.escape(_text_for_lang(_GUEST_PAYMENT_LINK_CTA, lang))
    amount_line = html.escape(
        _text_for_lang(
            {
                "hr": f"Ukupno za uplatu: {payment_amount} € (uključuje boravišnu pristojbu).",
                "en": f"Total payment: €{payment_amount} (includes tourist tax).",
                "de": f"Gesamtbetrag: {payment_amount} € (inkl. Kurtaxe).",
                "es": f"Importe total: {payment_amount} € (incluye tasa turística).",
                "fr": f"Montant total : {payment_amount} € (taxe de séjour incluse).",
                "it": f"Importo totale: {payment_amount} € (tassa di soggiorno inclusa).",
            },
            lang,
        )
    )
    cta = _whatsapp_cta_button_html(
        href=localized_url or "",
        label=_text_for_lang(_GUEST_PORTAL_LINK_CTA_LABEL, lang),
    )
    return "\n".join(
        [
            f"<p>{plain_cta}</p>",
            f"<p>{amount_line}</p>",
            f"<p>{cta}</p>",
            f"<p>{sign_off}<br>{property_name}</p>",
            f'<p style="color:#666;font-size:12px;">{footer}</p>',
        ]
    )


def render_documents_batch_confirm_message(reservation: Reservation) -> str:
    context = build_compose_context(reservation)
    return _property_guest_text(reservation, "documents_batch_confirm", context["language"])


def documents_batch_confirm_button_labels(reservation: Reservation) -> tuple[str, str]:
    context = build_compose_context(reservation)
    lang = context["language"]
    return (
        _property_guest_text(reservation, "documents_batch_confirm_yes", lang),
        _property_guest_text(reservation, "documents_batch_confirm_no", lang),
    )


def render_documents_batch_additional_photo_message(reservation: Reservation) -> str:
    context = build_compose_context(reservation)
    lang = context["language"]
    body = _property_guest_text(reservation, "documents_batch_additional_photo", lang)
    return "\n".join(
        [
            body,
            "",
            _text_for_lang(SIGN_OFF, lang),
            context["property_name"],
            "",
            FOOTER,
        ]
    )


def render_documents_batch_complete_reprompt_message(reservation: Reservation) -> str:
    context = build_compose_context(reservation)
    lang = context["language"]
    body = _property_guest_text(reservation, "documents_batch_complete_reprompt", lang)
    return "\n".join(
        [
            body,
            "",
            _text_for_lang(SIGN_OFF, lang),
            context["property_name"],
            "",
            FOOTER,
        ]
    )


def _render_checkin_partial_fallback(reservation: Reservation, context: dict) -> str:
    lang = context["language"]
    body = _property_guest_text(reservation, "checkin_partial_documents", lang)
    return "\n".join(
        [
            body,
            "",
            _text_for_lang(SIGN_OFF, lang),
            context["property_name"],
            "",
            FOOTER,
        ]
    )


def render_checkin_partial_documents_message(reservation: Reservation) -> str:
    context = build_compose_context(reservation)
    return _render_checkin_partial_fallback(reservation, context)


def _missing_id_side_label(*, reservation: Reservation, lang: str, gap: MissingIdSide) -> str:
    if gap.is_passport:
        return _property_guest_text(reservation, "missing_id_side_label_passport", lang)
    if gap.side == "back":
        return _property_guest_text(reservation, "missing_id_side_label_national_back", lang)
    return _property_guest_text(reservation, "missing_id_side_label_national_front", lang)


def _render_missing_id_sides_fallback(reservation: Reservation, context: dict, gaps: list[MissingIdSide]) -> str:
    lang = context["language"]
    line_template = _property_guest_text(reservation, "missing_id_side_line", lang)
    lines = [
        _property_guest_text(reservation, "missing_id_sides_intro", lang),
        "",
        *[
            line_template.format(name=gap.guest_name, side_label=_missing_id_side_label(reservation=reservation, lang=lang, gap=gap))
            for gap in gaps
        ],
        "",
        _text_for_lang(SIGN_OFF, lang),
        context["property_name"],
        "",
        FOOTER,
    ]
    return "\n".join(lines)


def render_missing_id_sides_message(reservation: Reservation, gaps: list[MissingIdSide]) -> str:
    context = build_compose_context(reservation)
    return _render_missing_id_sides_fallback(reservation, context, gaps)


def render_document_intake_incomplete_message(
    reservation: Reservation,
    completeness,
    *,
    image_count: int | None = None,
) -> str:
    """Concrete follow-up: missing guest slots, ID sides, or unmatched OCR persons."""
    context = build_compose_context(reservation)
    lang = context["language"]
    line_items: list[str] = []

    unassigned = getattr(completeness, "unassigned_image_indices", []) or []
    total_photos = image_count if image_count is not None else len(unassigned)

    if unassigned:
        missing_hint = _property_guest_text(
            reservation, "missing_guest_hint_other_adult", lang
        )
        if completeness.missing_guests:
            names = [g.guest_name for g in completeness.missing_guests if g.guest_name]
            if len(names) == 1:
                missing_hint = names[0]
        unread_intro = _property_guest_text(reservation, "unread_photos_intro", lang)
        line_items.append(
            unread_intro.format(
                total=total_photos,
                unread=len(unassigned),
                missing_guest_hint=missing_hint,
            )
        )
        line_items.append("")
    else:
        line_items.append(_property_guest_text(reservation, "missing_id_sides_intro", lang))
        line_items.append("")

    guest_line = _property_guest_text(reservation, "missing_guest_document_line", lang)
    for gap in completeness.missing_guests:
        line_items.append(guest_line.format(name=gap.guest_name))

    side_line_template = _property_guest_text(reservation, "missing_id_side_line", lang)
    for gap in completeness.missing_sides:
        side_label = _missing_id_side_label(reservation=reservation, lang=lang, gap=gap)
        line_items.append(side_line_template.format(name=gap.guest_name, side_label=side_label))

    unmatched_line = _property_guest_text(reservation, "unmatched_person_line", lang)
    for person in completeness.unmatched_persons:
        line_items.append(unmatched_line.format(name=person.display_name))

    line_items.extend(
        [
            "",
            _text_for_lang(SIGN_OFF, lang),
            context["property_name"],
            "",
            FOOTER,
        ]
    )
    return "\n".join(line_items)


def _render_checkin_automation_failed_fallback(reservation: Reservation, context: dict) -> str:
    lang = context["language"]
    body = _property_guest_text(reservation, "checkin_automation_failed", lang)
    return "\n".join(
        [
            body,
            "",
            _text_for_lang(SIGN_OFF, lang),
            context["property_name"],
            "",
            FOOTER,
        ]
    )


def render_checkin_automation_failed_message(reservation: Reservation) -> str:
    context = build_compose_context(reservation)
    return _render_checkin_automation_failed_fallback(reservation, context)


def render_whatsapp_autocheckin_maintenance_message(reservation: Reservation) -> str:
    context = build_compose_context(reservation)
    lang = context["language"]
    body = _property_guest_text(reservation, "whatsapp_autocheckin_maintenance", lang)
    return "\n".join(
        [
            body,
            "",
            _text_for_lang(SIGN_OFF, lang),
            context["property_name"],
            "",
            FOOTER,
        ]
    )


def _render_autocheckin_waived_fallback(reservation: Reservation, context: dict) -> str:
    lang = context["language"]
    raw_name = (context["guest_name"] or "").strip()
    first_name = raw_name.split()[0] if raw_name else _text_for_lang(DEFAULT_GUEST_NAME, lang)
    property_name = context["property_name"]
    body = _property_guest_text(
        reservation,
        "autocheckin_waived",
        lang,
        first_name=first_name,
        property_name=property_name,
    )
    return "\n".join(
        [
            body,
            "",
            _text_for_lang(SIGN_OFF, lang),
            property_name,
            "",
            FOOTER,
        ]
    )


def render_autocheckin_waived_message(reservation: Reservation) -> str:
    context = build_compose_context(reservation)
    return _render_autocheckin_waived_fallback(reservation, context)


def render_arrival_thanks_message(reservation: Reservation) -> str:
    """Short thanks when guest shares arrival time (waived auto check-in flow)."""
    context = build_compose_context(reservation)
    lang = context["language"]
    property_name = context["property_name"]
    body = _property_guest_text(reservation, "post_checkin_arrival_thanks", lang)
    return "\n".join(
        [
            body,
            "",
            _text_for_lang(SIGN_OFF, lang),
            property_name,
            "",
            FOOTER,
        ]
    )


def _arrival_reply_footer(reservation: Reservation, lang: str) -> list[str]:
    property_name = reservation.property.name
    return ["", _text_for_lang(SIGN_OFF, lang), property_name, "", FOOTER]


def _arrival_window_params(reservation: Reservation) -> dict[str, str]:
    prop = reservation.property
    earliest = format_time_hm(prop.check_in_time)
    latest = format_time_hm(prop.check_in_latest_time)
    return {
        "check_in_time": earliest,
        "check_in_latest_time": latest,
        "contact_phone": after_hours_contact_phone(prop),
    }


def render_arrival_late_inquiry_message(
    reservation: Reservation,
    *,
    message_text: str = "",
) -> str:
    """Reply when guest asks about late check-in without stating a time."""
    ctx = _resolve_language(
        reservation,
        mode=LanguageMode.REACTIVE,
        message_text=message_text,
    )
    lang = ctx.language
    params = _arrival_window_params(reservation)
    if params["check_in_latest_time"]:
        body = _text_for_lang(ARRIVAL_WINDOW_INFO, lang).format(**params)
    else:
        body = _text_for_lang(ARRIVAL_WINDOW_FROM_ONLY, lang).format(**params)
    ask = render_guest_template(
        reservation,
        lambda l: _property_guest_text(reservation, "checkin_complete_ask_arrival", l),
        ctx,
    )
    return "\n".join([body, "", ask, *_arrival_reply_footer(reservation, lang)])


def render_arrival_time_saved_message(
    reservation: Reservation,
    *,
    stated_time: str,
    parsed_late: bool,
    message_text: str = "",
) -> str:
    ctx = _resolve_language(
        reservation,
        mode=LanguageMode.REACTIVE,
        message_text=message_text,
    )
    lang = ctx.language
    params = {**_arrival_window_params(reservation), "stated_time": stated_time}
    prop = reservation.property

    if parsed_late and prop.check_in_latest_time:
        if is_after_hours_not_allowed(prop):
            body = _text_for_lang(ARRIVAL_LATE_NOT_ALLOWED, lang).format(**params)
        else:
            phone = params["contact_phone"] or "recepciju"
            params["contact_phone"] = phone
            body = _text_for_lang(ARRIVAL_LATE_CONTACT, lang).format(**params)
    else:
        body = _text_for_lang(ARRIVAL_TIME_SAVED_THANKS, lang).format(**params)

    return "\n".join([body, *_arrival_reply_footer(reservation, lang)])


def _system_prompt() -> str:
    return (
        "You are a professional hotel reception assistant drafting short guest messages. "
        "Use ONLY facts from the JSON context. Never invent prices, dates, room names, or policies. "
        "Reply in the language given in the context. "
        "Match the guest's latest inbound message language when present in message_history. "
        "Keep a warm, concise reception tone. "
        "End with the property name and this footer on its own line: "
        "Managed by stay.hr — https://stay.hr/"
    )


def _user_prompt(intent: str, hint: str, context: dict) -> str:
    payload = {
        "intent": intent,
        "hint": hint,
        "context": _compose_context_for_llm(context),
    }
    instructions = {
        GuestMessageIntent.REPLY: "Write a reply to the guest using message_history; address their latest inbound message.",
        GuestMessageIntent.CUSTOM: "Write a custom message following the hint.",
    }
    task = instructions.get(intent, instructions[GuestMessageIntent.CUSTOM])
    return (
        f"Task: {task}\n"
        f"Language: {context['language']}\n"
        f"Data (JSON):\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def _generate_body(
    reservation: Reservation,
    intent: str,
    hint: str,
    language: str | None,
) -> tuple[str, bool, str]:
    mode = LanguageMode.REACTIVE if intent == GuestMessageIntent.REPLY else LanguageMode.PROACTIVE
    context = build_compose_context(reservation, language=language, mode=mode)

    if intent == GuestMessageIntent.CHECKIN:
        return _render_checkin_fallback(reservation, context), False, ""

    if intent == GuestMessageIntent.REPLY and _normalize_hint(hint) == HINT_CHECKIN_READY:
        return _render_checkin_ready_fallback(reservation, context), False, ""

    if intent == GuestMessageIntent.REPLY and _normalize_hint(hint) == HINT_EVISITOR_REGISTERED:
        return _render_evisitor_registered_fallback(reservation, context), False, ""

    if intent == GuestMessageIntent.REPLY and _normalize_hint(hint) == HINT_ID_MISSING_SIDES:
        from apps.reservations.document_intake_sides import find_missing_id_sides

        gaps = find_missing_id_sides(reservation)
        return _render_missing_id_sides_fallback(reservation, context, gaps), False, ""

    used_llm = False
    model_name = ""

    if llm_configured():
        try:
            body = complete_chat(_system_prompt(), _user_prompt(intent, hint, context))
            used_llm = True
            model_name = llm_model()
            return body, used_llm, model_name
        except GuestComposeError:
            logger.warning(
                "LLM compose failed, using fallback",
                extra={"reservation_id": reservation.pk, "intent": intent},
            )

    fallback_hint = hint.strip() or {
        GuestMessageIntent.REPLY: "Thank you for your message. We will get back to you shortly.",
        GuestMessageIntent.CUSTOM: "Thank you for your message.",
    }.get(intent, "Thank you for your message.")
    lang = context["language"]
    greeting = _text_for_lang(GREETING, lang).format(
        name=context["guest_name"] or _text_for_lang(DEFAULT_GUEST_NAME, lang)
    )
    return f"{greeting}\n\n{fallback_hint}\n\n{FOOTER}", used_llm, model_name


def create_draft_from_body_text(
    reservation: Reservation,
    body_text: str,
    *,
    api_application: ApiApplication | None = None,
) -> tuple[GuestMessageDraft, dict]:
    """Create a draft from exact text (resend / relay) without LLM."""
    text = (body_text or "").strip()
    if not text:
        raise ValueError("body_text is required")

    lang = _lang_key(reservation, None)
    ctx = _resolve_language(reservation)
    channels = channels_with_reply_default(
        reservation,
        build_message_channels(reservation, intent=GuestMessageIntent.CUSTOM),
        intent=GuestMessageIntent.CUSTOM,
    )
    draft = GuestMessageDraft.objects.create(
        tenant_id=reservation.tenant_id,
        reservation=reservation,
        intent=GuestMessageIntent.CUSTOM,
        hint="resend",
        llm_body_text=text,
        final_body_text="",
        **_draft_language_fields(ctx),
        llm_model="",
        prompt_version=prompt_version(),
        api_application=api_application,
    )
    return draft, channels


def compose_guest_message(
    reservation: Reservation,
    *,
    intent: str,
    hint: str = "",
    api_application: ApiApplication | None = None,
    language: str | None = None,
) -> tuple[GuestMessageDraft, dict, bool]:
    """
    Compose message, persist draft, return (draft, channels, llm_used).
    """
    if intent not in {c.value for c in GuestMessageIntent}:
        raise ValueError(f"Invalid intent: {intent}")

    body, llm_used, model_name = _generate_body(reservation, intent, hint, language)
    mode = LanguageMode.REACTIVE if intent == GuestMessageIntent.REPLY else LanguageMode.PROACTIVE
    ctx = _resolve_language(reservation, override=language, mode=mode)
    channels = channels_with_reply_default(
        reservation,
        build_message_channels(reservation, intent=intent),
        intent=intent,
    )

    draft = GuestMessageDraft.objects.create(
        tenant_id=reservation.tenant_id,
        reservation=reservation,
        intent=intent,
        hint=(hint or "").strip(),
        llm_body_text=body,
        final_body_text="",
        **_draft_language_fields(ctx),
        llm_model=model_name,
        prompt_version=prompt_version(),
        api_application=api_application,
    )
    return draft, channels, llm_used


def smoke_test_llm() -> dict:
    """Quick connectivity check for production shell."""
    if not llm_configured():
        return {"ok": False, "reason": "not_configured"}
    try:
        text = complete_chat(
            "Reply with exactly: OK",
            "Ping",
            timeout=15.0,
        )
        return {"ok": text.strip().upper().startswith("OK"), "sample": text[:80]}
    except GuestComposeError as exc:
        return {"ok": False, "reason": str(exc)}
