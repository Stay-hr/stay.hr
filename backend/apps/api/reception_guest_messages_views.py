"""Reception guest messaging API (compose, send, timeline)."""

from __future__ import annotations

import time

from django.db import transaction
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.api.language import resolve_request_language
from apps.api.reception_views import ReceptionReadView, ReceptionWriteView, _validate_photo_file
from apps.communications.guest_compose import (
    compose_guest_message,
    create_draft_from_body_text,
    build_compose_response_fields,
)
from apps.communications.guest_message_logging import (
    GUEST_MESSAGE_COMPOSE_ATTEMPT,
    GUEST_MESSAGE_COMPOSE_SUCCESS,
    GUEST_MESSAGE_SEND_ATTEMPT,
    GUEST_MESSAGE_SEND_ERROR,
    GUEST_MESSAGE_SEND_SUCCESS,
    GuestMessageCorrelationMixin,
    body_meta,
    log_guest_message_event,
)
from apps.communications.guest_message_translate import (
    GuestMessageTranslateError,
    translate_guest_message,
)
from apps.communications.guest_message_whatsapp_v2 import WhatsAppSendPendingError
from apps.communications.guest_message_send import (
    build_message_channels,
    default_email_subject,
    send_guest_channex_image,
    send_guest_email_image,
    send_guest_message,
    send_guest_whatsapp_image,
)
from apps.communications.guest_message_timeline import (
    serialize_channex,
    serialize_outbound,
    serialize_whatsapp,
    timeline_for_reservation,
)
from apps.communications.models import (
    GuestMessageChannel,
    GuestMessageDraft,
    GuestMessageIntent,
    GuestMessageThreadState,
    GuestOutboundMessage,
    GuestOutboundMessageStatus,
)
from apps.integrations.channex.ari_service import get_active_channex_integration
from apps.integrations.channex.client import ChannexClient
from apps.integrations.channex.config import ChannexRuntimeConfig
from apps.integrations.channex.message_service import first_attachment_path
from apps.integrations.models import ChannexMessage, WhatsAppMessage
from apps.reservations.models import Reservation


class GuestMessageComposeSerializer(serializers.Serializer):
    intent = serializers.ChoiceField(choices=GuestMessageIntent.choices, required=False)
    hint = serializers.CharField(required=False, allow_blank=True, default="")
    language = serializers.CharField(required=False, allow_blank=True, default="")
    body_text = serializers.CharField(
        required=False,
        allow_blank=False,
        max_length=8000,
        trim_whitespace=True,
    )

    def validate(self, attrs):
        body_text = (attrs.get("body_text") or "").strip()
        intent = attrs.get("intent")
        if body_text:
            attrs["body_text"] = body_text
            return attrs
        if intent is None:
            raise serializers.ValidationError(
                {"intent": "Required unless body_text is provided."}
            )
        return attrs


class GuestMessageSendSerializer(serializers.Serializer):
    draft_id = serializers.IntegerField()
    channel = serializers.ChoiceField(choices=GuestMessageChannel.choices)
    body_text = serializers.CharField(max_length=8000, trim_whitespace=True)
    subject = serializers.CharField(required=False, allow_blank=True, default="", max_length=200)


class GuestMessageTranslateSerializer(serializers.Serializer):
    timeline_id = serializers.IntegerField(min_value=1)
    lang = serializers.CharField(required=False, allow_blank=True, default="")


class GuestMessageSendImageSerializer(serializers.Serializer):
    draft_id = serializers.IntegerField(required=False)
    caption = serializers.CharField(required=False, allow_blank=True, default="", max_length=1024)
    channel = serializers.ChoiceField(
        choices=GuestMessageChannel.choices,
        required=False,
        default=GuestMessageChannel.WHATSAPP,
    )
    file = serializers.FileField()

    def validate_file(self, value):
        return _validate_photo_file(value)


def _reservation_or_404(tenant, reservation_id: int) -> Reservation:
    reservation = (
        Reservation.objects.filter(tenant=tenant, pk=reservation_id)
        .select_related("property", "tenant")
        .prefetch_related("guests")
        .first()
    )
    if reservation is None:
        raise NotFound("Reservation not found.")
    return reservation


