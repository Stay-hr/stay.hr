"""Public token-scoped guest invoice-details form API (AllowAny)."""

from __future__ import annotations

from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.exceptions import BillingRecipientError
from apps.billing.services.billing_recipient import (
    BillingRecipientDraft,
    RecipientRejectReason,
    RecipientSource,
    is_structurally_ready,
    ready_missing_fields,
)
from apps.billing.services.billing_recipient_service import (
    create_open_recipient,
    get_open_recipient,
    update_open_recipient,
)
from apps.communications.guest_email_quality import is_usable_invoice_email
from apps.communications.invoice_email_capture import update_invoice_email
from apps.reservations.guest_invoice_details_access import (
    evaluate_invoice_details_access,
    get_invoice_details_access_by_token,
)
from apps.reservations.guest_invoice_details_context import (
    build_guest_invoice_details_context,
    serialize_guest_invoice_details_context,
)

_CLIENT_ERROR_REASONS = frozenset(
    {
        RecipientRejectReason.EMPTY_REQUEST,
        RecipientRejectReason.INVALID_COUNTRY,
        RecipientRejectReason.SKIP_TO_APPLIED,
        RecipientRejectReason.STRUCTURALLY_INCOMPLETE,
        RecipientRejectReason.INVALID_HR_TAX_ID,
    }
)


class GuestInvoiceDetailsWriteSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=("company", "personal"))
    company_name = serializers.CharField(required=False, allow_blank=True, max_length=255)
    tax_id = serializers.CharField(required=False, allow_blank=True, max_length=64)
    tax_id_country = serializers.CharField(required=False, allow_blank=True, max_length=2)
    country = serializers.CharField(required=False, allow_blank=True, max_length=2)
    address = serializers.CharField(required=False, allow_blank=True)
    postal_code = serializers.CharField(required=False, allow_blank=True, max_length=16)
    city = serializers.CharField(required=False, allow_blank=True, max_length=128)
    email = serializers.EmailField(required=False, allow_blank=True)
    phone = serializers.CharField(required=False, allow_blank=True, max_length=64)


def _load_access_or_404(token):
    access = get_invoice_details_access_by_token(token)
    if access is None:
        from django.http import Http404

        raise Http404("Invoice details access not found.")
    return access


def _access_error_response(access_result) -> Response:
    return Response({"status": access_result.gate_status}, status=access_result.http_status)


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


def _company_fields(data: dict) -> dict:
    return {
        "company_name": data.get("company_name") or "",
        "tax_id": data.get("tax_id") or "",
        "tax_id_country": data.get("tax_id_country") or "",
        "country": data.get("country") or "",
        "address": data.get("address") or "",
        "postal_code": data.get("postal_code") or "",
        "city": data.get("city") or "",
        "email": data.get("email") or "",
        "phone": data.get("phone") or "",
    }


class GuestInvoiceDetailsView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, token):
        access = _load_access_or_404(token)
        gate = evaluate_invoice_details_access(access)
        if not gate.allowed:
            return _access_error_response(gate)
        ctx = build_guest_invoice_details_context(access, gate)
        return Response(serialize_guest_invoice_details_context(ctx))

    def patch(self, request, token):
        access = _load_access_or_404(token)
        gate = evaluate_invoice_details_access(access)
        if not gate.allowed:
            return _access_error_response(gate)
        if not gate.writable:
            return Response({"status": gate.gate_status}, status=status.HTTP_410_GONE)

        serializer = GuestInvoiceDetailsWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        reservation = access.reservation
        kind = data["kind"]

        if kind == "personal":
            if get_open_recipient(reservation) is not None:
                return Response(
                    {
                        "status": "error",
                        "reason": "company_form_in_progress",
                        "detail": "Company invoice data is already in progress.",
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            email = (data.get("email") or "").strip()
            if not is_usable_invoice_email(email):
                return Response(
                    {
                        "status": "error",
                        "reason": "email_not_usable",
                        "detail": "A usable (non-OTA) email is required.",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            update_invoice_email(reservation, email)
            gate = evaluate_invoice_details_access(access)
            ctx = build_guest_invoice_details_context(access, gate)
            ctx["kind"] = "personal"
            return Response(serialize_guest_invoice_details_context(ctx))

        fields = _company_fields(data)
        draft = BillingRecipientDraft(
            company_name=fields["company_name"],
            tax_id=fields["tax_id"],
            tax_id_country=fields["tax_id_country"],
            country=fields["country"],
            address=fields["address"],
            postal_code=fields["postal_code"],
            city=fields["city"],
            email=fields["email"],
            phone=fields["phone"],
        )
        if not is_structurally_ready(draft):
            return Response(
                {
                    "status": "error",
                    "reason": RecipientRejectReason.STRUCTURALLY_INCOMPLETE.value,
                    "detail": "All company invoice fields are required.",
                    "missing": ready_missing_fields(draft),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            existing = get_open_recipient(reservation)
            if existing is None:
                row = create_open_recipient(
                    reservation,
                    fields,
                    source=RecipientSource.GUEST_FORM.value,
                    source_ref=str(access.pk),
                )
            else:
                fields["source"] = RecipientSource.GUEST_FORM.value
                fields["source_ref"] = str(access.pk)
                row = update_open_recipient(existing, fields)
        except BillingRecipientError as exc:
            return _error_response(exc)

        if row.status != row.Status.READY:
            return Response(
                {
                    "status": "error",
                    "reason": RecipientRejectReason.STRUCTURALLY_INCOMPLETE.value,
                    "detail": "Submitted company data is not ready for fiscal issue.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        gate = evaluate_invoice_details_access(access)
        ctx = build_guest_invoice_details_context(access, gate)
        return Response(serialize_guest_invoice_details_context(ctx))
