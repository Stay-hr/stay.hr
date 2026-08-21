"""Reception API for staff booking intake (parse + confirm)."""

from __future__ import annotations

from rest_framework import serializers, status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.api.reception_views import ReceptionWriteView
from apps.ai.provider import GuestComposeError
from apps.integrations.channel_manager.resolver import get_channel_manager
from apps.reservations.booking_intake_service import (
    BookingIntakeError,
    ConfirmPayload,
    confirm_draft,
    create_draft_from_parse,
    serialize_draft,
)
from apps.reservations.models import Reservation
from apps.tenants.models import ChannelManager


def _ensure_intake_enabled(tenant) -> None:
    manager = get_channel_manager(tenant)
    if manager not in {ChannelManager.CHANNEX, ChannelManager.NONE}:
        raise PermissionDenied("Booking intake is not enabled for this tenant.")


class BookingIntakeParseSerializer(serializers.Serializer):
    raw_text = serializers.CharField(min_length=1, max_length=20000)
    property_slug = serializers.SlugField(required=False, allow_blank=True, default="")


class BookingIntakeConfirmSerializer(serializers.Serializer):
    draft_id = serializers.IntegerField(min_value=1)
    property_slug = serializers.SlugField()
    unit_id = serializers.IntegerField()
    check_in = serializers.DateField()
    check_out = serializers.DateField()
    booker_name = serializers.CharField(max_length=255)
    booker_phone = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")
    booker_email = serializers.EmailField(required=False, allow_blank=True, default="")
    booker_address = serializers.CharField(required=False, allow_blank=True, default="")
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)
    currency = serializers.CharField(max_length=3, required=False, default="EUR")
    buyer_company_name = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default=""
    )
    buyer_oib = serializers.CharField(max_length=11, required=False, allow_blank=True, default="")
    buyer_address = serializers.CharField(required=False, allow_blank=True, default="")
    invoice_email = serializers.EmailField(required=False, allow_blank=True, default="")
    guest_first_name = serializers.CharField(
        max_length=100, required=False, allow_blank=True, default=""
    )
    guest_last_name = serializers.CharField(
        max_length=100, required=False, allow_blank=True, default=""
    )

    def validate_booker_phone(self, value: str) -> str:
        from apps.reservations.phone_validation import validate_booker_phone

        return validate_booker_phone(value)

    def validate_buyer_oib(self, value: str) -> str:
        oib = (value or "").strip()
        if not oib:
            return ""
        if not oib.isdigit() or len(oib) != 11:
            raise serializers.ValidationError("buyer_oib must be exactly 11 digits.")
        return oib

    def validate_amount(self, value):
        if value is not None and value < 0:
            raise serializers.ValidationError("amount must be >= 0.")
        return value

    def validate(self, attrs):
        if attrs["check_out"] <= attrs["check_in"]:
            raise serializers.ValidationError(
                {"check_out": "Check-out must be after check-in."}
            )
        return attrs


class BookingIntakeParseView(ReceptionWriteView, APIView):
    def post(self, request):
        _ensure_intake_enabled(request.tenant)
        serializer = BookingIntakeParseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        property_slug = (data.get("property_slug") or "").strip() or None
        try:
            draft = create_draft_from_parse(
                tenant=request.tenant,
                raw_text=data["raw_text"],
                property_slug=property_slug,
                created_by=request.user,
            )
        except GuestComposeError as exc:
            raise ValidationError({"detail": str(exc)}) from exc
        return Response(serialize_draft(draft), status=status.HTTP_201_CREATED)


class BookingIntakeConfirmView(ReceptionWriteView, APIView):
    def post(self, request):
        _ensure_intake_enabled(request.tenant)
        serializer = BookingIntakeConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        payload = ConfirmPayload(
            property_slug=data["property_slug"],
            unit_id=data["unit_id"],
            check_in=data["check_in"],
            check_out=data["check_out"],
            booker_name=data["booker_name"],
            booker_phone=data.get("booker_phone") or "",
            booker_email=data.get("booker_email") or "",
            booker_address=data.get("booker_address") or "",
            amount=data.get("amount"),
            currency=data.get("currency") or "EUR",
            buyer_company_name=data.get("buyer_company_name") or "",
            buyer_oib=data.get("buyer_oib") or "",
            buyer_address=data.get("buyer_address") or "",
            invoice_email=data.get("invoice_email") or "",
            guest_first_name=data.get("guest_first_name") or "",
            guest_last_name=data.get("guest_last_name") or "",
        )
        try:
            draft, reservation = confirm_draft(
                tenant=request.tenant,
                draft_id=data["draft_id"],
                payload=payload,
            )
        except BookingIntakeError as exc:
            if exc.code == "draft_not_found":
                raise NotFound(exc.detail) from exc
            raise ValidationError({"detail": exc.detail, "code": exc.code}) from exc

        from apps.api.reception_serializers import ReservationTimelineSerializer

        detail = Reservation.objects.filter(pk=reservation.pk).first()
        if detail is None:
            raise NotFound("Reservation not found after confirm.")
        return Response(
            {
                "draft": serialize_draft(draft),
                "reservation": ReservationTimelineSerializer(
                    detail, context={"request": request}
                ).data,
            },
            status=status.HTTP_200_OK,
        )