class ReceptionGuestMessagesView(ReceptionReadView, APIView):
    def get(self, request, reservation_id: int):
        reservation = _reservation_or_404(request.tenant, reservation_id)
        # ADR 0019 Phase A: GET is DB-only. ``sync`` is ignored (Flutter may still send it).
        return Response(timeline_for_reservation(reservation))


class ReceptionGuestMessageChannelsView(ReceptionReadView, APIView):
    def get(self, request, reservation_id: int):
        reservation = _reservation_or_404(request.tenant, reservation_id)
        return Response(build_message_channels(reservation))


class ReceptionGuestMessageComposeView(GuestMessageCorrelationMixin, ReceptionWriteView, APIView):
    def post(self, request, reservation_id: int):
        reservation = _reservation_or_404(request.tenant, reservation_id)
        request_id = request.guest_message_request_id
        started = time.perf_counter()
        serializer = GuestMessageComposeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        intent = data.get("intent") or GuestMessageIntent.CUSTOM
        log_guest_message_event(
            GUEST_MESSAGE_COMPOSE_ATTEMPT,
            request_id=request_id,
            reservation_id=reservation.pk,
            tenant_slug=request.tenant.slug,
            intent=intent,
        )

        language = (data.get("language") or "").strip() or None
        api_application = getattr(request, "api_application", None)

        body_text = (data.get("body_text") or "").strip()
        if body_text:
            draft, channels = create_draft_from_body_text(
                reservation,
                body_text,
                api_application=api_application,
            )
            llm_used = False
        else:
            draft, channels, llm_used = compose_guest_message(
                reservation,
                intent=data["intent"],
                hint=data.get("hint") or "",
                api_application=api_application,
                language=language,
            )

        preview = build_compose_response_fields(
            reservation,
            body_text=draft.llm_body_text,
            guest_language=draft.language,
        )

        compose_ms = int((time.perf_counter() - started) * 1000)
        log_guest_message_event(
            GUEST_MESSAGE_COMPOSE_SUCCESS,
            request_id=request_id,
            reservation_id=reservation.pk,
            tenant_slug=request.tenant.slug,
            draft_id=draft.pk,
            intent=draft.intent,
            compose_ms=compose_ms,
            llm_used=llm_used,
        )

        return Response(
            {
                "draft_id": draft.pk,
                "body_text": draft.llm_body_text,
                "language": draft.language,
                "language_source": draft.language_source or "",
                "language_reason": draft.language_reason or "",
                "body_text_tenant": preview["body_text_tenant"],
                "tenant_language": preview["tenant_language"],
                "llm_used": llm_used,
                "channels": channels,
            },
            status=status.HTTP_201_CREATED,
        )


