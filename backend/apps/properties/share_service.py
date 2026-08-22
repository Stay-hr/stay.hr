"""ShareService — kind/target dispatch for Property Settings share (ADR 0008 / PR-D2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from apps.communications.guest_payment_distribute import (
    VALID_PAYMENT_CHANNELS,
    send_guest_payment_link,
)
from apps.communications.guest_portal_distribute import (
    VALID_PORTAL_CHANNELS,
    default_channel_from_completed_checkin,
    send_guest_portal_link,
)
from apps.communications.models import GuestMessageChannel
from apps.properties.guest_settings_events import (
    GuestPortalShared,
    emit_guest_portal_shared,
)
from apps.properties.models import Property
from apps.properties.property_settings_service import build_settings_capabilities
from apps.reservations.models import (
    GuestPaymentAccessCreatedFrom,
    GuestPortalAccessCreatedFrom,
    Reservation,
)

SHARE_KIND_PORTAL = "portal"
SHARE_KIND_PAYMENT = "payment"
SHARE_TARGET_RESERVATION = "reservation"

SUPPORTED_KINDS = frozenset({SHARE_KIND_PORTAL, SHARE_KIND_PAYMENT})
SUPPORTED_TARGETS = frozenset({SHARE_TARGET_RESERVATION})
# Later: guide | invoice | payment | review; guest | thread


class ShareServiceError(Exception):
    """Validation / business error for ShareService (maps to HTTP by code)."""

    def __init__(self, code: str, detail: str, *, http_status: int = 400):
        self.code = code
        self.detail = detail
        self.http_status = http_status
        super().__init__(detail)


@dataclass(frozen=True)
class ShareResult:
    kind: str
    target: str
    reservation_id: int
    channel: str
    status: str
    portal_url: str | None = None
    payment_url: str | None = None
    access_id: int | None = None
    draft_id: int | None = None
    url_draft_id: int | None = None
    reason: str | None = None
    error: str | None = None
    send: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "target": self.target,
            "reservation_id": self.reservation_id,
            "channel": self.channel,
            "status": self.status,
        }
        if self.portal_url is not None:
            payload["portal_url"] = self.portal_url
        if self.payment_url is not None:
            payload["payment_url"] = self.payment_url
        if self.access_id is not None:
            payload["access_id"] = self.access_id
        if self.draft_id is not None:
            payload["draft_id"] = self.draft_id
        if self.url_draft_id is not None:
            payload["url_draft_id"] = self.url_draft_id
        if self.reason is not None:
            payload["reason"] = self.reason
        if self.error is not None:
            payload["error"] = self.error
        return payload


def _parse_positive_int(value: Any, *, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ShareServiceError(
            "invalid_reservation_id",
            f"{field} must be a positive integer.",
        ) from exc
    if parsed <= 0:
        raise ShareServiceError(
            "invalid_reservation_id",
            f"{field} must be a positive integer.",
        )
    return parsed


def _normalize_channel(raw: Any | None, *, kind: str) -> str | None:
    if raw is None or raw == "":
        return None
    channel = str(raw).strip().lower()
    allowed = VALID_PAYMENT_CHANNELS if kind == SHARE_KIND_PAYMENT else VALID_PORTAL_CHANNELS
    if channel not in allowed:
        label = "whatsapp or email" if kind == SHARE_KIND_PAYMENT else "booking, whatsapp, or email"
        raise ShareServiceError(
            "unsupported_channel",
            f"Unsupported channel '{raw}'. Use {label}.",
        )
    return channel


class ShareService:
    """kind/target dispatch; portal ensure+send for reservation targets."""

    @staticmethod
    def share_enabled() -> bool:
        caps = build_settings_capabilities().get("capabilities") or {}
        return bool(caps.get("share"))

    @classmethod
    def share(
        cls,
        property: Property,
        payload: dict[str, Any],
        *,
        actor_id: str | None = None,
        updated_by: dict[str, Any] | None = None,
        dry_run: bool = False,
    ) -> ShareResult:
        if not cls.share_enabled():
            raise ShareServiceError(
                "settings_section_not_available",
                "Share is not available.",
                http_status=404,
            )

        kind = str(payload.get("kind") or "").strip().lower()
        target = str(payload.get("target") or "").strip().lower()

        if kind not in SUPPORTED_KINDS:
            raise ShareServiceError(
                "unsupported_kind",
                f"Unsupported kind '{payload.get('kind')}'. v1 supports: portal, payment.",
            )
        if target not in SUPPORTED_TARGETS:
            raise ShareServiceError(
                "unsupported_target",
                f"Unsupported target '{payload.get('target')}'. v1 supports: reservation.",
            )

        if "reservation_id" not in payload or payload.get("reservation_id") in (None, ""):
            raise ShareServiceError(
                "reservation_id_required",
                "reservation_id is required when target is reservation.",
            )
        reservation_id = _parse_positive_int(
            payload.get("reservation_id"),
            field="reservation_id",
        )

        reservation = (
            Reservation.objects.filter(
                pk=reservation_id,
                tenant_id=property.tenant_id,
                property_id=property.pk,
            )
            .select_related("property", "tenant")
            .first()
        )
        if reservation is None:
            raise ShareServiceError(
                "reservation_not_found",
                "Reservation not found for this property.",
                http_status=404,
            )

        channel = _normalize_channel(payload.get("channel"), kind=kind)
        if channel is None:
            if kind == SHARE_KIND_PAYMENT:
                raise ShareServiceError(
                    "channel_required",
                    "channel is required for payment share (whatsapp or email).",
                )
            channel = default_channel_from_completed_checkin(reservation)
        if channel is None:
            raise ShareServiceError(
                "channel_required",
                "channel is required when no completed check-in channel can be inferred.",
            )

        if kind == SHARE_KIND_PORTAL and target == SHARE_TARGET_RESERVATION:
            return cls._share_portal(
                reservation,
                channel=channel,
                actor_id=actor_id,
                updated_by=updated_by or {},
                dry_run=dry_run,
            )
        if kind == SHARE_KIND_PAYMENT and target == SHARE_TARGET_RESERVATION:
            return cls._share_payment(
                reservation,
                channel=channel,
                actor_id=actor_id,
                updated_by=updated_by or {},
                dry_run=dry_run,
            )

        raise ShareServiceError(
            "unsupported_combination",
            f"Unsupported kind/target combination: {kind}/{target}.",
        )

    @classmethod
    def _share_portal(
        cls,
        reservation: Reservation,
        *,
        channel: str,
        actor_id: str | None,
        updated_by: dict[str, Any],
        dry_run: bool,
    ) -> ShareResult:
        send_result = send_guest_portal_link(
            reservation,
            channel=channel,
            portal_created_from=GuestPortalAccessCreatedFrom.RECEPTION_MANUAL,
            allow_resend=True,
            dry_run=dry_run,
            created_from="reception_share",
        )

        status = str(send_result.get("status") or "failed")
        reason = send_result.get("reason")
        error = send_result.get("error")

        # Map skip reasons that are client-fixable to ShareServiceError.
        if status == "skipped":
            if reason == "no_email":
                raise ShareServiceError(
                    "no_email",
                    "Reservation has no guest email for the email channel.",
                )
            if reason == "unknown_channel":
                raise ShareServiceError(
                    "unsupported_channel",
                    f"Unsupported channel '{channel}'.",
                )
            if reason == "empty_body":
                raise ShareServiceError(
                    "empty_body",
                    "Portal message body is empty.",
                )

        result = ShareResult(
            kind=SHARE_KIND_PORTAL,
            target=SHARE_TARGET_RESERVATION,
            reservation_id=reservation.pk,
            channel=channel,
            status=status,
            portal_url=send_result.get("portal_url"),
            access_id=send_result.get("access_id"),
            draft_id=send_result.get("draft_id"),
            url_draft_id=send_result.get("url_draft_id"),
            reason=reason,
            error=error,
            send=send_result,
        )

        if status in {"sent", "queued", "partial", "dry_run"}:
            emit_guest_portal_shared(
                GuestPortalShared(
                    property_id=reservation.property_id,
                    tenant_id=reservation.tenant_id,
                    reservation_id=reservation.pk,
                    channel=channel,
                    kind=SHARE_KIND_PORTAL,
                    target=SHARE_TARGET_RESERVATION,
                    status=status,
                    actor_id=actor_id,
                    updated_by=dict(updated_by),
                )
            )

        if status == "failed":
            raise ShareServiceError(
                "share_failed",
                error or "Failed to share guest portal.",
                http_status=502,
            )

        return result

    @classmethod
    def _share_payment(
        cls,
        reservation: Reservation,
        *,
        channel: str,
        actor_id: str | None,
        updated_by: dict[str, Any],
        dry_run: bool,
    ) -> ShareResult:
        send_result = send_guest_payment_link(
            reservation,
            channel=channel,
            payment_created_from=GuestPaymentAccessCreatedFrom.RECEPTION_MANUAL,
            dry_run=dry_run,
            created_from="reception_share",
        )

        status = str(send_result.get("status") or "failed")
        reason = send_result.get("reason")
        error = send_result.get("error")

        if status == "skipped":
            if reason == "no_email":
                raise ShareServiceError(
                    "no_email",
                    "Reservation has no guest email for the email channel.",
                )
            if reason == "unknown_channel":
                raise ShareServiceError(
                    "unsupported_channel",
                    f"Unsupported channel '{channel}'.",
                )
            if reason == "no_amount":
                raise ShareServiceError(
                    "no_amount",
                    "Reservation amount is required for payment instructions.",
                )
            if reason == "empty_body":
                raise ShareServiceError(
                    "empty_body",
                    "Payment message body is empty.",
                )
            if "not available" in str(reason or "").lower():
                raise ShareServiceError(
                    "reservation_unavailable",
                    str(reason),
                )

        result = ShareResult(
            kind=SHARE_KIND_PAYMENT,
            target=SHARE_TARGET_RESERVATION,
            reservation_id=reservation.pk,
            channel=channel,
            status=status,
            payment_url=send_result.get("payment_url"),
            access_id=send_result.get("access_id"),
            draft_id=send_result.get("draft_id"),
            reason=reason,
            error=error,
            send=send_result,
        )

        if status == "failed":
            raise ShareServiceError(
                "share_failed",
                error or "Failed to share payment instructions.",
                http_status=502,
            )

        return result


# Re-export channel constants for API docs / tests.
SHARE_CHANNELS = (
    GuestMessageChannel.BOOKING,
    GuestMessageChannel.WHATSAPP,
    GuestMessageChannel.EMAIL,
)
