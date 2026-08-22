"""Public token-scoped guest payment instructions API (AllowAny)."""

from __future__ import annotations

from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.reservations.guest_payment_access import (
    evaluate_payment_access,
    get_payment_access_by_token,
)
from apps.reservations.guest_payment_context import (
    build_guest_payment_context,
    serialize_guest_payment_context,
)


def _load_access_or_404(token):
    access = get_payment_access_by_token(token)
    if access is None:
        from django.http import Http404

        raise Http404("Payment access not found.")
    return access


def _access_error_response(access_result) -> Response:
    return Response({"status": access_result.gate_status}, status=access_result.http_status)


class GuestPaymentView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, token):
        access = _load_access_or_404(token)
        gate = evaluate_payment_access(access)
        if not gate.allowed:
            return _access_error_response(gate)

        ctx = build_guest_payment_context(access)
        return Response(serialize_guest_payment_context(ctx))