class ReceptionGuestMessageSendView(GuestMessageCorrelationMixin, ReceptionWriteView, APIView):
    def post(self, request, reservation_id: int):
        reservation = _reservation_or_404(request.tenant, reservation_id)
        request_id = request.guest_message_request_id
        started = time.perf_counter()
        serializer = GuestMessageSendSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        channel = data["channel"]
        body_text = data["body_text"]
        channels = build_message_channels(reservation)

        log_guest_message_event(
            GUEST_MESSAGE_SEND_ATTEMPT,
            request_id=request_id,
            reservation_id=reservation.pk,
            tenant_slug=request.tenant.slug,
            draft_id=data["draft_id"],
            channel=channel,
            **body_meta(body_text),
        )

        if channel == GuestMessageChannel.EMAIL and not channels["email"]["available"]:
            self._log_send_error(
                request_id=request_id,
                reservation=reservation,
                draft_id=data["draft_id"],
                channel=channel,
                body_text=body_text,
                started=started,
                detail="No guest email on this reservation.",
            )
            raise ValidationError({"channel": "No guest email on this reservation."})
        if channel == GuestMessageChannel.WHATSAPP and not channels["whatsapp"]["available"]:
            self._log_send_error(
                request_id=request_id,
                reservation=reservation,
                draft_id=data["draft_id"],
                channel=channel,
                body_text=body_text,
                started=started,
                detail="No guest phone on this reservation.",
            )
            raise ValidationError({"channel": "No guest phone on this reservation."})
        if channel == GuestMessageChannel.BOOKING and not channels["booking"]["available"]:
            self._log_send_error(
                request_id=request_id,
                reservation=reservation,
                draft_id=data["draft_id"],
                channel=channel,
                body_text=body_text,
                started=started,
                detail="Booking.com messaging is not available for this reservation.",
            )
            raise ValidationError({"channel": "Booking.com messaging is not available for this reservation."})

        api_application = getattr(request, "api_application", None)
        subject = (data.get("subject") or "").strip() or None

        with transaction.atomic():
            draft = (
                GuestMessageDraft.objects.select_for_update()
                .filter(
                    tenant=request.tenant,
                    reservation=reservation,
                    pk=data["draft_id"],
                )
                .first()
            )
            if draft is None:
                self._log_send_error(
                    request_id=request_id,
                    reservation=reservation,
                    draft_id=data["draft_id"],
                    channel=channel,
                    body_text=body_text,
                    started=started,
                    detail="Draft not found for this reservation.",
                )
                raise ValidationError({"draft_id": "Draft not found for this reservation."})

            if draft.sent_at is not None:
                existing = (
                    draft.outbound_messages.filter(channel=channel)
                    .order_by("-pk")
                    .first()
                )
                if existing is not None:
                    return self._send_response(
                        existing,
                        draft,
                        channel,
                        api_application,
                        status.HTTP_200_OK,
                        request_id=request_id,
                        started=started,
                    )

            existing_outbound = draft.outbound_messages.filter(
                channel=channel,
                status=GuestOutboundMessageStatus.PENDING_SEND,
            ).first()

            try:
                result = send_guest_message(
                    reservation=reservation,
                    draft=draft,
                    channel=channel,
                    body_text=body_text,
                    api_application=api_application,
                    subject=subject or default_email_subject(reservation),
                    existing_outbound=existing_outbound,
                    request_id=request_id,
                )
            except WhatsAppSendPendingError as exc:
                return self._send_response(
                    exc.outbound,
                    draft,
                    channel,
                    api_application,
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    request_id=request_id,
                    started=started,
                )
            except ValueError as exc:
                self._log_send_error(
                    request_id=request_id,
                    reservation=reservation,
                    draft_id=draft.pk,
                    channel=channel,
                    body_text=body_text,
                    started=started,
                    detail=str(exc),
                    intent=draft.intent,
                )
                raise ValidationError({"channel": str(exc)}) from exc

        if isinstance(result, ChannexMessage):
            app = getattr(request, "api_application", None)
            payload = serialize_channex(result)
            payload["status"] = "sent"
            payload["sent_by_name"] = app.name if app else None
            payload["edited"] = draft.edited
            send_ms = int((time.perf_counter() - started) * 1000)
            log_guest_message_event(
                GUEST_MESSAGE_SEND_SUCCESS,
                request_id=request_id,
                reservation_id=reservation.pk,
                tenant_slug=request.tenant.slug,
                draft_id=draft.pk,
                channel=channel,
                intent=draft.intent,
                status="sent",
                send_ms=send_ms,
            )
            return Response(payload, status=status.HTTP_201_CREATED)

        return self._send_response(
            result,
            draft,
            channel,
            api_application,
            status.HTTP_201_CREATED,
            request_id=request_id,
            started=started,
        )

    def _log_send_error(
        self,
        *,
        request_id: str,
        reservation: Reservation,
        draft_id: int,
        channel: str,
        body_text: str,
        started: float,
        detail: str,
        intent: str = "",
    ) -> None:
        send_ms = int((time.perf_counter() - started) * 1000)
        log_guest_message_event(
            GUEST_MESSAGE_SEND_ERROR,
            request_id=request_id,
            reservation_id=reservation.pk,
            tenant_slug=reservation.tenant.slug,
            draft_id=draft_id,
            channel=channel,
            intent=intent,
            send_ms=send_ms,
            detail=detail[:200],
            **body_meta(body_text),
        )

    def _send_response(
        self,
        outbound,
        draft,
        channel,
        api_application,
        http_status,
        *,
        request_id: str = "",
        started: float | None = None,
    ):
        payload = serialize_outbound(outbound)
        if channel == GuestMessageChannel.WHATSAPP:
            payload["wa_me_url"] = outbound.wa_me_url
            handoff_reason = getattr(outbound, "handoff_reason", None)
            if handoff_reason:
                payload["handoff_reason"] = handoff_reason
        payload["edited"] = draft.edited
        if api_application is not None:
            payload["sent_by_name"] = api_application.name
        if request_id and started is not None:
            send_ms = int((time.perf_counter() - started) * 1000)
            tenant_slug = ""
            if getattr(outbound, "reservation", None) is not None:
                tenant_slug = outbound.reservation.tenant.slug
            log_guest_message_event(
                GUEST_MESSAGE_SEND_SUCCESS,
                request_id=request_id,
                reservation_id=draft.reservation_id,
                tenant_slug=tenant_slug,
                draft_id=draft.pk,
                channel=channel,
                intent=draft.intent,
                status=payload.get("status") or outbound.status,
                handoff_reason=payload.get("handoff_reason") or getattr(outbound, "handoff_reason", "") or "",
                send_ms=send_ms,
            )
        return Response(payload, status=http_status)


