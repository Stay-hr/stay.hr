"""Admin-only invoice replacement API (ADR 0022).

Authorization is HasScope admin:read / admin:write. reception:* is insufficient.
Lifecycle, link, and audit fields are read-only. Delivery is a separate
post-commit command and never runs inside storno/complete.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.api.authentication import AppKeyAuthentication
from apps.api.permissions import HasApiApplication, HasScope
from apps.billing.exceptions import (
    FiscalConfigError,
    InvoiceBuildError,
    InvoiceGraphError,
    InvoiceReplacementError,
)
from apps.billing.models import Invoice, InvoiceReplacement
from apps.billing.services.billing_recipient import RecipientSource
from apps.billing.services.invoice_replacement import ReplacementRejectReason
from apps.billing.services.invoice_replacement_delivery import (
    send_replacement_invoice_email,
    send_replacement_storno_email,
)
from apps.billing.services.invoice_replacement_issue import (
    complete_replacement,
    issue_replacement_storno,
)
from apps.billing.services.invoice_replacement_service import (
    EDITABLE_RECIPIENT_FIELDS,
    cancel_replacement_case,
    open_replacement_case,
    update_replacement_recipient,
    verify_replacement_recipient,
)

_CLIENT_ERROR_REASONS: frozenset[str] = frozenset(
    {
        ReplacementRejectReason.CASE_REASON_REQUIRED.value,
        ReplacementRejectReason.CANCEL_REASON_REQUIRED.value,
        ReplacementRejectReason.ISSUER_OIB_EVIDENCE_REQUIRED.value,
        ReplacementRejectReason.ISSUER_OIB_INVALID.value,
        ReplacementRejectReason.INVALID_COUNTRY.value,
        ReplacementRejectReason.INVALID_RECIPIENT.value,
    }
)

FORBIDDEN_WRITE_FIELDS: frozenset[str] = frozenset(
    {
        "status",
        "identity_confidence",
        "storno_invoice_id",
        "replacement_invoice_id",
        "opened_by",
        "opened_at",
        "completed_by",
        "completed_at",
        "cancelled_by",
        "cancelled_at",
        "verified_by",
        "verified_at",
        "original_issuer_oib_recorded_by",
        "original_issuer_oib_recorded_at",
    }
)


class AdminAPIView(APIView):
    authentication_classes = [AppKeyAuthentication]
    permission_classes = [HasApiApplication, HasScope]


class AdminReadView(AdminAPIView):
    required_scopes = ["admin:read"]


class AdminWriteView(AdminAPIView):
    required_scopes = ["admin:write"]


class OpenReplacementCaseSerializer(serializers.Serializer):
    original_invoice_id = serializers.IntegerField()
    reason = serializers.CharField()
    original_issuer_oib = serializers.CharField(required=False, allow_blank=True, default="")
    original_issuer_oib_source = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
    )


class ReplacementRecipientWriteSerializer(serializers.Serializer):
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


class CancelReplacementSerializer(serializers.Serializer):
    cancel_reason = serializers.CharField()


def _dt(value) -> str | None:
    return value.isoformat() if value else None


def _username(user) -> str | None:
    if user is None:
        return None
    return user.username


def _invoice_ref(invoice: Invoice | None) -> dict | None:
    if invoice is None:
        return None
    return {
        "id": invoice.pk,
        "invoice_number": invoice.invoice_number,
        "total": str(invoice.total),
        "fiscal_status": invoice.fiscal_status,
        "email_recipient": invoice.email_recipient or "",
        "email_sent_at": _dt(invoice.email_sent_at),
    }


def serialize_replacement_case(case: InvoiceReplacement) -> dict:
    try:
        recipient = case.recipient
    except ObjectDoesNotExist:
        recipient = None
    return {
        "id": case.pk,
        "reservation_id": case.reservation_id,
        "status": case.status,
        "reason": case.reason,
        "original_invoice": _invoice_ref(case.original_invoice),
        "storno_invoice": _invoice_ref(case.storno_invoice),
        "replacement_invoice": _invoice_ref(case.replacement_invoice),
        "opened_by": _username(case.opened_by),
        "opened_at": _dt(case.opened_at),
        "completed_by": _username(case.completed_by),
        "completed_at": _dt(case.completed_at),
        "cancelled_by": _username(case.cancelled_by),
        "cancelled_at": _dt(case.cancelled_at),
        "cancel_reason": case.cancel_reason,
        "original_issuer_oib": case.original_issuer_oib,
        "original_issuer_oib_source": case.original_issuer_oib_source,
        "recipient": None
        if recipient is None
        else {
            "company_name": recipient.company_name,
            "tax_id": recipient.tax_id,
            "tax_id_country": recipient.tax_id_country,
            "country": recipient.country,
            "address": recipient.address,
            "postal_code": recipient.postal_code,
            "city": recipient.city,
            "email": recipient.email,
            "phone": recipient.phone,
            "identity_confidence": recipient.identity_confidence,
            "source": recipient.source,
            "source_ref": recipient.source_ref,
            "source_excerpt": recipient.source_excerpt,
            "verified_by": _username(recipient.verified_by),
            "verified_at": _dt(recipient.verified_at),
        },
    }


def _case_queryset(tenant):
    return (
        InvoiceReplacement.objects.for_tenant(tenant)
        .select_related(
            "original_invoice",
            "storno_invoice",
            "replacement_invoice",
            "opened_by",
            "completed_by",
            "cancelled_by",
            "recipient",
            "recipient__verified_by",
        )
        .order_by("-opened_at", "-id")
    )


def _case_for_request(request, case_id: int) -> InvoiceReplacement:
    case = _case_queryset(request.tenant).filter(pk=case_id).first()
    if case is None:
        raise NotFound("Replacement case not found.")
    return case


def _admin_actor(request):
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False) and user.is_staff:
        return user
    tenant = request.tenant
    username = f"admin-api.{tenant.slug}"
    actor, _created = get_user_model().objects.get_or_create(
        username=username,
        defaults={"is_staff": True, "is_active": True},
    )
    if not actor.is_staff or not actor.is_active:
        actor.is_staff = True
        actor.is_active = True
        actor.save(update_fields=["is_staff", "is_active"])
    return actor


def _reject_forbidden_writes(data) -> Response | None:
    present = [name for name in FORBIDDEN_WRITE_FIELDS if name in data]
    if not present:
        return None
    return Response(
        {
            "status": "error",
            "reason": "field_not_writable",
            "detail": "Lifecycle, link, and audit fields are read-only.",
            "fields": present,
        },
        status=status.HTTP_400_BAD_REQUEST,
    )


def _error_response(exc: InvoiceReplacementError) -> Response:
    reason = (
        exc.reason.value
        if isinstance(exc.reason, ReplacementRejectReason)
        else str(exc.reason)
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


def _command_error_response(exc: Exception) -> Response:
    if isinstance(exc, InvoiceReplacementError):
        return _error_response(exc)
    if isinstance(exc, InvoiceGraphError):
        return Response(
            {
                "status": "error",
                "reason": "invoice_graph_error",
                "detail": str(exc) or "invoice_graph_error",
            },
            status=status.HTTP_409_CONFLICT,
        )
    if isinstance(exc, FiscalConfigError):
        return Response(
            {
                "status": "error",
                "reason": "fiscal_config_incomplete",
                "detail": str(exc),
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    if isinstance(exc, InvoiceBuildError):
        return Response(
            {
                "status": "error",
                "reason": "invoice_build_failed",
                "detail": str(exc),
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    raise exc


def _delivery_response(result: dict) -> Response:
    if result.get("status") == "sent":
        return Response(
            {
                "status": "sent",
                "recipient": result.get("recipient"),
                "invoice_id": result.get("invoice_id"),
            }
        )
    if result.get("reason") == "no_smtp":
        return Response(
            {"status": "skipped", "reason": "no_smtp"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return Response(
        {"status": "skipped", "reason": result.get("reason", "no_recipient")},
        status=status.HTTP_400_BAD_REQUEST,
    )


class InvoiceReplacementListCreateView(AdminAPIView):
    def get_permissions(self):
        if self.request.method == "POST":
            self.required_scopes = ["admin:write"]
        else:
            self.required_scopes = ["admin:read"]
        return [permission() for permission in self.permission_classes]

    def get(self, request):
        queryset = _case_queryset(request.tenant)
        reservation_id = request.query_params.get("reservation_id")
        if reservation_id:
            queryset = queryset.filter(reservation_id=reservation_id)
        return Response([serialize_replacement_case(case) for case in queryset])

    def post(self, request):
        forbidden = _reject_forbidden_writes(request.data)
        if forbidden is not None:
            return forbidden
        serializer = OpenReplacementCaseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        original = (
            Invoice.objects.for_tenant(request.tenant)
            .filter(pk=data["original_invoice_id"])
            .first()
        )
        if original is None:
            raise NotFound("Invoice not found.")
        try:
            case = open_replacement_case(
                original=original,
                actor=_admin_actor(request),
                reason=data["reason"],
                original_issuer_oib=data.get("original_issuer_oib") or "",
                original_issuer_oib_source=data.get("original_issuer_oib_source") or "",
            )
        except (InvoiceReplacementError, InvoiceGraphError) as exc:
            return _command_error_response(exc)
        return Response(
            serialize_replacement_case(_case_for_request(request, case.pk)),
            status=status.HTTP_201_CREATED,
        )


class InvoiceReplacementDetailView(AdminReadView):
    def get(self, request, case_id: int):
        return Response(serialize_replacement_case(_case_for_request(request, case_id)))


class InvoiceReplacementRecipientView(AdminWriteView):
    def patch(self, request, case_id: int):
        case = _case_for_request(request, case_id)
        forbidden = _reject_forbidden_writes(request.data)
        if forbidden is not None:
            return forbidden
        serializer = ReplacementRecipientWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        fields = {
            name: value
            for name, value in serializer.validated_data.items()
            if name in EDITABLE_RECIPIENT_FIELDS
        }
        try:
            update_replacement_recipient(case=case, fields=fields)
        except InvoiceReplacementError as exc:
            return _error_response(exc)
        return Response(serialize_replacement_case(_case_for_request(request, case_id)))


class InvoiceReplacementVerifyView(AdminWriteView):
    def post(self, request, case_id: int):
        case = _case_for_request(request, case_id)
        try:
            verify_replacement_recipient(case=case, actor=_admin_actor(request))
        except InvoiceReplacementError as exc:
            return _error_response(exc)
        return Response(serialize_replacement_case(_case_for_request(request, case_id)))


class InvoiceReplacementCancelView(AdminWriteView):
    def post(self, request, case_id: int):
        case = _case_for_request(request, case_id)
        forbidden = _reject_forbidden_writes(request.data)
        if forbidden is not None:
            return forbidden
        serializer = CancelReplacementSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            cancel_replacement_case(
                case=case,
                actor=_admin_actor(request),
                cancel_reason=serializer.validated_data["cancel_reason"],
            )
        except InvoiceReplacementError as exc:
            return _error_response(exc)
        return Response(serialize_replacement_case(_case_for_request(request, case_id)))


class InvoiceReplacementStornoView(AdminWriteView):
    def post(self, request, case_id: int):
        case = _case_for_request(request, case_id)
        try:
            issue_replacement_storno(case=case)
        except (InvoiceReplacementError, FiscalConfigError, InvoiceBuildError) as exc:
            return _command_error_response(exc)
        return Response(serialize_replacement_case(_case_for_request(request, case_id)))


class InvoiceReplacementCompleteView(AdminWriteView):
    def post(self, request, case_id: int):
        case = _case_for_request(request, case_id)
        try:
            complete_replacement(case=case, actor=_admin_actor(request))
        except (InvoiceReplacementError, FiscalConfigError, InvoiceBuildError) as exc:
            return _command_error_response(exc)
        return Response(serialize_replacement_case(_case_for_request(request, case_id)))


class InvoiceReplacementSendStornoView(AdminWriteView):
    def post(self, request, case_id: int):
        case = _case_for_request(request, case_id)
        try:
            result = send_replacement_storno_email(case=case)
        except InvoiceReplacementError as exc:
            return _error_response(exc)
        return _delivery_response(result)


class InvoiceReplacementSendReplacementView(AdminWriteView):
    def post(self, request, case_id: int):
        case = _case_for_request(request, case_id)
        try:
            result = send_replacement_invoice_email(case=case)
        except InvoiceReplacementError as exc:
            return _error_response(exc)
        return _delivery_response(result)
