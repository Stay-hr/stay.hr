from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.integrations.models import IntegrationConfig, WhatsAppInboundRouting, WhatsAppMessage
from apps.integrations.whatsapp.reservation_lookup import (
    extract_booking_code_from_text,
    find_reservation_by_booking_code,
    find_reservation_for_wa_id,
)
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant

User = get_user_model()

THREAD_WINDOW_HOURS = 48


def is_platform_integration(integration: IntegrationConfig) -> bool:
    return bool(integration.is_platform_default or integration.tenant.is_system)


def route_inbound_message(
    *,
    message: WhatsAppMessage,
    integration: IntegrationConfig,
) -> WhatsAppInboundRouting:
    routing, _created = WhatsAppInboundRouting.objects.get_or_create(
        message=message,
        defaults={
            "tenant_id": message.tenant_id,
            "status": WhatsAppInboundRouting.Status.PENDING,
        },
    )
    if routing.status != WhatsAppInboundRouting.Status.PENDING:
        return routing

    if is_platform_integration(integration):
        result = _route_platform_inbound(message)
    else:
        result = _route_tenant_inbound(message, tenant_id=integration.tenant_id)

    routing.status = result["status"]
    routing.routing_method = result.get("routing_method") or ""
    routing.candidate_reservations = result.get("candidate_reservations") or []
    routing.resolved_tenant_id = result.get("resolved_tenant_id")
    routing.resolved_reservation_id = result.get("resolved_reservation_id")
    if result.get("resolved_reservation_id"):
        routing.resolved_at = timezone.now()
    routing.save(
        update_fields=[
            "status",
            "routing_method",
            "candidate_reservations",
            "resolved_tenant",
            "resolved_reservation",
            "resolved_at",
            "updated_at",
        ]
    )

    if routing.resolved_reservation_id and message.reservation_id is None:
        message.reservation_id = routing.resolved_reservation_id
        message.save(update_fields=["reservation"])

    return routing


def _route_tenant_inbound(message: WhatsAppMessage, *, tenant_id: int) -> dict:
    reservation = _match_by_thread(wa_id=message.wa_id)
    if reservation is not None and reservation.tenant_id == tenant_id:
        return _routed_result(reservation, WhatsAppInboundRouting.RoutingMethod.THREAD)

    code = extract_booking_code_from_text(message.body)
    if code:
        reservation = find_reservation_by_booking_code(tenant_id=tenant_id, code=code)
        if reservation is not None:
            return _routed_result(
                reservation,
                WhatsAppInboundRouting.RoutingMethod.BOOKING_CODE,
            )

    reservation = find_reservation_for_wa_id(tenant_id=tenant_id, wa_id=message.wa_id)
    if reservation is not None:
        return _routed_result(reservation, WhatsAppInboundRouting.RoutingMethod.PHONE)

    return {"status": WhatsAppInboundRouting.Status.UNROUTED}


def _route_platform_inbound(message: WhatsAppMessage) -> dict:
    reservation = _match_by_thread(wa_id=message.wa_id)
    if reservation is not None:
        return _routed_result(reservation, WhatsAppInboundRouting.RoutingMethod.THREAD)

    code = extract_booking_code_from_text(message.body)
    if code:
        matches = _find_reservations_by_booking_code_cross_tenant(code)
        if len(matches) == 1:
            return _routed_result(
                matches[0],
                WhatsAppInboundRouting.RoutingMethod.BOOKING_CODE,
            )
        if len(matches) > 1:
            return _ambiguous_result(matches)

    matches = _find_reservations_by_phone_cross_tenant(message.wa_id)
    if len(matches) == 1:
        return _routed_result(matches[0], WhatsAppInboundRouting.RoutingMethod.PHONE)
    if len(matches) > 1:
        return _ambiguous_result(matches)

    return {"status": WhatsAppInboundRouting.Status.UNROUTED}