class ReceptionGuestMessageSendImageView(ReceptionWriteView, APIView):
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, reservation_id: int):
        reservation = _reservation_or_404(request.tenant, reservation_id)
        serializer = GuestMessageSendImageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        channels = build_message_channels(reservation)
        channel = data.get("channel") or GuestMessageChannel.WHATSAPP

        if channel == GuestMessageChannel.EMAIL:
            if not channels["email"]["available"]:
                raise ValidationError({"channel": "No guest email on this reservation."})
        elif channel == GuestMessageChannel.WHATSAPP:
            if not channels["whatsapp"]["available"]:
                raise ValidationError({"channel": "No guest phone on this reservation."})
            if not channels["whatsapp"].get("api_send"):
                raise ValidationError({"channel": "WhatsApp API send is not available for this tenant."})
        elif channel == GuestMessageChannel.BOOKING:
            if not channels["booking"]["available"]:
                raise ValidationError({"channel": "Booking.com messaging is not available for this reservation."})
        else:
            raise ValidationError({"channel": f"Unsupported channel: {channel}"})

        draft_id = data.get("draft_id")
        draft = None
        if draft_id is not None:
            draft = GuestMessageDraft.objects.filter(
                tenant=request.tenant,
                reservation=reservation,
                pk=draft_id,
            ).first()
            if draft is None:
                raise ValidationError({"draft_id": "Draft not found for this reservation."})
        else:
            caption = (data.get("caption") or "").strip()
            draft, _ = create_draft_from_body_text(
                reservation,
                caption or "📷 Slika poslana",
                api_application=getattr(request, "api_application", None),
            )

        api_application = getattr(request, "api_application", None)
        try:
            if channel == GuestMessageChannel.EMAIL:
                outbound = send_guest_email_image(
                    reservation=reservation,
                    draft=draft,
                    uploaded_file=data["file"],
                    caption=data.get("caption") or "",
                    api_application=api_application,
                )
            elif channel == GuestMessageChannel.BOOKING:
                channex_message = send_guest_channex_image(
                    reservation=reservation,
                    draft=draft,
                    uploaded_file=data["file"],
                    caption=data.get("caption") or "",
                )
            else:
                wa_message = send_guest_whatsapp_image(
                    reservation=reservation,
                    draft=draft,
                    uploaded_file=data["file"],
                    caption=data.get("caption") or "",
                    api_application=api_application,
                )
        except ValueError as exc:
            raise ValidationError({"file": str(exc)}) from exc

        if channel == GuestMessageChannel.EMAIL:
            payload = serialize_outbound(outbound)
            if api_application is not None:
                payload["sent_by_name"] = api_application.name
            return Response(payload, status=status.HTTP_201_CREATED)

        if channel == GuestMessageChannel.BOOKING:
            payload = serialize_channex(channex_message)
            payload["status"] = "sent"
            if api_application is not None:
                payload["sent_by_name"] = api_application.name
            return Response(payload, status=status.HTTP_201_CREATED)

        payload = serialize_whatsapp(wa_message)
        if api_application is not None:
            payload["sent_by_name"] = api_application.name
        return Response(payload, status=status.HTTP_201_CREATED)


