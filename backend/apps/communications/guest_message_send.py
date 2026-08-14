"""Send guest messages via email SMTP, WhatsApp API (Meta Cloud), or WhatsApp handoff."""

from __future__ import annotations

import logging
import mimetypes
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote

from django.conf import settings
from django.core.files.base import ContentFile
from django.db.models import Q
from django.core.mail import EmailMultiAlternatives
from django.utils import timezone

from apps.communications.canonical_store import create_with_canonical
from apps.communications.guest_message_timeline import last_timeline_entry
from apps.communications.guest_email import (
    _email_context,
    _guest_recipient,
    _language_for_reservation,
    _sender_for_reservation,
    _send_guest_email,
)
from apps.communications.guest_email_html import prepare_guest_email_bodies
from apps.communications.guest_language_context import LanguageMode
from apps.communications.guest_language_resolver import GuestLanguageResolver
from apps.communications.models import (
    GuestMessageChannel,
    GuestMessageDraft,
    GuestMessageIntent,
    GuestOutboundMessage,
    GuestOutboundMessageStatus,
)
from apps.integrations.channex.ari_service import get_active_channex_integration
from apps.integrations.channex.booking_service import (
    _channex_booking_lookup_codes,
    parse_channex_booking_id,
)
from apps.integrations.channex.exceptions import ChannexBookingIngestError
from apps.integrations.channex.message_service import send_image_for_reservation, send_message_for_reservation
from apps.integrations.channel_manager.resolver import get_channel_manager
from apps.integrations.models import ChannexBookingRevision, ChannexMessage, IntegrationConfig, WhatsAppMessage
from apps.integrations.whatsapp.client import (
    WhatsAppApiError,
    extract_outbound_wamid,
    send_image_message,
    send_text_message,
    upload_media,
)
from apps.integrations.whatsapp.integration_lookup import resolve_whatsapp_integration
from apps.integrations.whatsapp.phone import normalize_phone
from apps.integrations.whatsapp.whatsapp_errors import is_whatsapp_session_api_error
from apps.integrations.whatsapp.whatsapp_session import is_customer_service_window_open
from apps.reservations.models import Reservation, ReservationVersionScope
from apps.reservations.reservation_version import touch_reservation_version
from apps.tenants.models import ApiApplication, ChannelManager

logger = logging.getLogger(__name__)

MAPS_LINK = "https://maps.app.goo.gl/BN15CcMmmAapmjUs7"

# Pre-filled WhatsApp text limit (chars before URL encoding).
WA_ME_BODY_MAX_LEN = 1500
_OUTBOUND_IMAGE_BODY = "📷 Slika poslana"


def guest_phone_number(reservation: Reservation) -> str:
    return (reservation.booker_phone or "").strip()


def truncate_wa_me_body(body_text: str) -> str:
    text = (body_text or "").strip()
    if len(text) <= WA_ME_BODY_MAX_LEN:
        return text
    suffix = "…"
    return text[: WA_ME_BODY_MAX_LEN - len(suffix)].rstrip() + suffix


def build_wa_me_url(phone_digits: str, body_text: str) -> str:
    digits = normalize_phone(phone_digits)
    if not digits:
        return ""
    encoded = quote(truncate_wa_me_body(body_text), safe="")
    return f"https://wa.me/{digits}?text={encoded}"


def _booking_channel_available(reservation: Reservation) -> bool:
    if get_channel_manager(reservation.tenant) != ChannelManager.CHANNEX:
        return False
    if reservation.import_source != "channex":
        return False
    has_channex_link = bool(parse_channex_booking_id(reservation.external_id))
    if not has_channex_link:
        has_channex_link = bool(_channex_booking_lookup_codes(reservation)) or (
            ChannexBookingRevision.objects.filter(reservation=reservation).exists()
        )
    if not has_channex_link:
        return False
    return IntegrationConfig.objects.filter(
        tenant=reservation.tenant,
        provider=IntegrationConfig.Provider.CHANNEX,
        is_active=True,
    ).exists()


