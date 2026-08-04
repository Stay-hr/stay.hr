from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
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
from apps.billing.models import Invoice, TenantFiscalSettings
from apps.billing.services.pdf import render_invoice_html, split_rendered_invoice_html
from apps.communications.invoice_email import send_invoice_email
from apps.reservations.models import Guest, Reservation


def _get_reservation_invoice(request, pk: int) -> tuple[Reservation, Invoice]:
    reservation = get_object_or_404(
        Reservation.objects.for_tenant(request.tenant).select_related("invoice"),
        pk=pk,
    )
    invoice = getattr(reservation, "invoice", None)
    if invoice is None:
        raise Invoice.DoesNotExist
    return reservation, invoice


def _vat_settings_for_tenant(tenant) -> TenantFiscalSettings | None:
    settings = TenantFiscalSettings.objects.filter(tenant=tenant).first()
    if settings is None or not settings.is_vat_registered:
        return None
    return settings


class InvoiceSendEmailSerializer(serializers.Serializer):
    email = serializers.EmailField(required=False, allow_blank=True)


class InvoiceSerializerMixin:
    @staticmethod
    def serialize_invoice(invoice: Invoice) -> dict:
        pdf_url = invoice.pdf_file.url if invoice.pdf_file else None
        return {
            "id": invoice.pk,
            "invoice_number": invoice.invoice_number,
            "issued_at": invoice.issued_at.isoformat(),
            "buyer_name": invoice.buyer_name,
            "buyer_document_number": invoice.buyer_document_number,
            "buyer_address": invoice.buyer_address,
            "buyer_country": invoice.buyer_country,
            "payment_method": invoice.payment_method,
            "payment_note": invoice.payment_note,
            "subtotal": str(invoice.subtotal),
            "vat_amount": str(invoice.vat_amount),
            "total": str(invoice.total),
            "currency": invoice.currency,
            "zki": invoice.zki,
            "jir": invoice.jir,
            "fiscal_status": invoice.fiscal_status,
            "fiscal_error": invoice.fiscal_error,
            "fiscalized_at": invoice.fiscalized_at.isoformat() if invoice.fiscalized_at else None,
            "pdf_url": pdf_url,
            "public_access_token": str(invoice.public_access_token),
            "email_sent_at": invoice.email_sent_at.isoformat() if invoice.email_sent_at else None,
            "lines": [
                {
                    "description": line.description,
                    "quantity": str(line.quantity),
                    "unit_price": str(line.unit_price),
                    "vat_rate": str(line.vat_rate),
                    "vat_amount": str(line.vat_amount),
                    "line_total": str(line.line_total),
                    "line_kind": line.line_kind,
                }
                for line in invoice.lines.order_by("sort_order", "id")
            ],
        }


