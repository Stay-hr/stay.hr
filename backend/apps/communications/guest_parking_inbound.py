"""Central guest parking inbound detection and auto-reply (all channels)."""

from __future__ import annotations

import logging

from django.utils import timezone

from apps.ai.provider import GuestComposeError, llm_configured
from apps.communications.guest_compose import FOOTER, GREETING, HINT_PARKING_AUTO_REPLY, SIGN_OFF
from apps.communications.guest_language_context import LanguageMode
from apps.communications.guest_language_resolver import GuestLanguageResolver
from apps.communications.guest_message_send import send_guest_message
from apps.communications.guest_parking_llm import (
    analyze_and_compose_parking_reply,
    build_parking_auto_reply,
    is_parking_only_message,
    parking_llm_audit_fields,
)
from apps.communications.guest_parking_patterns import classify_parking_only
from apps.communications.models import GuestMessageChannel, GuestMessageDraft, GuestMessageIntent
from apps.core.timezone import property_local_now
from apps.reservations.models import Reservation

logger = logging.getLogger(__name__)

_CHANNEL_MAP = {
    "whatsapp": GuestMessageChannel.WHATSAPP,
    "email": GuestMessageChannel.EMAIL,
    "booking": GuestMessageChannel.BOOKING,
}


def _text_for_lang(texts: dict[str, str], lang: str) -> str:
    base = (lang or "en").split("-")[0].lower()
    if base in texts and texts[base]:
        return texts[base]
    return texts.get("en") or next(iter(texts.values()), "")


def _parking_reply_sent_today(reservation: Reservation) -> bool:
    now = property_local_now(reservation.property)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return GuestMessageDraft.objects.filter(
        reservation=reservation,
        hint=HINT_PARKING_AUTO_REPLY,
        sent_at__gte=start_of_day,
    ).exists()


def _format_parking_reply_with_greeting(
    reservation: Reservation,
    reply_body: str,
    language: str,
) -> str:
    raw_name = (reservation.booker_name or "").strip()
    first_name = raw_name.split()[0] if raw_name else _text_for_lang(
        {"hr": "gost", "en": "guest"}, language
    )
    greeting = _text_for_lang(GREETING, language).format(name=first_name)
    sign_off = _text_for_lang(SIGN_OFF, language)
    property_name = reservation.property.name
    return "\n".join([greeting, "", reply_body, "", sign_off, property_name, "", FOOTER])


def send_parking_auto_reply(
    reservation: Reservation,
    *,
    channel: str,
    body: str,
    reply_body: str,
    language: str | None = None,
    used_llm: bool = False,
) -> dict:
    ctx = GuestLanguageResolver.resolve(
        reservation,
        mode=LanguageMode.REACTIVE,
        reply_language=language,
        message_text=body,
    )
    lang = ctx.language
    channel_enum = _CHANNEL_MAP.get(channel, GuestMessageChannel.EMAIL)
    full_body = _format_parking_reply_with_greeting(
        reservation,
        reply_body,
        lang,
    )

    draft = GuestMessageDraft.objects.create(
        tenant_id=reservation.tenant_id,
        reservation=reservation,
        intent=GuestMessageIntent.REPLY,
        hint=HINT_PARKING_AUTO_REPLY,
        llm_body_text=reply_body,
        final_body_text=full_body,
        language=lang,
        language_source=ctx.source.value,
        language_reason=(ctx.reason or "")[:255],
        channel=channel_enum,
        **(parking_llm_audit_fields() if used_llm else {}),
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
            "parking auto-reply send failed reservation_id=%s channel=%s: %s",
            reservation.pk,
            channel,
            exc,
        )
        return {"status": "send_failed", "detail": str(exc)}

    draft.sent_at = timezone.now()
    draft.save(update_fields=["sent_at"])
    return {"status": "sent", "channel": channel, "used_llm": used_llm}


def _handle_parking_fallback(
    reservation: Reservation,
    body: str,
    *,
    channel: str,
) -> dict | None:
    if not classify_parking_only(body):
        return None

    if _parking_reply_sent_today(reservation):
        return {"status": "guest_parking_handled", "reply": {"status": "dedup_skipped"}}

    ctx = GuestLanguageResolver.resolve(
        reservation,
        mode=LanguageMode.REACTIVE,
        message_text=body,
    )
    lang = ctx.language
    reply_body = build_parking_auto_reply(reservation, body, language=lang)
    if not reply_body:
        return None

    reply_result = send_parking_auto_reply(
        reservation,
        channel=channel,
        body=body,
        reply_body=reply_body,
        language=lang,
        used_llm=False,
    )
    return {"status": "guest_parking_handled", "reply": reply_result, "used_llm": False}


def _handle_parking_llm(
    reservation: Reservation,
    body: str,
    *,
    channel: str,
    llm_result,
) -> dict:
    if not llm_result.is_parking_related:
        return None

    if not is_parking_only_message(body):
        return None

    if _parking_reply_sent_today(reservation):
        return {"status": "guest_parking_handled", "reply": {"status": "dedup_skipped"}}

    # Single source of truth for the reply language. The LLM claim is compared
    # against it unresolved, so a claim we did not accept cannot slip its text
    # through in the wrong language.
    final_language = GuestLanguageResolver.resolve(
        reservation,
        mode=LanguageMode.REACTIVE,
        reply_language=llm_result.reported_reply_language or None,
        message_text=body,
    ).language

    if llm_result.reply_text and llm_result.reported_reply_language == final_language:
        reply_body = llm_result.reply_text
    else:
        logger.info(
            "parking LLM language rejected reservation_id=%s reported=%s final=%s",
            reservation.pk,
            llm_result.reported_reply_language or "-",
            final_language,
        )
        reply_body = build_parking_auto_reply(reservation, body, language=final_language)

    if not reply_body:
        return None

    reply_result = send_parking_auto_reply(
        reservation,
        channel=channel,
        body=body,
        reply_body=reply_body,
        language=final_language,
        used_llm=True,
    )
    return {"status": "guest_parking_handled", "reply": reply_result, "used_llm": True}


def maybe_handle_guest_parking_inbound(
    reservation: Reservation,
    body: str,
    *,
    channel: str,
) -> dict | None:
    """Auto-reply to parking-only guest messages on the inbound channel."""
    if reservation.status != Reservation.Status.EXPECTED:
        return None

    if not reservation.property.guest_parking_auto_reply_enabled:
        return None

    if not classify_parking_only(body):
        return None

    if llm_configured():
        try:
            llm_result = analyze_and_compose_parking_reply(
                reservation,
                body,
                channel=channel,
            )
            if not llm_result.is_parking_related:
                return _handle_parking_fallback(reservation, body, channel=channel)
            return _handle_parking_llm(
                reservation,
                body,
                channel=channel,
                llm_result=llm_result,
            )
        except GuestComposeError as exc:
            logger.warning(
                "parking LLM failed, using regex fallback reservation_id=%s: %s",
                reservation.pk,
                exc,
            )

    return _handle_parking_fallback(reservation, body, channel=channel)