def _reply_channel_from_last_inbound(
    reservation: Reservation,
    *,
    email_available: bool,
    whatsapp_available: bool,
    booking_available: bool,
) -> str:
    entry = last_timeline_entry(reservation)
    if not entry or entry.get("direction") != "inbound":
        return ""
    channel = (entry.get("channel") or "").strip()
    if channel == GuestMessageChannel.EMAIL and email_available:
        return GuestMessageChannel.EMAIL
    if channel == GuestMessageChannel.WHATSAPP and whatsapp_available:
        return GuestMessageChannel.WHATSAPP
    if channel == GuestMessageChannel.BOOKING and booking_available:
        return GuestMessageChannel.BOOKING
    return ""


def last_inbound_channel_for_reply(reservation: Reservation) -> str:
    """Last inbound timeline channel when available for outbound reply."""
    channels = build_message_channels(reservation)
    return (channels.get("reply_channel") or "").strip()


def guest_whatsapp_session_open(reservation: Reservation) -> bool:
    """True when guest sent inbound WhatsApp within the 24h customer-care window."""
    return is_customer_service_window_open(
        tenant_id=reservation.tenant_id,
        reservation=reservation,
    )


def _whatsapp_api_send_enabled(whatsapp_runtime) -> bool:
    return bool(whatsapp_runtime and whatsapp_runtime.can_send_messages())


def channels_with_reply_default(
    reservation: Reservation,
    channels: dict,
    *,
    intent: str,
) -> dict:
    """For reply compose, prefer the last inbound channel as default."""
    if intent != GuestMessageIntent.REPLY:
        return channels
    reply_channel = (channels.get("reply_channel") or "").strip()
    if not reply_channel:
        return channels
    updated = dict(channels)
    updated["default_channel"] = reply_channel
    return updated


def _welcome_template_available(
    *,
    integration: IntegrationConfig,
    runtime,
    template_name: str,
    language_code: str,
) -> bool:
    from apps.integrations.whatsapp.meta_templates import MetaTemplateApiError, find_message_template

    waba_id = runtime.effective_waba_id()
    if not waba_id or not runtime.access_token:
        return False
    try:
        existing = find_message_template(
            waba_id=waba_id,
            access_token=runtime.access_token,
            name=template_name,
            language=language_code,
        )
    except MetaTemplateApiError:
        return False
    return bool(existing and str(existing.get("status") or "").upper() == "APPROVED")


def _whatsapp_template_compose_fields(
    reservation: Reservation,
    *,
    intent: str | None,
    whatsapp_api_send: bool,
    integration: IntegrationConfig | None,
    runtime,
) -> dict:
    from apps.integrations.whatsapp.welcome_template import (
        build_welcome_template_parameters,
        resolve_welcome_template,
    )

    template_name: str | None = None
    template_available = False
    if not whatsapp_api_send or integration is None or runtime is None:
        return {"template_name": template_name, "template_available": template_available}
    if intent not in (
        GuestMessageIntent.CHECKIN,
        GuestMessageIntent.WELCOME_TEMPLATE,
        None,
    ):
        return {"template_name": template_name, "template_available": template_available}

    config = integration.get_config_dict()
    lang, _params = build_welcome_template_parameters(reservation)
    resolved = resolve_welcome_template(language=lang, platform_config=config)
    computed_name = resolved.template_name
    if intent in (GuestMessageIntent.CHECKIN, GuestMessageIntent.WELCOME_TEMPLATE):
        template_name = computed_name
        template_available = _welcome_template_available(
            integration=integration,
            runtime=runtime,
            template_name=computed_name,
            language_code=resolved.meta_language,
        )
    return {"template_name": template_name, "template_available": template_available}


