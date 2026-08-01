"""Shared check-in, eVisitor submission, and guest notification after document apply."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from django.db import transaction

from apps.integrations.evisitor.eligibility import guest_requires_evisitor
from apps.integrations.evisitor.exceptions import (
    EvisitorApiError,
    EvisitorConfigError,
    EvisitorValidationError,
)
from apps.integrations.evisitor.metrics import record_checkin_auto
from apps.integrations.evisitor.service import submit_guest_checkin
from apps.integrations.evisitor.summary import evisitor_summary_for_reservation
from apps.integrations.models import IntegrationConfig
from apps.integrations.whatsapp.apply_reply import is_document_checkin_complete
from apps.integrations.whatsapp.guest_docs_awaiting_arrival import docs_awaiting_arrival_already_sent
from apps.integrations.whatsapp.integration_lookup import resolve_whatsapp_integration
from apps.integrations.whatsapp.runtime_config import WhatsAppRuntimeConfig
from apps.integrations.whatsapp.whatsapp_operator import operator_name_for_wa_id
from apps.integrations.whatsapp.whatsapp_operator_service import (
    _send_operator_text,
    notify_guest_operator_checkin_complete,
)
from apps.reservations.checkin import CheckInBlockedError, validate_reservation_check_in
from apps.reservations.models import DocumentIntakeJob, EvisitorGuestStatus, Reservation
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

_RECEPTION_EVISITOR_HTTP_TIMEOUT = 8.0
_SENT_STATUSES = frozenset(
    {
        EvisitorGuestStatus.SENT,
        "sent",
        "SENT",
    }
)


def mark_reservation_checked_in(
    reservation: Reservation,
    *,
    notify: bool = True,
) -> dict:
    if reservation.status == Reservation.Status.CHECKED_IN:
        return {"status": "already_checked_in"}

    tenant = reservation.tenant
    try:
        validate_reservation_check_in(reservation, tenant=tenant)
    except CheckInBlockedError as exc:
        return {"status": "blocked", "code": exc.code, "message": exc.message}

    old_status = reservation.status
    reservation.status = Reservation.Status.CHECKED_IN
    reservation.save(update_fields=["status", "updated_at"])

    from apps.integrations.whatsapp.apply_reply import waive_whatsapp_autocheckin

    waive_whatsapp_autocheckin(reservation)

    if notify:
        from apps.core.tasks import notify_reservation_status_changed

        notify_reservation_status_changed.delay(
            reservation.pk,
            old_status,
            reservation.status,
        )
    return {"status": "checked_in", "old_status": old_status}


def submit_evisitor_for_reservation(
    reservation: Reservation,
    *,
    time_stay_from: str | None = None,
    http_timeout: float | None = None,
    correlation_id: str | None = None,
) -> list[dict]:
    results: list[dict] = []
    guests = list(reservation.guests.all())
    for guest in guests:
        guest_name = guest.name or f"{guest.first_name} {guest.last_name}".strip()
        if not guest_requires_evisitor(guest, reference_date=reservation.check_in):
            results.append(
                {
                    "guest_id": guest.pk,
                    "guest_name": guest_name,
                    "status": "not_required",
                }
            )
            continue
        try:
            submission = submit_guest_checkin(
                guest,
                time_stay_from=time_stay_from,
                http_timeout=http_timeout,
                correlation_id=correlation_id,
            )
            results.append(
                {
                    "guest_id": guest.pk,
                    "guest_name": guest_name,
                    "status": submission.status,
                    "registration_id": str(submission.registration_id),
                }
            )
        except EvisitorValidationError as exc:
            results.append(
                {
                    "guest_id": guest.pk,
                    "guest_name": guest_name,
                    "status": "validation_failed",
                    "message": str(exc),
                    "field_errors": exc.field_errors or {},
                }
            )
        except EvisitorConfigError as exc:
            results.append(
                {
                    "guest_id": guest.pk,
                    "guest_name": guest_name,
                    "status": "config_error",
                    "message": str(exc),
                }
            )
        except EvisitorApiError as exc:
            results.append(
                {
                    "guest_id": guest.pk,
                    "guest_name": guest_name,
                    "status": "api_error",
                    "message": str(exc),
                }
            )
        except Exception as exc:  # noqa: BLE001 — best-effort; never fail check-in
            logger.exception(
                "evisitor.submit_unexpected reservation_id=%s guest_id=%s correlation_id=%s",
                reservation.pk,
                guest.pk,
                correlation_id,
            )
            results.append(
                {
                    "guest_id": guest.pk,
                    "guest_name": guest_name,
                    "status": "api_error",
                    "message": str(exc) or "unexpected_error",
                }
            )
    return results


def _aggregate_evisitor_checkin(results: list[dict]) -> dict:
    submitted = 0
    skipped = 0
    failed = 0
    validation_failed = 0
    failed_guests: list[dict] = []

    for row in results:
        status = str(row.get("status") or "")
        if status in _SENT_STATUSES:
            submitted += 1
            continue
        if status == "not_required":
            skipped += 1
            continue
        if status == "validation_failed":
            validation_failed += 1
            failed_guests.append(
                {
                    "guest_id": row.get("guest_id"),
                    "guest_name": row.get("guest_name"),
                    "status": status,
                    "message": row.get("message") or "",
                    "field_errors": row.get("field_errors") or {},
                }
            )
            continue
        failed += 1
        failed_guests.append(
            {
                "guest_id": row.get("guest_id"),
                "guest_name": row.get("guest_name"),
                "status": status,
                "message": row.get("message") or "",
            }
        )

    eligible = submitted + failed + validation_failed
    if eligible == 0:
        overall = "not_required"
    elif submitted == eligible:
        overall = "complete"
    elif submitted == 0:
        overall = "none"
    else:
        overall = "partial"

    return {
        "overall": overall,
        "submitted": submitted,
        "skipped": skipped,
        "failed": failed,
        "validation_failed": validation_failed,
        "failed_guests": failed_guests,
    }


def perform_reception_checkin(
    reservation: Reservation,
    *,
    tenant: Tenant | None = None,
    time_stay_from: str | None = None,
    correlation_id: str | None = None,
) -> dict:
    """Reception check-in: commit local status, then best-effort eVisitor.

    Does not re-submit eVisitor when already checked in (retry via evisitor-submit).
    """
    cid = (correlation_id or "").strip() or str(uuid.uuid4())
    logger.info(
        "reception.checkin.start",
        extra={
            "event": "reception.checkin.start",
            "correlation_id": cid,
            "reservation_id": reservation.pk,
        },
    )

    if reservation.status == Reservation.Status.CHECKED_IN:
        return {
            "status": "already_checked_in",
            "checkin": {"status": "already_checked_in"},
            "evisitor": None,
            "correlation_id": cid,
        }

    check_tenant = tenant or reservation.tenant
    try:
        validate_reservation_check_in(reservation, tenant=check_tenant)
    except CheckInBlockedError:
        raise

    # Commit check-in before any eVisitor HTTP so timeouts cannot roll it back.
    with transaction.atomic():
        checkin_result = mark_reservation_checked_in(reservation, notify=False)

    if checkin_result.get("status") == "blocked":
        raise CheckInBlockedError(
            str(checkin_result.get("code") or "blocked"),
            str(checkin_result.get("message") or "Check-in nije moguć."),
        )

    if checkin_result.get("status") == "already_checked_in":
        return {
            "status": "already_checked_in",
            "checkin": checkin_result,
            "evisitor": None,
            "correlation_id": cid,
        }

    reservation.refresh_from_db()
    guests = list(reservation.guests.all())
    eligible = [
        g
        for g in guests
        if guest_requires_evisitor(g, reference_date=reservation.check_in)
    ]

    # DoD: no eVisitor service call when nobody is eligible.
    if not eligible:
        evisitor_summary = {
            "overall": "not_required",
            "submitted": 0,
            "skipped": len(guests),
            "failed": 0,
            "validation_failed": 0,
            "failed_guests": [],
        }
        record_checkin_auto(result="not_required")
        logger.info(
            "reception.checkin.done",
            extra={
                "event": "reception.checkin.done",
                "correlation_id": cid,
                "reservation_id": reservation.pk,
                "evisitor_overall": "not_required",
            },
        )
        return {
            "status": "checked_in",
            "checkin": checkin_result,
            "evisitor": evisitor_summary,
            "correlation_id": cid,
        }

    evisitor_results = submit_evisitor_for_reservation(
        reservation,
        time_stay_from=time_stay_from,
        http_timeout=_RECEPTION_EVISITOR_HTTP_TIMEOUT,
        correlation_id=cid,
    )
    evisitor_summary = _aggregate_evisitor_checkin(evisitor_results)
    record_checkin_auto(result=str(evisitor_summary["overall"]))
    logger.info(
        "reception.checkin.done",
        extra={
            "event": "reception.checkin.done",
            "correlation_id": cid,
            "reservation_id": reservation.pk,
            "evisitor_overall": evisitor_summary["overall"],
            "evisitor_submitted": evisitor_summary["submitted"],
            "evisitor_failed": evisitor_summary["failed"],
            "evisitor_validation_failed": evisitor_summary["validation_failed"],
        },
    )
    return {
        "status": "checked_in",
        "checkin": checkin_result,
        "evisitor": evisitor_summary,
        "correlation_id": cid,
    }


def complete_guest_checkin_after_apply(
    *,
    job: DocumentIntakeJob,
    reservation: Reservation,
    applied: list[dict[str, Any]],
    time_stay_from: str | None = None,
) -> dict:
    """Mark checked-in, submit eVisitor, notify guest (idempotent if already checked-in)."""
    if reservation.status == Reservation.Status.CHECKED_IN:
        reservation.refresh_from_db()
        guest_notify = notify_guest_operator_checkin_complete(reservation)
        return {
            "status": "already_checked_in",
            "job_id": job.pk,
            "reservation_id": reservation.pk,
            "applied": applied,
            "checkin": {"status": "already_checked_in"},
            "guest_notify": guest_notify,
        }

    checkin_result = mark_reservation_checked_in(reservation)
    if checkin_result.get("status") == "blocked":
        logger.warning(
            "Deferred guest check-in blocked reservation_id=%s code=%s",
            reservation.pk,
            checkin_result.get("code"),
        )
        from apps.core.tasks import notify_guest_message_inbound

        notify_guest_message_inbound.delay(
            reservation.pk,
            channel="whatsapp",
            body_preview="Check-in blokiran — dokumenti spremljeni, recepcija provjerava",
        )
        return {
            "status": "checkin_blocked",
            "job_id": job.pk,
            "reservation_id": reservation.pk,
            "applied": applied,
            "checkin": checkin_result,
        }

    reservation.refresh_from_db()
    evisitor_results = submit_evisitor_for_reservation(reservation, time_stay_from=time_stay_from)
    reservation.refresh_from_db()
    guest_notify = notify_guest_operator_checkin_complete(reservation)

    return {
        "status": "completed",
        "job_id": job.pk,
        "reservation_id": reservation.pk,
        "applied": applied,
        "checkin": checkin_result,
        "evisitor": evisitor_results,
        "evisitor_summary": evisitor_summary_for_reservation(reservation),
        "guest_notify": guest_notify,
    }


def perform_arrival_confirmed_checkin(
    reservation: Reservation,
    *,
    time_stay_from: str | None,
    operator_wa_id: str,
    confirmed_arrival_at=None,
    integration_row: IntegrationConfig | None = None,
    runtime: WhatsAppRuntimeConfig | None = None,
) -> dict:
    """Toni-confirmed arrival: check-in + eVisitor; skip guest complete if docs-awaiting already sent."""
    if integration_row is None or runtime is None:
        integration_row, runtime = resolve_whatsapp_integration(reservation.tenant)

    checkin_result = mark_reservation_checked_in(reservation)
    if checkin_result.get("status") == "blocked":
        message = checkin_result.get("message") or "Check-in nije moguć."
        if integration_row is not None and runtime is not None:
            _send_operator_text(
                integration_row=integration_row,
                runtime=runtime,
                operator_wa_id=operator_wa_id,
                body=f"Check-in nije moguć.\n{message}",
                reservation=reservation,
            )
        return {
            "status": "checkin_blocked",
            "reservation_id": reservation.pk,
            "checkin": checkin_result,
        }

    reservation.refresh_from_db()
    evisitor_results = submit_evisitor_for_reservation(reservation, time_stay_from=time_stay_from)
    reservation.refresh_from_db()

    guest_notify: dict
    if docs_awaiting_arrival_already_sent(reservation):
        guest_notify = {"channel": "none", "status": "already_sent", "reason": "docs_awaiting_arrival"}
    else:
        guest_notify = notify_guest_operator_checkin_complete(reservation)

    from apps.integrations.whatsapp.operator_job_complete import _format_operator_success_message

    operator_name = operator_name_for_wa_id(tenant_id=reservation.tenant_id, wa_id=operator_wa_id) or "Operator"
    success_body = _format_operator_success_message(
        reservation=reservation,
        operator_name=operator_name,
        applied=[],
        guest_notify=guest_notify,
        evisitor_results=evisitor_results,
    )
    if not is_document_checkin_complete(reservation):
        success_body += "\n\nCheck-in OK — dokumenti nisu kompletni, slikaj dokumente ručno."
    failed_evisitor = [
        r
        for r in evisitor_results
        if str(r.get("status") or "") not in {"not_required", "sent", "SENT"}
    ]
    if failed_evisitor:
        success_body += "\neVisitor: nije sve uspjelo — provjeri u recepciji."

    operator_notify: dict = {"status": "skipped", "reason": "no_integration"}
    if integration_row is not None and runtime is not None:
        operator_notify = _send_operator_text(
            integration_row=integration_row,
            runtime=runtime,
            operator_wa_id=operator_wa_id,
            body=success_body,
            reservation=reservation,
        )

    return {
        "status": "completed",
        "reservation_id": reservation.pk,
        "checkin": checkin_result,
        "evisitor": evisitor_results,
        "evisitor_summary": evisitor_summary_for_reservation(reservation),
        "guest_notify": guest_notify,
        "operator_whatsapp": operator_notify,
        "confirmed_arrival_at": confirmed_arrival_at.isoformat() if confirmed_arrival_at else None,
    }
