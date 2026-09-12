"""Reception API for an open BillingRecipient. Does not apply or issue invoices."""

from __future__ import annotations

from rest_framework import serializers, status
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.api.permissions import DenyAdminScopes, HasReceptionAccess
from apps.api.views import TenantAPIView
from apps.billing.exceptions import BillingRecipientError
from apps.billing.models import BillingRecipient
from apps.billing.services.billing_recipient import RecipientRejectReason, RecipientSource
from apps.billing.services.billing_recipient_service import (
    create_open_recipient,
    update_open_recipient,
)
from apps.reservations.models import Reservation

FORBIDDEN_WRITE_FIELDS: frozenset[str] = frozenset(
    {"status", "identity_confidence"}
)

OPEN_STATUSES: tuple[str, ...] = (
    BillingRecipient.Status.REQUESTED,
    BillingRecipient.Status.READY,
)

_CLIENT_ERROR_REASONS: frozenset[str] = frozenset(
    {
        RecipientRejectReason.EMPTY_REQUEST,
        RecipientRejectReason.INVALID_COUNTRY,
        RecipientRejectReason.SKIP_TO_APPLIED,
        RecipientRejectReason.STRUCTURALLY_INCOMPLETE,
        RecipientRejectReason.INVALID_HR_TAX_ID,
    }
)


class OpenBillingRecipientWriteSerializer(serializers.Serializer):
    company_name = serializers.CharField(required=False, allow_blank=True, max_length=255)
    tax_id = serializers.CharField(required=False, allow_blank=True, max_length=64)
    tax_id_country = serializers.CharField(required=False, allow_blank=True, max_length=2)
    country = serializers.CharField(required=False, allow_blank=True, max_length=2)
    address = serializers.CharField(required=False, allow_blank=True)
    postal_code = serializers.CharField(required=False, allow_blank=True, max_length=16)
    city = serializers.CharField(required=False, allow_blank=True, max_length=128)
    email = serializers.EmailField(required=False, allow_blank=True)
    phone = serializers.CharField(required=False, allow_blank=True, max_length=64)
    source = serializers.ChoiceField(
        required=False,
        allow_blank=True,
        choices=[choice.value for choice in RecipientSource],
    )
    source_ref = serializers.CharField(required=False, allow_blank=True, max_length=64)
    source_excerpt = serializers.CharField(required=False, allow_blank=True)


def _dt(value) -> str | None:
    return value.isoformat() if value else None


def serialize_open_recipient(row: BillingRecipient) -> dict:
    return {
        "id": row.pk,
        "reservation_id": row.reservation_id,
        "status": row.status,
        "identity_confidence": row.identity_confidence,
        "company_name": row.company_name,
        "tax_id": row.tax_id,
        "tax_id_country": row.tax_id_country,
        "country": row.country,
        "address": row.address,
        "postal_code": row.postal_code,
        "city": row.city,
        "email": row.email,
        "phone": row.phone,
        "source": row.source,
        "source_ref": row.source_ref,
        "source_excerpt": row.source_excerpt,
        "requested_at": _dt(row.requested_at),
        "ready_at": _dt(row.ready_at),
    }


def _reservation_for_request(request, pk: int) -> Reservation:
    reservation = Reservation.objects.for_tenant(request.tenant).filter(pk=pk).first()
    if reservation is None:
        raise NotFound("Rezervacija nije pronađena.")
    return reservation


def _open_recipient_for_reservation(reservation: Reservation) -> BillingRecipient | None:
    return (
        BillingRecipient.objects.for_tenant(reservation.tenant)
        .filter(reservation=reservation, status__in=OPEN_STATUSES)
        .first()
    )


def _error_response(exc: BillingRecipientError) -> Response:
    reason = (
        exc.reason.value if isinstance(exc.reason, RecipientRejectReason) else str(exc.reason)
    )
    http_status = (
        status.HTTP_400_BAD_REQUEST
        if reason in _CLIENT_ERROR_REASONS
        else status.HTTP_409_CONFLICT
    )
    return Response(
        {"status": "error", "reason": reason, "detail": str(exc)},
        status=http_status,
    )


def _reject_forbidden_writes(data) -> Response | None:
    present = [name for name in FORBIDDEN_WRITE_FIELDS if name in data]
    if not present:
        return None
    return Response(
        {
            "status": "error",
            "reason": "field_not_writable",
            "detail": "status and identity_confidence are not writable.",
            "fields": present,
        },
        status=status.HTTP_400_BAD_REQUEST,
    )


class ReservationOpenBillingRecipientView(TenantAPIView, APIView):
    permission_classes = [HasReceptionAccess, DenyAdminScopes]

    def get_permissions(self):
        if self.request.method in {"POST", "PATCH"}:
            self.required_scopes = ["reception:write"]
        else:
            self.required_scopes = ["reception:read"]
        return [permission() for permission in self.permission_classes]

    def get(self, request, pk: int):
        reservation = _reservation_for_request(request, pk)
        row = _open_recipient_for_reservation(reservation)
        if row is None:
            return Response(
                {"detail": "Open billing recipient not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(serialize_open_recipient(row))

    def post(self, request, pk: int):
        reservation = _reservation_for_request(request, pk)
        forbidden = _reject_forbidden_writes(request.data)
        if forbidden is not None:
            return forbidden
        serializer = OpenBillingRecipientWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            row = create_open_recipient(reservation, dict(serializer.validated_data))
        except BillingRecipientError as exc:
            return _error_response(exc)
        return Response(serialize_open_recipient(row), status=status.HTTP_201_CREATED)

    def patch(self, request, pk: int):
        reservation = _reservation_for_request(request, pk)
        row = _open_recipient_for_reservation(reservation)
        if row is None:
            return Response(
                {"detail": "Open billing recipient not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        forbidden = _reject_forbidden_writes(request.data)
        if forbidden is not None:
            return forbidden
        serializer = OpenBillingRecipientWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        try:
            row = update_open_recipient(row, dict(serializer.validated_data))
        except BillingRecipientError as exc:
            return _error_response(exc)
        return Response(serialize_open_recipient(row))