def build_message_channels(reservation: Reservation, *, intent: str | None = None) -> dict:
    email = _guest_recipient(reservation)
    phone_raw = guest_phone_number(reservation)
    phone_wa = normalize_phone(phone_raw)
    booking_available = _booking_channel_available(reservation)
    whatsapp_integration, whatsapp_runtime = resolve_whatsapp_integration(reservation.tenant)
    whatsapp_api_send = _whatsapp_api_send_enabled(whatsapp_runtime)

    email_available = bool(email)
    whatsapp_available = bool(phone_wa)
    session_open = (
        is_customer_service_window_open(
            tenant_id=reservation.tenant_id,
            reservation=reservation,
        )
        if whatsapp_available
        else False
    )

    if email_available:
        default_channel = GuestMessageChannel.EMAIL
    elif whatsapp_available:
        default_channel = GuestMessageChannel.WHATSAPP
    elif booking_available:
        default_channel = GuestMessageChannel.BOOKING
    else:
        default_channel = ""

    reply_channel = _reply_channel_from_last_inbound(
        reservation,
        email_available=email_available,
        whatsapp_available=whatsapp_available,
        booking_available=booking_available,
    )

    template_fields = _whatsapp_template_compose_fields(
        reservation,
        intent=intent,
        whatsapp_api_send=whatsapp_api_send,
        integration=whatsapp_integration if whatsapp_api_send else None,
        runtime=whatsapp_runtime if whatsapp_api_send else None,
    )

    return {
        "email": {
            "available": email_available,
            "to": email or "",
        },
        "whatsapp": {
            "available": whatsapp_available,
            "phone_raw": phone_raw,
            "phone_wa": phone_wa,
            "wa_me_url": build_wa_me_url(phone_wa, "") if phone_wa else "",
            "api_send": whatsapp_api_send,
            "session_open": session_open,
            **template_fields,
        },
        "booking": {
            "available": booking_available,
        },
        "default_channel": default_channel,
        "reply_channel": reply_channel,
    }


def default_email_subject(reservation: Reservation) -> str:
    code = (reservation.booking_code or reservation.external_id or "").strip()
    lang = _language_for_reservation(reservation)
    if code:
        if lang == "hr":
            return f"Poruka o rezervaciji {code}"
        return f"Message about booking {code}"
    if lang == "hr":
        return f"Poruka o rezervaciji #{reservation.pk}"
    return f"Message about reservation #{reservation.pk}"


def send_guest_text_email(
    reservation: Reservation,
    body_text: str,
    *,
    subject: str | None = None,
    body_html: str | None = None,
    attachment: tuple[str, bytes, str] | None = None,
) -> dict:
    """Send guest email via SMTP (no timeline record).

    Prefer :func:`send_guest_email_with_timeline_record` so the message appears in
    reception / Hospira guest threads.

    attachment: optional (filename, bytes, mime_type).
    """
    recipient = _guest_recipient(reservation)
    if not recipient:
        return {"sent": False, "reason": "no_recipient"}

    text = (body_text or "").strip()
    if not text and attachment is None:
        return {"sent": False, "reason": "empty_body"}

    subj = (subject or default_email_subject(reservation)).strip()
    from_header, reply_to = _sender_for_reservation(reservation)
    message = EmailMultiAlternatives(
        subject=subj,
        body=text or _OUTBOUND_IMAGE_BODY,
        from_email=from_header,
        to=[recipient],
        reply_to=[reply_to] if reply_to else None,
    )
    html = (body_html or "").strip()
    if html:
        message.attach_alternative(html, "text/html")
    if attachment is not None:
        filename, file_bytes, mime_type = attachment
        message.attach(filename, file_bytes, mime_type)
    try:
        if not _send_guest_email(message, reservation):
            return {"sent": False, "reason": "smtp_not_configured"}
    except Exception as exc:
        logger.exception(
            "guest text email failed",
            extra={"reservation_id": reservation.pk},
        )
        return {"sent": False, "reason": "send_failed", "error": str(exc)}

    logger.info(
        "guest text email sent",
        extra={"reservation_id": reservation.pk, "to": recipient},
    )
    return {"sent": True, "to": recipient}


