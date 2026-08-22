"""Reception API — send guest payment instructions."""

from __future__ import annotations

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.api.reception_views import ReceptionWriteView
from apps.communications.guest_payment_distribute import (
    VALID_PAYMENT_CHANNELS,
    send_guest_payment_link,
)
from apps.reservations.models import GuestPaymentAccessCreatedFrom, Reservation


class ReservationPaymentInstructionsSendView(ReceptionWriteView, APIView):
    """POST …/reservations/{pk}/payment-instructions/send/ — WhatsApp or email."""

    def post(self, request, pk: int):
        reservation = (
            Reservation.objects.filter(pk=pk, tenant_id=request.tenant.pk)
            .select_related("property", "tenant")
            .first()
        )
        if reservation is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        channel = str(request.data.get("channel") or "").strip().lower()
        if channel not in VALID_PAYMENT_CHANNELS:
            return Response(
                {"detail": "channel must be whatsapp or email."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        result = send_guest_payment_link(
            reservation,
            channel=channel,
            payment_created_from=GuestPaymentAccessCreatedFrom.RECEPTION_MANUAL,
            created_from="reception_payment_send",
        )

        send_status = str(result.get("status") or "failed")
        if send_status == "skipped":
            reason = str(result.get("reason") or "skipped")
            if reason == "no_email":
                return Response(
                    {"detail": "Reservation has no guest email.", "reason": reason},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if reason == "no_amount":
                return Response(
                    {"detail": "Reservation amount is required.", "reason": reason},
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
                    "payment_url": result.get("payment_url"),
                    "access_id": result.get("access_id"),
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(
            {
                "status": send_status,
                "channel": channel,
                "reservation_id": reservation.pk,
                "payment_url": result.get("payment_url"),
                "access_id": result.get("access_id"),
                "draft_id": result.get("draft_id"),
            },
            status=status.HTTP_200_OK,
        )
