from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.db.models import Q, QuerySet
from django.utils import timezone

from apps.integrations.channex.ari_service import get_active_channex_integration
from apps.integrations.channex.exceptions import ChannexApiError, ChannexBookingIngestError
from apps.integrations.channex.message_service import (
    _reservation_can_sync_messages,
    relink_unlinked_channex_messages,
    sync_booking_messages_from_channex,
)
from apps.integrations.models import ChannexMessage
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

# ADR 0019 Phase B — automatic Channex reconcile membership.
CHANNEX_RECONCILE_PRE_ARRIVAL_DAYS = 7
CHANNEX_RECONCILE_POST_CHECKOUT_DAYS = 1
CHANNEX_RECONCILE_ACTIVITY_DAYS = 7

_CHANNEX_RECONCILE_EXCLUDED_STATUSES = (
    Reservation.Status.CANCELED,
    Reservation.Status.NO_SHOW,
    Reservation.Status.REFUSED,
    Reservation.Status.PENDING,
)


def channex_reconcile_membership_qs(tenant: Tenant) -> QuerySet[Reservation]:
    """Reservations in automatic Channex reconcile: Eligible ∩ (A ∪ B ∪ C ∪ D).

    Eligible (applied after the union, including D):
    - import_source = channex
    - status not in canceled / no_show / refused / pending

    A: expected, check_in ∈ [today, today+7d]
    B: checked_in
    C: checked_out, check_out ∈ [today−1d, today]
    D: ChannexMessage.created_at ≥ now−7d (ingest time)

    D must not re-admit a reservation the status filter excludes.
    """
    today = timezone.localdate()
    activity_since = timezone.now() - timedelta(days=CHANNEX_RECONCILE_ACTIVITY_DAYS)
    active_ids = ChannexMessage.objects.filter(
        tenant=tenant,
        reservation_id__isnull=False,
        created_at__gte=activity_since,
    ).values("reservation_id")

    eligible = Reservation.objects.filter(
        tenant=tenant,
        import_source="channex",
    ).exclude(status__in=_CHANNEX_RECONCILE_EXCLUDED_STATUSES)

    return (
        eligible.filter(
            Q(
                status=Reservation.Status.EXPECTED,
                check_in__gte=today,
                check_in__lte=today + timedelta(days=CHANNEX_RECONCILE_PRE_ARRIVAL_DAYS),
            )
            | Q(status=Reservation.Status.CHECKED_IN)
            | Q(
                status=Reservation.Status.CHECKED_OUT,
                check_out__gte=today - timedelta(days=CHANNEX_RECONCILE_POST_CHECKOUT_DAYS),
                check_out__lte=today,
            )
            | Q(pk__in=active_ids)
        )
        .select_related("property", "tenant")
        .order_by("pk")
        .distinct()
    )


@shared_task
def sync_channex_messages_for_upcoming_checkins(*, tenant_slug: str = "uzorita") -> dict:
    """Pull Channex messages for ADR 0019 Phase B membership (A∪B∪C∪D)."""
    result = {
        "synced": 0,
        "skipped": 0,
        "failed": 0,
        "relinked": 0,
        "candidates": 0,
    }

    tenant = Tenant.objects.filter(slug=tenant_slug).first()
    if tenant is None:
        return {**result, "error": "tenant_not_found"}

    try:
        integration = get_active_channex_integration(tenant_slug)
    except ChannexBookingIngestError as exc:
        return {**result, "error": str(exc)}

    result["relinked"] = relink_unlinked_channex_messages(tenant)

    by_id: dict[int, Reservation] = {
        reservation.pk: reservation for reservation in channex_reconcile_membership_qs(tenant)
    }
    result["candidates"] = len(by_id)

    for reservation in by_id.values():
        if not _reservation_can_sync_messages(reservation):
            result["skipped"] += 1
            continue
        try:
            rows = sync_booking_messages_from_channex(integration, reservation)
            result["synced"] += len(rows)
        except (ChannexBookingIngestError, ChannexApiError) as exc:
            result["failed"] += 1
            logger.warning(
                "channex message reconcile failed",
                extra={"reservation_id": reservation.pk, "error": str(exc)},
            )

    logger.info(
        "channex message reconcile cycle",
        extra={
            "tenant_slug": tenant_slug,
            "candidates": result["candidates"],
            "synced": result["synced"],
            "skipped": result["skipped"],
            "failed": result["failed"],
            "relinked": result["relinked"],
        },
    )
    return result