def send_guest_email_with_timeline_record(
    reservation: Reservation,
    body_text: str,
    *,
    subject: str | None = None,
    body_html: str | None = None,
    draft: GuestMessageDraft | None = None,
    api_application: ApiApplication | None = None,
    intent: str = GuestMessageIntent.CUSTOM,
    hint: str = "",
) -> GuestOutboundMessage:
    """Send guest SMTP email and record GuestOutboundMessage for unified timeline."""
    text, html_part = prepare_guest_email_bodies(body_text, body_html=body_html)
    recipient = _guest_recipient(reservation) or ""

    if draft is None:
        ctx = GuestLanguageResolver.resolve(reservation, mode=LanguageMode.PROACTIVE)
        draft = GuestMessageDraft.objects.create(
            tenant_id=reservation.tenant_id,
            reservation=reservation,
            intent=intent,
            hint=hint,
            language=ctx.language,
            language_source=ctx.source.value,
            language_reason=(ctx.reason or "")[:255],
            llm_body_text=(body_text or "").strip(),
            final_body_text="",
            channel=GuestMessageChannel.EMAIL,
            api_application=api_application,
        )

    outbound = create_with_canonical(GuestOutboundMessage,
        tenant_id=reservation.tenant_id,
        reservation=reservation,
        draft=draft,
        channel=GuestMessageChannel.EMAIL,
        body_text=text,
        status=GuestOutboundMessageStatus.QUEUED,
        to_email=recipient,
        api_application=api_application,
    )

    result = send_guest_text_email(
        reservation,
        text,
        subject=subject,
        body_html=html_part,
    )
    if result.get("sent"):
        outbound.status = GuestOutboundMessageStatus.SENT
        outbound.error_message = ""
    else:
        outbound.status = GuestOutboundMessageStatus.FAILED
        outbound.error_message = result.get("reason") or result.get("error") or "send_failed"

    outbound.save(update_fields=["status", "error_message"])

    draft.final_body_text = text
    draft.channel = GuestMessageChannel.EMAIL
    update_fields = ["final_body_text", "channel"]
    if outbound.status == GuestOutboundMessageStatus.SENT:
        draft.sent_at = timezone.now()
        update_fields.append("sent_at")
        touch_reservation_version(
            reservation.pk,
            ReservationVersionScope.MESSAGES,
            reason="email_outbound",
        )
    draft.save(update_fields=update_fields)

    return outbound


def send_guest_message(
    *,
    reservation: Reservation,
    draft: GuestMessageDraft,
    channel: str,
    body_text: str,
    api_application: ApiApplication | None,
    subject: str | None = None,
    existing_outbound: GuestOutboundMessage | None = None,
    request_id: str = "",
) -> GuestOutboundMessage | ChannexMessage:
    """Send via email, WhatsApp handoff, or Channex Booking.com messages."""
    text = (body_text or "").strip()
    if not text:
        raise ValueError("body_text is required")

    if channel == GuestMessageChannel.EMAIL:
        return _send_email_channel(
            reservation=reservation,
            draft=draft,
            body_text=text,
            api_application=api_application,
            subject=subject,
        )
    if channel == GuestMessageChannel.WHATSAPP:
        return _send_whatsapp_channel(
            reservation=reservation,
            draft=draft,
            body_text=text,
            api_application=api_application,
            existing_outbound=existing_outbound,
            request_id=request_id,
        )
    if channel == GuestMessageChannel.BOOKING:
        return _send_booking_channel(
            reservation=reservation,
            draft=draft,
            body_text=text,
        )
    raise ValueError(f"Unsupported channel: {channel}")


def _whatsapp_image_api_available(runtime) -> bool:
    return bool(runtime and runtime.can_send_media())


def _send_email_channel(
    *,
    reservation: Reservation,
    draft: GuestMessageDraft,
    body_text: str,
    api_application: ApiApplication | None,
    subject: str | None,
) -> GuestOutboundMessage:
    return send_guest_email_with_timeline_record(
        reservation,
        body_text,
        subject=subject,
        draft=draft,
        api_application=api_application,
    )


def _send_whatsapp_channel(
    *,
    reservation: Reservation,
    draft: GuestMessageDraft,
    body_text: str,
    api_application: ApiApplication | None,
    existing_outbound: GuestOutboundMessage | None = None,
    request_id: str = "",
) -> GuestOutboundMessage:
    from apps.communications.guest_message_whatsapp_v2 import send_whatsapp_channel_v2

    return send_whatsapp_channel_v2(
        reservation=reservation,
        draft=draft,
        body_text=body_text,
        api_application=api_application,
        existing_outbound=existing_outbound,
        request_id=request_id,
    )