class ReservationInvoiceView(TenantAPIView, InvoiceSerializerMixin, APIView):
    permission_classes = [HasReceptionAccess, DenyAdminScopes]

    def get_permissions(self):
        if self.request.method == "POST":
            self.required_scopes = ["reception:write"]
        else:
            self.required_scopes = ["reception:read"]
        return [permission() for permission in self.permission_classes]

    def get(self, request, pk: int):
        reservation = get_object_or_404(
            Reservation.objects.for_tenant(request.tenant),
            pk=pk,
        )
        invoice = getattr(reservation, "invoice", None)
        if invoice is None:
            return Response({"detail": "Invoice not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(self.serialize_invoice(invoice))

    def post(self, request, pk: int):
        if _vat_settings_for_tenant(request.tenant) is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        reservation = get_object_or_404(
            Reservation.objects.for_tenant(request.tenant).select_related("invoice"),
            pk=pk,
        )
        existing = getattr(reservation, "invoice", None)
        if existing is not None:
            return Response(self.serialize_invoice(existing))

        if reservation.status != Reservation.Status.CHECKED_OUT:
            return Response(
                {"status": "error", "reason": "not_checked_out"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from apps.billing.exceptions import FiscalConfigError, InvoiceBuildError
        from apps.billing.services.issue import issue_guest_invoice
        from apps.billing.tasks import fiscalize_invoice, send_invoice_email_task
        from apps.communications.invoice_email import resolve_invoice_recipient

        try:
            invoice = issue_guest_invoice(reservation)
        except FiscalConfigError as exc:
            return Response(
                {
                    "status": "error",
                    "reason": "fiscal_config_incomplete",
                    "detail": str(exc),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        except InvoiceBuildError as exc:
            return Response(
                {
                    "status": "error",
                    "reason": "invoice_build_failed",
                    "detail": str(exc),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        fiscalize_invoice.delay(invoice.pk)
        if resolve_invoice_recipient(reservation):
            send_invoice_email_task.delay(invoice.pk)

        return Response(self.serialize_invoice(invoice), status=status.HTTP_201_CREATED)


class ReservationInvoicePdfView(ReceptionReadView, APIView):
    def get(self, request, pk: int):
        if _vat_settings_for_tenant(request.tenant) is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        try:
            _reservation, invoice = _get_reservation_invoice(request, pk)
        except Invoice.DoesNotExist:
            return Response({"detail": "Invoice not found."}, status=status.HTTP_404_NOT_FOUND)
        if not invoice.pdf_file:
            return Response({"detail": "PDF not available."}, status=status.HTTP_404_NOT_FOUND)
        return FileResponse(
            invoice.pdf_file.open("rb"),
            as_attachment=True,
            filename=f"racun-{invoice.invoice_number.replace('/', '-')}.pdf",
            content_type="application/pdf",
        )


class ReservationInvoiceSendEmailView(ReceptionWriteView, APIView):
    def post(self, request, pk: int):
        if _vat_settings_for_tenant(request.tenant) is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        try:
            reservation, invoice = _get_reservation_invoice(request, pk)
        except Invoice.DoesNotExist:
            return Response({"detail": "Invoice not found."}, status=status.HTTP_404_NOT_FOUND)

        serializer = InvoiceSendEmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        raw_email = (serializer.validated_data.get("email") or "").strip()
        guest_email_saved = False

        if raw_email:
            try:
                validate_email(raw_email)
            except ValidationError:
                return Response(
                    {"status": "error", "reason": "invalid_email"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            primary_guest = reservation.guests.filter(is_primary=True).first()
            if primary_guest is None:
                return Response(
                    {"status": "error", "reason": "no_primary_guest"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            primary_guest.email = raw_email
            primary_guest.save(update_fields=["email", "updated_at"])
            guest_email_saved = True

        result = send_invoice_email(invoice.pk)
        if result.get("status") == "sent":
            return Response(
                {
                    "status": "sent",
                    "recipient": result.get("recipient"),
                    "guest_email_saved": guest_email_saved,
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


class InvoicePdfView(ReceptionReadView, APIView):
    def get(self, request, invoice_id: int):
        invoice = get_object_or_404(
            Invoice.objects.for_tenant(request.tenant).select_related("tenant"),
            pk=invoice_id,
        )
        if not invoice.pdf_file:
            return Response({"detail": "PDF not available."}, status=status.HTTP_404_NOT_FOUND)
        return FileResponse(
            invoice.pdf_file.open("rb"),
            as_attachment=True,
            filename=invoice.pdf_file.name.rsplit("/", 1)[-1],
            content_type="application/pdf",
        )


def _invoice_unavailable_response() -> HttpResponse:
    html = render_to_string("billing/invoice_unavailable.html")
    return HttpResponse(html, status=404)


class PublicInvoiceHtmlView(InvoiceSerializerMixin, APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, public_access_token):
        invoice = (
            Invoice.objects.select_related("tenant", "reservation", "reservation__property")
            .filter(public_access_token=public_access_token)
            .first()
        )
        if invoice is None:
            return _invoice_unavailable_response()

        settings, _ = TenantFiscalSettings.objects.get_or_create(tenant=invoice.tenant)
        rendered = render_invoice_html(invoice, settings)
        invoice_styles, invoice_body_html = split_rendered_invoice_html(rendered)
        property_name = ""
        if invoice.reservation_id and invoice.reservation.property_id:
            property_name = invoice.reservation.property.name
        page_title = f"Invoice {invoice.invoice_number}"
        if property_name:
            page_title = f"{page_title} — {property_name}"
        total_display = f"{invoice.total.quantize(Decimal('0.01')):.2f}".replace(".", ",")
        html = render_to_string(
            "billing/invoice_guest_portal.html",
            {
                "invoice": invoice,
                "invoice_styles": invoice_styles,
                "invoice_body_html": invoice_body_html,
                "property_name": property_name,
                "issued_at_display": invoice.issued_at.strftime("%d.%m.%Y %H:%M"),
                "amount_display": f"{total_display} {invoice.currency}",
                "page_title": page_title,
            },
        )
        return HttpResponse(html)


class PublicInvoicePdfView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, public_access_token):
        invoice = (
            Invoice.objects.select_related("tenant")
            .filter(public_access_token=public_access_token)
            .first()
        )
        if invoice is None:
            return _invoice_unavailable_response()
        if not invoice.pdf_file:
            return _invoice_unavailable_response()
        return FileResponse(
            invoice.pdf_file.open("rb"),
            as_attachment=True,
            filename=f"racun-{invoice.invoice_number.replace('/', '-')}.pdf",
            content_type="application/pdf",
        )
