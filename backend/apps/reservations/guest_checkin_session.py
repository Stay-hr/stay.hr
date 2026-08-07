"""Guest web check-in session CRUD and access window helpers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Literal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.core.timezone import property_local_now
from apps.reservations.checkin_readiness import effective_session_status
from apps.reservations.models import (
    GuestCheckInSession,
    GuestCheckInSessionCreatedFrom,
    GuestCheckInSessionStatus,
    Reservation,
)
from apps.tenants.models import TenantDomain

logger = logging.getLogger(__name__)

SessionGateStatus = Literal[
    "active",
    "ready",
    "not_open_yet",
    "completed",
    "expired",
    "revoked",
]


@dataclass(frozen=True)
class GuestCheckInWindow:
    opens_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class SessionAccessResult:
    allowed: bool
    http_status: int
    gate_status: SessionGateStatus
    opens_at: datetime | None = None


def guest_checkin_window(reservation: Reservation) -> GuestCheckInWindow:
    """Single source of truth for session opens_at / expires_at."""
    prop = reservation.property
    days_before = max(int(prop.guest_checkin_opens_days_before or 7), 0)
    opens_local_date = reservation.check_in - timedelta(days=days_before)
    opens_local = datetime.combine(opens_local_date, time.min)
    tz = property_local_now(prop).tzinfo
    opens_at = timezone.make_aware(opens_local, tz) if tz else timezone.make_aware(opens_local)

    checkout_local = datetime.combine(reservation.check_out + timedelta(days=1), time.max)
    expires_at = (
        timezone.make_aware(checkout_local, tz) if tz else timezone.make_aware(checkout_local)
    )
    return GuestCheckInWindow(opens_at=opens_at, expires_at=expires_at)


def _maybe_expire_session(session: GuestCheckInSession, *, now: datetime) -> GuestCheckInSession:
    if session.status != GuestCheckInSessionStatus.ACTIVE:
        return session
    if now <= session.expires_at:
        return session
    session.status = GuestCheckInSessionStatus.EXPIRED
    session.save(update_fields=["status", "updated_at"])
    return session


def evaluate_session_access(
    session: GuestCheckInSession,
    reservation: Reservation,
    *,
    now: datetime | None = None,
) -> SessionAccessResult:
    now = now or timezone.now()
    session = _maybe_expire_session(session, now=now)

    if session.status == GuestCheckInSessionStatus.COMPLETED:
        return SessionAccessResult(False, 410, "completed")
    if session.status == GuestCheckInSessionStatus.EXPIRED:
        return SessionAccessResult(False, 410, "expired")
    if session.status == GuestCheckInSessionStatus.REVOKED:
        return SessionAccessResult(False, 410, "revoked")

    if now < session.opens_at:
        return SessionAccessResult(
            False,
            403,
            "not_open_yet",
            opens_at=session.opens_at,
        )

    effective = effective_session_status(session, reservation)
    return SessionAccessResult(True, 200, effective)  # type: ignore[arg-type]


def get_session_by_token(token) -> GuestCheckInSession | None:
    return (
        GuestCheckInSession.objects.select_related(
            "reservation",
            "reservation__property",
            "reservation__tenant",
        )
        .filter(token=token)
        .first()
    )


def get_active_session(reservation: Reservation) -> GuestCheckInSession | None:
    return (
        GuestCheckInSession.objects.filter(
            reservation=reservation,
            status=GuestCheckInSessionStatus.ACTIVE,
        )
        .order_by("-created_at")
        .first()
    )


def reservation_has_completed_web_checkin(reservation: Reservation) -> bool:
    """True if any GuestCheckInSession for this reservation is completed.

    Does not filter by ``created_from`` — Channex, email, reception, and WA
    links all lead to the same HTML form. Active/ready sessions do not count.
    """
    return reservation.guest_checkin_sessions.filter(
        status=GuestCheckInSessionStatus.COMPLETED,
    ).exists()


@transaction.atomic
def expire_stale_sessions(*, now: datetime | None = None) -> dict:
    """Mark ACTIVE sessions past ``expires_at`` as EXPIRED. Used by Celery beat."""
    now = now or timezone.now()
    stale = GuestCheckInSession.objects.filter(
        status=GuestCheckInSessionStatus.ACTIVE,
        expires_at__lt=now,
    )
    expired = stale.update(status=GuestCheckInSessionStatus.EXPIRED)
    if expired:
        logger.info("guest_checkin expired_stale_sessions count=%s", expired)
    return {"expired": expired}


@transaction.atomic
def ensure_active_session(
    reservation: Reservation,
    *,
    created_from: str,
    wa_id: str = "",
) -> GuestCheckInSession:
    """Return the single active session for a reservation, creating if needed."""
    existing = get_active_session(reservation)
    if existing is not None:
        return existing

    window = guest_checkin_window(reservation)
    return GuestCheckInSession.objects.create(
        tenant_id=reservation.tenant_id,
        reservation=reservation,
        status=GuestCheckInSessionStatus.ACTIVE,
        opens_at=window.opens_at,
        expires_at=window.expires_at,
        created_from=created_from,
        wa_id=(wa_id or "").strip(),
    )


@transaction.atomic
def revoke_session(session: GuestCheckInSession) -> GuestCheckInSession:
    if session.status == GuestCheckInSessionStatus.REVOKED:
        return session
    session.status = GuestCheckInSessionStatus.REVOKED
    session.save(update_fields=["status", "updated_at"])
    return session


@transaction.atomic
def regenerate_session(
    reservation: Reservation,
    *,
    created_from: str,
    wa_id: str = "",
) -> tuple[GuestCheckInSession | None, GuestCheckInSession]:
    """Revoke current active session (if any) and create a new active one."""
    old = get_active_session(reservation)
    if old is not None:
        revoke_session(old)
    new = ensure_active_session(
        reservation,
        created_from=created_from,
        wa_id=wa_id,
    )
    return old, new


@transaction.atomic
def mark_session_completed(session: GuestCheckInSession) -> GuestCheckInSession:
    if session.status != GuestCheckInSessionStatus.ACTIVE:
        return session
    now = timezone.now()
    session.status = GuestCheckInSessionStatus.COMPLETED
    session.completed_at = now
    session.save(update_fields=["status", "completed_at", "updated_at"])
    return session


def touch_session_activity(session: GuestCheckInSession) -> None:
    GuestCheckInSession.objects.filter(pk=session.pk).update(last_activity_at=timezone.now())


def resolve_guest_checkin_base_url(reservation: Reservation) -> str:
    domain = (
        TenantDomain.objects.filter(
            tenant_id=reservation.tenant_id,
            property_id=reservation.property_id,
            is_verified=True,
        )
        .order_by("-is_primary", "id")
        .values_list("domain", flat=True)
        .first()
    )
    if not domain:
        domain = (
            TenantDomain.objects.filter(
                tenant_id=reservation.tenant_id,
                property__isnull=True,
                is_verified=True,
            )
            .order_by("-is_primary", "id")
            .values_list("domain", flat=True)
            .first()
        )
    if domain:
        return f"https://{domain}".rstrip("/")
    fallback = getattr(settings, "STAY_BOOKING_PUBLIC_URL", "https://stay.hr")
    return fallback.rstrip("/")


def build_guest_checkin_url(session: GuestCheckInSession, reservation: Reservation) -> str:
    base = resolve_guest_checkin_base_url(reservation)
    return f"{base}/check-in/{session.token}"