def send_guest_whatsapp_image(
    *,
    reservation: Reservation,
    draft: GuestMessageDraft,
    uploaded_file,
    caption: str = "",
    api_application: ApiApplication | None,
) -> WhatsAppMessage:
    """Send image via WhatsApp API; returns WhatsAppMessage row."""
    integration, runtime = resolve_whatsapp_integration(reservation.tenant)
    if integration is None or runtime is None:
        raise ValueError("whatsapp_not_configured")
    if not _whatsapp_image_api_available(runtime):
        raise ValueError("whatsapp_api_send_unavailable")

    phone_raw = guest_phone_number(reservation)
    phone_wa = normalize_phone(phone_raw)
    if not phone_wa:
        raise ValueError("no_phone")
    if not guest_whatsapp_session_open(reservation):
        raise ValueError("whatsapp_session_closed")

    file_bytes = uploaded_file.read()
    uploaded_file.seek(0)
    if not file_bytes:
        raise ValueError("empty_file")

    mime_type = getattr(uploaded_file, "content_type", None) or mimetypes.guess_type(
        getattr(uploaded_file, "name", "") or ""
    )[0] or "image/jpeg"
    if not mime_type.startswith("image/"):
        raise ValueError("unsupported_media_type")

    filename = getattr(uploaded_file, "name", None) or "image.jpg"

    outbound = create_with_canonical(GuestOutboundMessage,
        tenant_id=reservation.tenant_id,
        reservation=reservation,
        draft=draft,
        channel=GuestMessageChannel.WHATSAPP,
        body_text=(caption or "").strip() or _OUTBOUND_IMAGE_BODY,
        status=GuestOutboundMessageStatus.QUEUED,
        to_phone=phone_raw,
        api_application=api_application,
    )

    try:
        media_id = upload_media(
            file_bytes=file_bytes,
            mime_type=mime_type,
            filename=filename,
            phone_number_id=runtime.phone_number_id,
            access_token=runtime.access_token,
        )
        response = send_image_message(
            phone_number_id=runtime.phone_number_id,
            access_token=runtime.access_token,
            to_wa_id=phone_wa,
            media_id=media_id,
            caption=caption,
        )
    except WhatsAppApiError as exc:
        outbound.status = GuestOutboundMessageStatus.FAILED
        outbound.error_message = str(exc)
        outbound.save(update_fields=["status", "error_message"])
        raise ValueError(str(exc)) from exc

    wamid = extract_outbound_wamid(response) or f"local.outbound.image.{outbound.pk}"
    wa_message = create_with_canonical(WhatsAppMessage,
        tenant_id=reservation.tenant_id,
        integration=integration,
        reservation=reservation,
        wamid=wamid,
        wa_id=phone_wa,
        phone_number_id=runtime.phone_number_id,
        direction=WhatsAppMessage.Direction.OUTBOUND,
        message_type="image",
        body=(caption or "").strip(),
        raw_payload=response,
    )
    wa_message.media_file.save(filename, ContentFile(file_bytes), save=True)

    now = timezone.now()
    outbound.status = GuestOutboundMessageStatus.SENT
    outbound.error_message = ""
    outbound.save(update_fields=["status", "error_message"])

    draft.final_body_text = (caption or "").strip() or _OUTBOUND_IMAGE_BODY
    draft.channel = GuestMessageChannel.WHATSAPP
    draft.sent_at = now
    draft.save(update_fields=["final_body_text", "channel", "sent_at"])

    touch_reservation_version(
        reservation.pk,
        ReservationVersionScope.MESSAGES,
        reason="whatsapp_outbound",
    )
    return wa_message


def uzorita_entrance_image_path() -> Path:
    """Legacy fallback when Property.guest_info has no entrance_image."""
    return Path(settings.BASE_DIR) / "assets" / "whatsapp" / "uzorita_entrance.jpg"


