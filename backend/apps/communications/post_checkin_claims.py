"""G5: atomic post-checkin send claims (portal + arrival ask).

Provider I/O must run **outside** the claim transaction. ``pending``/``sent``
block parallel work; ``failed`` is reclaimable for retry.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db import IntegrityError, transaction

from apps.communications.models import PostCheckinSendClaim, PostCheckinSendClaimStatus
from apps.reservations.models import Reservation

logger = logging.getLogger(__name__)


def portal_claim_key(
    *,
    session_id: int | None,
    portal_token,
    channel: str,
) -> str:
    return f"guest_portal:{session_id or 0}:{portal_token}:{channel}"


def arrival_ask_claim_key(*, session_id: int | None) -> str:
    return f"arrival_ask:{session_id or 0}"


@dataclass(frozen=True)
class ClaimAcquireResult:
    claim: PostCheckinSendClaim | None
    """Acquired claim when we own the send; None when blocked."""

    blocked_status: str | None = None
    """``pending`` or ``sent`` when another worker owns/owned the key."""


def try_acquire_claim(
    *,
    claim_key: str,
    reservation: Reservation,
) -> ClaimAcquireResult:
    """Atomically claim ``claim_key`` for a send attempt.

    - Inserts ``pending`` when absent.
    - Reclaims ``failed`` → ``pending``.
    - Returns blocked when ``pending`` or ``sent``.
    """
    with transaction.atomic():
        existing = (
            PostCheckinSendClaim.objects.select_for_update()
            .filter(claim_key=claim_key)
            .first()
        )
        if existing is None:
            try:
                claim = PostCheckinSendClaim.objects.create(
                    tenant_id=reservation.tenant_id,
                    reservation=reservation,
                    claim_key=claim_key,
                    status=PostCheckinSendClaimStatus.PENDING,
                )
            except IntegrityError:
                existing = (
                    PostCheckinSendClaim.objects.select_for_update()
                    .filter(claim_key=claim_key)
                    .first()
                )
                if existing is None:
                    raise
                if existing.status == PostCheckinSendClaimStatus.FAILED:
                    existing.status = PostCheckinSendClaimStatus.PENDING
                    existing.save(update_fields=["status", "updated_at"])
                    return ClaimAcquireResult(claim=existing)
                return ClaimAcquireResult(
                    claim=None,
                    blocked_status=existing.status,
                )
            return ClaimAcquireResult(claim=claim)

        if existing.status == PostCheckinSendClaimStatus.FAILED:
            existing.status = PostCheckinSendClaimStatus.PENDING
            existing.save(update_fields=["status", "updated_at"])
            return ClaimAcquireResult(claim=existing)

        return ClaimAcquireResult(
            claim=None,
            blocked_status=existing.status,
        )


def mark_claim_sent(claim: PostCheckinSendClaim) -> PostCheckinSendClaim:
    PostCheckinSendClaim.objects.filter(pk=claim.pk).update(
        status=PostCheckinSendClaimStatus.SENT,
    )
    claim.status = PostCheckinSendClaimStatus.SENT
    return claim


def mark_claim_failed(claim: PostCheckinSendClaim) -> PostCheckinSendClaim:
    PostCheckinSendClaim.objects.filter(pk=claim.pk).update(
        status=PostCheckinSendClaimStatus.FAILED,
    )
    claim.status = PostCheckinSendClaimStatus.FAILED
    return claim
