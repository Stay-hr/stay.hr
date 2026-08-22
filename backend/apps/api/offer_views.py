"""Reception + public API for booking offer (B2B ponuda PDF)."""

from __future__ import annotations

from django.http import FileResponse, HttpResponse
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.api.permissions import DenyAdminScopes, HasReceptionAccess
from apps.api.reception_views import ReceptionReadView, ReceptionWriteView
from apps.api.views import TenantAPIView
from apps.billing.exceptions import FiscalConfigError, InvoiceBuildError
from apps.billing.models import BookingOffer, TenantFiscalSettings
from apps.billing.services.offer_issue import issue_booking_offer
from apps.billing.services.offer_pdf import render_offer_html
from apps.communications.offer_email import send_booking_offer_email
from apps.reservations.models import Reservation


def _vat_settings_for_tenant(tenant) -> TenantFiscalSettings | None:
    settings = TenantFiscalSettings.objects.filter(tenant=tenant).first()
    if settings is None or not settings.is_vat_registered:
        return None
    return settings


def _get_reservation_offer(request, pk: int) -> tuple[Reservation, BookingOffer]:
    reservation = get_object_or_404(
        Reservation.objects.for_tenant(request.tenant).select_related("booking_offer"),
        pk=pk,
    )
    offer = getattr(reservation, "booking_offer", None)
    if offer is None:
        raise BookingOffer.DoesNotExist
    return reservation, offer


class OfferSerializerMixin:
    @staticmethod
    def serialize_offer(offer: BookingOffer) -> dict:
        snapshot = offer.snapshot or {}
        pdf_url = offer.pdf_file.url if offer.pdf_file else None
        return {
            "id": offer.pk,
            "offer_number": offer.offer_number,
            "issued_at": offer.issued_at.isoformat(),
            "valid_until": offer.valid_until.isoformat() if offer.valid_until else None,
            "total": snapshot.get("total"),
            "currency": snapshot.get("currency"),
            "buyer_name": (snapshot.get("buyer") or {}).get("name"),
            "payment_reference": snapshot.get("payment_reference"),
            "pdf_url": pdf_url,
            "public_access_token": str(offer.public_access_token),
            "public_pdf_url": f"/api/v1/public/offers/{offer.public_access_token}/pdf/",
            "email_sent_at": offer.email_sent_at.isoformat() if offer.email_sent_at else None,
            "email_recipient": offer.email_recipient or None,
            "lines": snapshot.get("lines") or [],
        }


class ReservationOfferView(TenantAPIView, OfferSerializerMixin, APIView):
    permission_classes = [HasReceptionAccess, DenyAdminScopes]

    def get_permissions(self):
        if self.request.method == "POST":
            self.required_scopes = ["reception:write"]
        else:
            self.required_scopes = ["reception:read"]
        return [permission() for permission in self.permission_classes]

    def get(self, request, pk: int):
        if _vat_settings_for_tenant(request.tenant) is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        try:
            _reservation, offer = _get_reservation_offer(request, pk)
        except BookingOffer.DoesNotExist:
            return Response({"detail": "Offer not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(self.serialize_offer(offer))

    def post(self, request, pk: int):
        if _vat_settings_for_tenant(request.tenant) is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        reservation = get_object_or_404(
            Reservation.objects.for_tenant(request.tenant).select_related("booking_offer"),
            pk=pk,
        )
        existing = getattr(reservation, "booking_offer", None)
        if existing is not None:
            return Response(self.serialize_offer(existing))

        if reservation.amount is None:
            return Response(
                {"status": "error", "reason": "no_amount"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            offer = issue_booking_offer(reservation)
        except FiscalConfigError as exc:
            return Response(
                {"status": "error", "reason": "fiscal_config_incomplete", "detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except InvoiceBuildError as exc:
            return Response(
                {"status": "error", "reason": "offer_build_failed", "detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(self.serialize_offer(offer), status=status.HTTP_201_CREATED)


class ReservationOfferPdfView(ReceptionReadView, APIView):
    def get(self, request, pk: int):
        if _vat_settings_for_tenant(request.tenant) is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        try:
            _reservation, offer = _get_reservation_offer(request, pk)
        except BookingOffer.DoesNotExist:
            return Response({"detail": "Offer not found."}, status=status.HTTP_404_NOT_FOUND)
        if not offer.pdf_file:
            return Response({"detail": "PDF not available."}, status=status.HTTP_404_NOT_FOUND)
        safe = offer.offer_number.replace("/", "-")
        return FileResponse(
            offer.pdf_file.open("rb"),
            as_attachment=True,
            filename=f"ponuda-{safe}.pdf",
            content_type="application/pdf",
        )


class OfferSendEmailSerializer(serializers.Serializer):
    email = serializers.EmailField(required=False, allow_blank=True)


class ReservationOfferSendEmailView(ReceptionWriteView, APIView):
    def post(self, request, pk: int):
        if _vat_settings_for_tenant(request.tenant) is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        reservation = get_object_or_404(
            Reservation.objects.for_tenant(request.tenant).select_related("booking_offer"),
            pk=pk,
        )
        offer = getattr(reservation, "booking_offer", None)
        if offer is None:
            try:
                offer = issue_booking_offer(reservation)
            except (FiscalConfigError, InvoiceBuildError) as exc:
                return Response(
                    {"status": "error", "detail": str(exc)},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        serializer = OfferSendEmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        raw_email = (serializer.validated_data.get("email") or "").strip()
        if raw_email:
            reservation.invoice_email = raw_email
            reservation.save(update_fields=["invoice_email", "updated_at"])

        result = send_booking_offer_email(offer.pk)
        if result.get("status") == "sent":
            return Response(
                {
                    "status": "sent",
                    "recipient": result.get("recipient"),
                    "offer_id": offer.pk,
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


class PublicOfferPdfView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, public_access_token):
        offer = (
            BookingOffer.objects.select_related("tenant")
            .filter(public_access_token=public_access_token)
            .first()
        )
        if offer is None or not offer.pdf_file:
            html = render_to_string("billing/invoice_unavailable.html")
            return HttpResponse(html, status=404)
        safe = offer.offer_number.replace("/", "-")
        return FileResponse(
            offer.pdf_file.open("rb"),
            as_attachment=False,
            filename=f"ponuda-{safe}.pdf",
            content_type="application/pdf",
        )
