from __future__ import annotations

from rest_framework import serializers, status
from rest_framework.exceptions import APIException, NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.api.language import parse_translate_flag, resolve_request_language
from apps.api.permissions import DenyAdminScopes, HasReceptionAccess
from apps.api.reception_channex_views import _guard_channex_tenant
from apps.api.views import TenantAPIView
from apps.integrations.channex.ari_service import get_active_channex_integration
from apps.integrations.channex.exceptions import (
    ChannexApiError,
    ChannexBookingIngestError,
    ChannexWriteDisabled,
)
from apps.integrations.channex.review_service import (
    DEFAULT_PAGE_SIZE,
    compose_review_reply,
    get_review_for_tenant,
    list_reviews_for_property,
    list_reviews_for_reservation,
    reply_to_review,
    reservation_should_auto_sync_reviews,
    serialize_channex_review,
    submit_airbnb_guest_review,
)
from apps.reservations.models import Reservation


class ChannexWriteUnavailable(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "Channex writes are disabled on this host."
    default_code = "channex_write_disabled"


def _serialize_review(request, row, *, translate_default: bool) -> dict:
    lang = resolve_request_language(request, tenant=request.tenant)
    translate = parse_translate_flag(
        request.query_params.get("translate"),
        default=translate_default,
    )
    return serialize_channex_review(row, lang=lang, translate=translate)


class ChannexReviewReplySerializer(serializers.Serializer):
    reply = serializers.CharField(max_length=4000, trim_whitespace=True)


class ChannexReviewComposeReplySerializer(serializers.Serializer):
    hint = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    language = serializers.CharField(max_length=8, required=False, allow_blank=True)


class ChannexGuestReviewScoreSerializer(serializers.Serializer):
    category = serializers.CharField(max_length=64)
    rating = serializers.IntegerField(min_value=1, max_value=5)


class ChannexGuestReviewSerializer(serializers.Serializer):
    scores = ChannexGuestReviewScoreSerializer(many=True)
    public_review = serializers.CharField(max_length=4000, required=False, allow_blank=True)
    private_review = serializers.CharField(max_length=4000, required=False, allow_blank=True)
    is_reviewee_recommended = serializers.BooleanField(default=True)
    tags = serializers.ListField(
        child=serializers.CharField(max_length=128),
        required=False,
        allow_empty=True,
    )


class ReceptionReviewsListView(TenantAPIView, APIView):
    permission_classes = [HasReceptionAccess, DenyAdminScopes]
    required_scopes = ["reception:read"]

    def get(self, request):
        _guard_channex_tenant(request.tenant)
        try:
            integration = get_active_channex_integration(request.tenant.slug)
        except ChannexBookingIngestError as exc:
            raise ValidationError(str(exc)) from exc

        sync_param = request.query_params.get("sync", "auto")
        page = max(int(request.query_params.get("page", 1) or 1), 1)
        page_size = min(
            max(int(request.query_params.get("page_size", DEFAULT_PAGE_SIZE) or DEFAULT_PAGE_SIZE), 1),
            100,
        )
        unreplied_only = request.query_params.get("unreplied", "") in ("1", "true", "yes")
        ota = request.query_params.get("ota", "")

        try:
            rows, total = list_reviews_for_property(
                integration,
                unreplied_only=unreplied_only,
                ota=ota,
                page=page,
                page_size=page_size,
                sync_if_empty=sync_param == "auto",
                sync_if_stale=sync_param == "auto",
                force_sync=sync_param == "1",
            )
        except ChannexBookingIngestError as exc:
            raise ValidationError(str(exc)) from exc
        except ChannexApiError as exc:
            raise ValidationError(str(exc)) from exc

        return Response(
            {
                "page": page,
                "page_size": page_size,
                "total": total,
                "reviews": [
                    _serialize_review(request, row, translate_default=True) for row in rows
                ],
            }
        )


class ReceptionReviewDetailView(TenantAPIView, APIView):
    permission_classes = [HasReceptionAccess, DenyAdminScopes]
    required_scopes = ["reception:read"]

    def get(self, request, review_id: int):
        _guard_channex_tenant(request.tenant)
        row = get_review_for_tenant(request.tenant, review_id)
        if row is None:
            raise NotFound("Review not found.")
        return Response(_serialize_review(request, row, translate_default=True))


class ReceptionReviewComposeReplyView(TenantAPIView, APIView):
    permission_classes = [HasReceptionAccess, DenyAdminScopes]
    required_scopes = ["reception:write"]

    def post(self, request, review_id: int):
        _guard_channex_tenant(request.tenant)
        row = get_review_for_tenant(request.tenant, review_id)
        if row is None:
            raise NotFound("Review not found.")

        serializer = ChannexReviewComposeReplySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            body_text, llm_used, language = compose_review_reply(
                row,
                hint=data.get("hint") or "",
                language=(data.get("language") or "").strip() or None,
            )
        except ChannexBookingIngestError as exc:
            raise ValidationError(str(exc)) from exc

        return Response(
            {
                "body_text": body_text,
                "language": language,
                "llm_used": llm_used,
            },
            status=status.HTTP_201_CREATED,
        )


class ReceptionReviewReplyView(TenantAPIView, APIView):
    permission_classes = [HasReceptionAccess, DenyAdminScopes]
    required_scopes = ["reception:write"]

    def post(self, request, review_id: int):
        _guard_channex_tenant(request.tenant)
        row = get_review_for_tenant(request.tenant, review_id)
        if row is None:
            raise NotFound("Review not found.")

        serializer = ChannexReviewReplySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            integration = get_active_channex_integration(request.tenant.slug)
            row = reply_to_review(
                integration,
                row,
                serializer.validated_data["reply"],
            )
        except ChannexBookingIngestError as exc:
            raise ValidationError({"reply": [str(exc)]}) from exc
        except ChannexApiError as exc:
            raise ValidationError({"reply": [str(exc)]}) from exc
        except ChannexWriteDisabled as exc:
            raise ChannexWriteUnavailable(detail=str(exc)) from exc

        return Response(_serialize_review(request, row, translate_default=True))


class ReceptionReviewGuestReviewView(TenantAPIView, APIView):
    permission_classes = [HasReceptionAccess, DenyAdminScopes]
    required_scopes = ["reception:write"]

    def post(self, request, review_id: int):
        _guard_channex_tenant(request.tenant)
        row = get_review_for_tenant(request.tenant, review_id)
        if row is None:
            raise NotFound("Review not found.")

        serializer = ChannexGuestReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        channex_scores = [
            {"category": item["category"], "rating": item["rating"]}
            for item in data["scores"]
        ]

        try:
            integration = get_active_channex_integration(request.tenant.slug)
            row = submit_airbnb_guest_review(
                integration,
                row,
                scores=channex_scores,
                public_review=data.get("public_review", ""),
                private_review=data.get("private_review", ""),
                is_reviewee_recommended=data.get("is_reviewee_recommended", True),
                tags=data.get("tags"),
            )
        except ChannexBookingIngestError as exc:
            raise ValidationError(str(exc)) from exc
        except ChannexApiError as exc:
            raise ValidationError({"reply": [str(exc)]}) from exc
        except ChannexWriteDisabled as exc:
            raise ChannexWriteUnavailable(detail=str(exc)) from exc

        return Response(_serialize_review(request, row, translate_default=True))


class ReceptionReservationReviewsView(TenantAPIView, APIView):
    permission_classes = [HasReceptionAccess, DenyAdminScopes]
    required_scopes = ["reception:read"]

    def _reservation(self, request, reservation_id: int) -> Reservation:
        reservation = (
            Reservation.objects.filter(tenant=request.tenant, pk=reservation_id)
            .select_related("property")
            .first()
        )
        if reservation is None:
            raise NotFound("Reservation not found.")
        return reservation

    def get(self, request, reservation_id: int):
        _guard_channex_tenant(request.tenant)
        reservation = self._reservation(request, reservation_id)
        try:
            integration = get_active_channex_integration(request.tenant.slug)
        except ChannexBookingIngestError as exc:
            raise ValidationError(str(exc)) from exc

        sync_param = request.query_params.get("sync")
        if sync_param is None:
            sync_param = "auto" if reservation_should_auto_sync_reviews(reservation) else "0"
        try:
            rows = list_reviews_for_reservation(
                integration,
                reservation,
                sync_if_empty=sync_param == "auto",
                sync_if_stale=sync_param == "auto",
                force_sync=sync_param in ("1", "true", "yes"),
            )
        except ChannexBookingIngestError as exc:
            raise ValidationError(str(exc)) from exc
        except ChannexApiError as exc:
            raise ValidationError(str(exc)) from exc

        return Response(
            {
                "reservation_id": reservation.pk,
                "reviews": [
                    _serialize_review(request, row, translate_default=True) for row in rows
                ],
            }
        )