class WhatsAppMessageMediaView(ReceptionReadView, APIView):
    def get(self, request, message_id: int):
        import mimetypes

        from django.http import FileResponse

        row = WhatsAppMessage.objects.filter(tenant=request.tenant, pk=message_id).first()
        if row is None or not row.media_file:
            raise NotFound("Media nije dostupna.")

        content_type, _ = mimetypes.guess_type(row.media_file.name)
        return FileResponse(
            row.media_file.open("rb"),
            content_type=content_type or "image/jpeg",
        )


class GuestOutboundMessageMediaView(ReceptionReadView, APIView):
    def get(self, request, message_id: int):
        import mimetypes

        from django.http import FileResponse

        row = GuestOutboundMessage.objects.filter(tenant=request.tenant, pk=message_id).first()
        if row is None or not row.media_file:
            raise NotFound("Media nije dostupna.")

        content_type, _ = mimetypes.guess_type(row.media_file.name)
        return FileResponse(
            row.media_file.open("rb"),
            content_type=content_type or "image/jpeg",
        )


class ChannexMessageMediaView(ReceptionReadView, APIView):
    def get(self, request, message_id: int):
        import mimetypes

        from django.http import FileResponse, HttpResponse

        row = (
            ChannexMessage.objects.filter(tenant=request.tenant, pk=message_id)
            .select_related("integration")
            .first()
        )
        if row is None:
            raise NotFound("Media nije dostupna.")

        if row.media_file:
            content_type, _ = mimetypes.guess_type(row.media_file.name)
            return FileResponse(
                row.media_file.open("rb"),
                content_type=content_type or "image/jpeg",
            )

        attachment_path = first_attachment_path(row.raw_payload or {})
        if not attachment_path:
            raise NotFound("Media nije dostupna.")

        integration = row.integration or get_active_channex_integration(request.tenant.slug)
        if integration is None:
            raise NotFound("Channex integracija nije dostupna.")

        config = ChannexRuntimeConfig.from_integration_dict(integration.get_config_dict())
        with ChannexClient(config) as client:
            file_bytes, content_type = client.fetch_attachment_bytes(attachment_path)

        guessed, _ = mimetypes.guess_type(attachment_path)
        return HttpResponse(
            file_bytes,
            content_type=guessed or content_type or "image/jpeg",
        )


class ReceptionGuestMessageDismissReplyView(ReceptionWriteView, APIView):
    def post(self, request, reservation_id: int):
        reservation = _reservation_or_404(request.tenant, reservation_id)
        now = timezone.now()
        state, _ = GuestMessageThreadState.objects.get_or_create(
            tenant=request.tenant,
            reservation=reservation,
        )
        state.reply_dismissed_at = now
        state.save(update_fields=["reply_dismissed_at"])
        return Response({"reply_dismissed_at": state.reply_dismissed_at.isoformat()})


class ReceptionGuestMessageTranslateView(ReceptionWriteView, APIView):
    def post(self, request, reservation_id: int):
        reservation = _reservation_or_404(request.tenant, reservation_id)
        serializer = GuestMessageTranslateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        lang = (data.get("lang") or "").strip()
        if not lang:
            lang = resolve_request_language(request, tenant=request.tenant)

        try:
            payload = translate_guest_message(
                reservation=reservation,
                timeline_id=data["timeline_id"],
                target_lang=lang,
            )
        except GuestMessageTranslateError as exc:
            raise ValidationError({"timeline_id": str(exc)}) from exc

        return Response(payload)