def send_whatsapp_entrance_image_from_asset(
    *,
    reservation: Reservation,
    draft: GuestMessageDraft,
    caption: str = "",
    api_application: ApiApplication | None,
) -> WhatsAppMessage:
    """Send property entrance photo from guest_info asset (WhatsApp auto-reply after check-in)."""
    from apps.properties.guest_info import property_entrance_image_path

    path = property_entrance_image_path(reservation.property)
    if not path.is_file():
        raise ValueError("entrance_image_missing")

    integration, runtime = resolve_whatsapp_integration(reservation.tenant)
    if integration is None or runtime is None:
        raise ValueError("whatsapp_not_configured")
    if not _whatsapp_image_api_available(runtime):
        raise ValueError("whatsapp_api_send_unavailable")

    phone_raw = guest_phone_number(reservation)
    phone_wa = normalize_phone(phone_raw)
    if not phone_wa:
        raise ValueError("no_phone")

    file_bytes = path.read_bytes()
    mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    filename = path.name

    outbound = create_with_canonical(GuestOutboundMessage,
        tenant_id=reservation.tenant_id,
        reservation=reservation,
        draft=draft,
        channel=GuestMessageChannel.WHATSAPP,
        body_text=(caption or "").strip() or _OUTBOUND_IMAGE_BODY,
        status=GuestOutboundMessageStatus.QUEUED,
        to_phone=phone_raw,
        api_application=api_application,
    )

    try:
        media_id = upload_media(
            file_bytes=file_bytes,
            mime_type=mime_type,
            filename=filename,
            phone_number_id=runtime.phone_number_id,
            access_token=runtime.access_token,
        )
        response = send_image_message(
            phone_number_id=runtime.phone_number_id,
            access_token=runtime.access_token,
            to_wa_id=phone_wa,
            media_id=media_id,
            caption=caption,
        )
    except WhatsAppApiError as exc:
        outbound.status = GuestOutboundMessageStatus.FAILED
        outbound.error_message = str(exc)
        outbound.save(update_fields=["status", "error_message"])
        raise ValueError(str(exc)) from exc

    wamid = extract_outbound_wamid(response) or f"local.outbound.image.{outbound.pk}"
    wa_message = create_with_canonical(WhatsAppMessage,
        tenant_id=reservation.tenant_id,
        integration=integration,
        reservation=reservation,
        wamid=wamid,
        wa_id=phone_wa,
        phone_number_id=runtime.phone_number_id,
        direction=WhatsAppMessage.Direction.OUTBOUND,
        message_type="image",
        body=(caption or "").strip(),
        raw_payload=response,
    )
    wa_message.media_file.save(filename, ContentFile(file_bytes), save=True)

    now = timezone.now()
    outbound.status = GuestOutboundMessageStatus.SENT
    outbound.error_message = ""
    outbound.save(update_fields=["status", "error_message"])

    draft.final_body_text = (caption or "").strip() or _OUTBOUND_IMAGE_BODY
    draft.channel = GuestMessageChannel.WHATSAPP
    draft.sent_at = now
    draft.save(update_fields=["final_body_text", "channel", "sent_at"])

    touch_reservation_version(
        reservation.pk,
        ReservationVersionScope.MESSAGES,
        reason="whatsapp_outbound",
    )
    return wa_message


def send_guest_email_image(
    *,
    reservation: Reservation,
    draft: GuestMessageDraft,
    uploaded_file,
    caption: str = "",
    api_application: ApiApplication | None,
    subject: str | None = None,
) -> GuestOutboundMessage:
    """Send image attachment to guest via tenant SMTP."""
    recipient = _guest_recipient(reservation) or ""
    if not recipient:
        raise ValueError("no_recipient")

    file_bytes = uploaded_file.read()
    uploaded_file.seek(0)
    if not file_bytes:
        raise ValueError("empty_file")

    mime_type = getattr(uploaded_file, "content_type", None) or mimetypes.guess_type(
        getattr(uploaded_file, "name", "") or ""
    )[0] or "image/jpeg"
    if not mime_type.startswith("image/"):
        raise ValueError("unsupported_media_type")

    filename = getattr(uploaded_file, "name", None) or "image.jpg"
    raw_body = (caption or "").strip() or _OUTBOUND_IMAGE_BODY
    body_text, html_part = prepare_guest_email_bodies(raw_body)

    outbound = create_with_canonical(GuestOutboundMessage,
        tenant_id=reservation.tenant_id,
        reservation=reservation,
        draft=draft,
        channel=GuestMessageChannel.EMAIL,
        body_text=body_text,
        status=GuestOutboundMessageStatus.QUEUED,
        to_email=recipient,
        api_application=api_application,
    )

    result = send_guest_text_email(
        reservation,
        body_text,
        subject=subject,
        body_html=html_part,
        attachment=(filename, file_bytes, mime_type),
    )
    if result.get("sent"):
        outbound.status = GuestOutboundMessageStatus.SENT
        outbound.error_message = ""
        outbound.media_file.save(filename, ContentFile(file_bytes), save=True)
    else:
        outbound.status = GuestOutboundMessageStatus.FAILED
        outbound.error_message = result.get("reason") or result.get("error") or "send_failed"
        outbound.save(update_fields=["status", "error_message"])
        raise ValueError(outbound.error_message)

    draft.final_body_text = body_text
    draft.channel = GuestMessageChannel.EMAIL
    draft.sent_at = timezone.now()
    draft.save(update_fields=["final_body_text", "channel", "sent_at"])

    return outbound


