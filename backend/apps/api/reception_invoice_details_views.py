"""Reception API — send guest invoice-details form."""

from __future__ import annotations

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.api.reception_views import ReceptionWriteView
from apps.communications.guest_invoice_details_distribute import (
    VALID_INVOICE_DETAILS_CHANNELS,
    send_guest_invoice_details_link,
)
from apps.reservations.models import GuestInvoiceDetailsAccessCreatedFrom, Reservation


class ReservationInvoiceDetailsSendView(ReceptionWriteView, APIView):
    """POST …/reservations/{pk}/invoice-details/send/ — WhatsApp, email, or Booking."""

    def post(self, request, pk: int):
        reservation = (
            Reservation.objects.filter(pk=pk, tenant_id=request.tenant.pk)
            .select_related("property", "tenant")
            .first()
        )
        if reservation is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        channel = str(request.data.get("channel") or "").strip().lower()
        if channel not in VALID_INVOICE_DETAILS_CHANNELS:
            return Response(
                {"detail": "channel must be whatsapp, email, or booking."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        result = send_guest_invoice_details_link(
            reservation,
            channel=channel,
            access_created_from=GuestInvoiceDetailsAccessCreatedFrom.RECEPTION_MANUAL,
            created_from="reception_invoice_details_send",
        )

        send_status = str(result.get("status") or "failed")
        if send_status == "skipped":
            reason = str(result.get("reason") or "skipped")
            if reason == "no_email":
                return Response(
                    {"detail": "Reservation has no guest email.", "reason": reason},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            return Response(
                {"detail": reason, "reason": reason},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if send_status == "failed":
            return Response(
                {
                    "status": send_status,
                    "error": result.get("error"),
                    "invoice_details_url": result.get("invoice_details_url"),
                    "access_id": result.get("access_id"),
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(
            {
                "status": send_status,
                "channel": channel,
                "reservation_id": reservation.pk,
                "invoice_details_url": result.get("invoice_details_url"),
                "access_id": result.get("access_id"),
                "draft_id": result.get("draft_id"),
            },
            status=status.HTTP_200_OK,
        )