def resolve_business_app_echo_reservation(
    *,
    wa_id: str,
    integration: IntegrationConfig,
) -> tuple[Reservation | None, str]:
    """Match reservation for a Business App outbound echo.

    Cascade: thread → phone → none. Unmatched (none) is expected, not an error.
    Does not create WhatsAppInboundRouting rows.
    """
    # TODO: MATCH_WINDOW configurable (thread match currently 48h hard-coded)
    reservation = _match_by_thread(wa_id=wa_id)
    if reservation is not None:
        if is_platform_integration(integration) or reservation.tenant_id == integration.tenant_id:
            return reservation, "thread"
        reservation = None

    if is_platform_integration(integration):
        matches = _find_reservations_by_phone_cross_tenant(wa_id)
        if len(matches) == 1:
            return matches[0], "phone"
        return None, "none"

    reservation = find_reservation_for_wa_id(tenant_id=integration.tenant_id, wa_id=wa_id)
    if reservation is not None:
        return reservation, "phone"
    return None, "none"


def _match_by_thread(*, wa_id: str) -> Reservation | None:
    cutoff = timezone.now() - timedelta(hours=THREAD_WINDOW_HOURS)
    recent = (
        WhatsAppMessage.objects.filter(
            direction=WhatsAppMessage.Direction.OUTBOUND,
            wa_id=wa_id,
            reservation__isnull=False,
            created_at__gte=cutoff,
        )
        .select_related("reservation", "reservation__tenant")
        .order_by("-created_at")
        .first()
    )
    return recent.reservation if recent else None


def _active_hotel_tenants():
    return Tenant.objects.filter(status=Tenant.Status.ACTIVE, is_system=False)


def _find_reservations_by_booking_code_cross_tenant(code: str) -> list[Reservation]:
    matches: list[Reservation] = []
    seen: set[int] = set()
    for tenant in _active_hotel_tenants():
        reservation = find_reservation_by_booking_code(tenant_id=tenant.pk, code=code)
        if reservation is not None and reservation.pk not in seen:
            matches.append(reservation)
            seen.add(reservation.pk)
    return matches


def _find_reservations_by_phone_cross_tenant(wa_id: str) -> list[Reservation]:
    matches: list[Reservation] = []
    seen: set[int] = set()
    for tenant in _active_hotel_tenants():
        reservation = find_reservation_for_wa_id(tenant_id=tenant.pk, wa_id=wa_id)
        if reservation is not None and reservation.pk not in seen:
            matches.append(reservation)
            seen.add(reservation.pk)
    if len(matches) <= 1:
        return matches
    today = timezone.localdate()
    return sorted(matches, key=lambda row: abs((row.check_in - today).days))


def _routed_result(reservation: Reservation, method: str) -> dict:
    return {
        "status": WhatsAppInboundRouting.Status.ROUTED,
        "routing_method": method,
        "resolved_tenant_id": reservation.tenant_id,
        "resolved_reservation_id": reservation.pk,
        "candidate_reservations": [],
    }


def _ambiguous_result(reservations: list[Reservation]) -> dict:
    return {
        "status": WhatsAppInboundRouting.Status.AMBIGUOUS,
        "routing_method": "",
        "resolved_tenant_id": None,
        "resolved_reservation_id": None,
        "candidate_reservations": [
            {
                "reservation_id": row.pk,
                "tenant_id": row.tenant_id,
                "booking_code": row.booking_code,
                "check_in": row.check_in.isoformat(),
                "check_out": row.check_out.isoformat(),
                "booker_name": row.booker_name,
            }
            for row in reservations
        ],
    }


def manual_link_routing(
    *,
    routing: WhatsAppInboundRouting,
    reservation: Reservation,
    user: User | None = None,
    notes: str = "",
) -> WhatsAppInboundRouting:
    routing.status = WhatsAppInboundRouting.Status.ROUTED
    routing.routing_method = WhatsAppInboundRouting.RoutingMethod.MANUAL
    routing.resolved_tenant = reservation.tenant
    routing.resolved_reservation = reservation
    routing.candidate_reservations = []
    routing.resolved_at = timezone.now()
    routing.resolved_by = user
    routing.notes = notes
    routing.save()

    message = routing.message
    if message.reservation_id != reservation.pk:
        message.reservation = reservation
        message.save(update_fields=["reservation"])

    return routing


def dismiss_routing(
    *,
    routing: WhatsAppInboundRouting,
    user: User | None = None,
    notes: str = "",
) -> WhatsAppInboundRouting:
    routing.status = WhatsAppInboundRouting.Status.DISMISSED
    routing.resolved_at = timezone.now()
    routing.resolved_by = user
    routing.notes = notes
    routing.save()
    return routing