def send_guest_channex_image(
    *,
    reservation: Reservation,
    draft: GuestMessageDraft,
    uploaded_file,
    caption: str = "",
) -> ChannexMessage:
    """Send image via Channex Booking.com / Airbnb / Expedia messages API."""
    if not _booking_channel_available(reservation):
        raise ValueError("booking_channel_unavailable")

    integration = get_active_channex_integration(reservation.tenant.slug)
    if integration is None:
        raise ValueError("channex_not_configured")

    file_bytes = uploaded_file.read()
    uploaded_file.seek(0)
    if not file_bytes:
        raise ValueError("empty_file")

    mime_type = getattr(uploaded_file, "content_type", None) or mimetypes.guess_type(
        getattr(uploaded_file, "name", "") or ""
    )[0] or "image/jpeg"
    if not mime_type.startswith("image/"):
        raise ValueError("unsupported_media_type")

    filename = getattr(uploaded_file, "name", None) or "image.jpg"
    body_text = (caption or "").strip() or _OUTBOUND_IMAGE_BODY

    try:
        row = send_image_for_reservation(
            integration,
            reservation,
            file_bytes=file_bytes,
            filename=filename,
            mime_type=mime_type,
            caption=(caption or "").strip(),
        )
    except ChannexBookingIngestError as exc:
        raise ValueError(str(exc)) from exc

    now = timezone.now()
    draft.final_body_text = body_text
    draft.channel = GuestMessageChannel.BOOKING
    draft.sent_at = now
    draft.save(update_fields=["final_body_text", "channel", "sent_at"])

    return row


def _send_whatsapp_handoff(
    *,
    reservation: Reservation,
    draft: GuestMessageDraft,
    body_text: str,
    api_application: ApiApplication | None,
    handoff_reason: str = "",
) -> GuestOutboundMessage:
    phone_raw = guest_phone_number(reservation)
    phone_wa = normalize_phone(phone_raw)
    if not phone_wa:
        raise ValueError("no_phone")

    wa_me_url = build_wa_me_url(phone_wa, body_text)
    outbound = create_with_canonical(GuestOutboundMessage,
        tenant_id=reservation.tenant_id,
        reservation=reservation,
        draft=draft,
        channel=GuestMessageChannel.WHATSAPP,
        body_text=body_text,
        status=GuestOutboundMessageStatus.HANDOFF_WHATSAPP,
        to_phone=phone_raw,
        wa_me_url=wa_me_url,
        api_application=api_application,
    )

    now = timezone.now()
    draft.final_body_text = body_text
    draft.channel = GuestMessageChannel.WHATSAPP
    draft.sent_at = now
    draft.save(update_fields=["final_body_text", "channel", "sent_at"])

    if handoff_reason:
        outbound.handoff_reason = handoff_reason  # type: ignore[attr-defined]

    touch_reservation_version(
        reservation.pk,
        ReservationVersionScope.MESSAGES,
        reason="whatsapp_handoff",
    )
    return outbound


def _send_booking_channel(
    *,
    reservation: Reservation,
    draft: GuestMessageDraft,
    body_text: str,
) -> ChannexMessage:
    if not _booking_channel_available(reservation):
        raise ValueError("booking_channel_unavailable")

    integration = get_active_channex_integration(reservation.tenant.slug)
    try:
        row = send_message_for_reservation(integration, reservation, body_text)
    except ChannexBookingIngestError as exc:
        raise ValueError(str(exc)) from exc

    now = timezone.now()
    draft.final_body_text = body_text
    draft.channel = GuestMessageChannel.BOOKING
    draft.sent_at = now
    draft.save(update_fields=["final_body_text", "channel", "sent_at"])

    return row
