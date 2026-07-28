"""Reception API for PDV-S foreign-service invoices (ADR 0012 / PR1.1)."""

from __future__ import annotations

import re

from django.http import HttpResponse
from rest_framework import serializers, status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.api.reception_views import ReceptionReadView
from apps.billing.models import ForeignServiceInvoice
from apps.billing.services.eporezna.errors import EporeznaError
from apps.billing.services.eporezna.import_service import import_foreign_service_invoice
from apps.billing.services.eporezna.pdvs.builder import PDVSBuilder
from apps.billing.services.eporezna.pdvs.validate import validate_pdvs_xml
from apps.billing.services.eporezna.readiness import fiscal_pdvs_readiness

MAX_PDF_BYTES = 8 * 1024 * 1024
_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _serialize_invoice(
    inv: ForeignServiceInvoice,
    *,
    created: bool | None = None,
    already_imported: bool | None = None,
) -> dict:
    payload = {
        "id": inv.pk,
        "provider": inv.provider,
        "invoice_number": inv.invoice_number,
        "tax_period": inv.tax_period,
        "taxable_amount": str(inv.taxable_amount),
        "currency": inv.currency,
        "supplier_name": inv.supplier_name,
        "supplier_country": inv.supplier_country,
        "supplier_vat_id": inv.supplier_vat_id,
        "invoice_date": inv.invoice_date.isoformat(),
        "imported_at": inv.imported_at.isoformat().replace("+00:00", "Z"),
    }
    if created is not None:
        payload["created"] = created
    if already_imported is not None:
        payload["already_imported"] = already_imported
    return payload


def _parse_period(raw: str | None) -> str | None:
    if not raw:
        return None
    value = raw.strip()
    if not _PERIOD_RE.match(value):
        return None
    return value


class ForeignServiceInvoiceUploadSerializer(serializers.Serializer):
    file = serializers.FileField()

    def validate_file(self, value):
        if value.size > MAX_PDF_BYTES:
            raise serializers.ValidationError(
                f"File too large (max {MAX_PDF_BYTES} bytes)."
            )
        name = (getattr(value, "name", "") or "").lower()
        content_type = (getattr(value, "content_type", "") or "").lower()
        if not (name.endswith(".pdf") or "pdf" in content_type):
            raise serializers.ValidationError("Expected a PDF file.")
        return value


class EporeznaStatusView(ReceptionReadView, APIView):
    def get(self, request):
        return Response(fiscal_pdvs_readiness(request.tenant).as_dict())


class ForeignServiceInvoiceListCreateView(ReceptionReadView, APIView):
    parser_classes = [MultiPartParser, FormParser]

    def get_permissions(self):
        if self.request.method == "POST":
            self.required_scopes = ["reception:write"]
        else:
            self.required_scopes = ["reception:read"]
        return [permission() for permission in self.permission_classes]

    def get(self, request):
        period = _parse_period(request.query_params.get("period"))
        if period is None:
            return Response(
                {"detail": "Query parameter period=YYYY-MM is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        readiness = fiscal_pdvs_readiness(request.tenant)
        qs = ForeignServiceInvoice.objects.filter(
            tenant=request.tenant,
            tax_period=period,
        ).order_by("-imported_at", "-id")
        results = [_serialize_invoice(inv) for inv in qs]
        return Response(
            {
                "period": period,
                "count": len(results),
                **readiness.as_dict(),
                "results": results,
            }
        )

    def post(self, request):
        serializer = ForeignServiceInvoiceUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        uploaded = serializer.validated_data["file"]
        raw = uploaded.read()
        try:
            result = import_foreign_service_invoice(
                tenant=request.tenant,
                raw=raw,
                filename=uploaded.name or "invoice.pdf",
                user=getattr(request, "user", None),
            )
        except EporeznaError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        assert result.invoice is not None
        created = not result.already_imported
        payload = _serialize_invoice(
            result.invoice,
            created=created,
            already_imported=result.already_imported,
        )
        return Response(
            payload,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class PdvsExportView(ReceptionReadView, APIView):
    def get(self, request):
        period = _parse_period(request.query_params.get("period"))
        if period is None:
            return Response(
                {"detail": "Query parameter period=YYYY-MM is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        readiness = fiscal_pdvs_readiness(request.tenant)
        if not readiness.configured:
            return Response(
                {
                    "detail": "PDV-S fiscal settings incomplete.",
                    "missing": list(readiness.missing),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        has_invoices = ForeignServiceInvoice.objects.filter(
            tenant=request.tenant,
            tax_period=period,
        ).exists()
        if not has_invoices:
            return Response(
                {"detail": "No foreign service invoices for period."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            export = PDVSBuilder().build(tenant=request.tenant, period=period)
            validate_pdvs_xml(export.xml_bytes)
        except EporeznaError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        response = HttpResponse(export.xml_bytes, content_type="application/xml")
        response["Content-Disposition"] = f'attachment; filename="{export.filename}"'
        return response
