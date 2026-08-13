from __future__ import annotations

import logging
import os
from datetime import timedelta

from django.utils import timezone

from apps.communications.canonical_store import create_with_canonical
from apps.communications.guest_compose import (
    HINT_CHECKIN_READY,
    HINT_ID_MISSING_SIDES,
    HINT_OPERATOR_CHECKIN_COMPLETE,
    render_checkin_automation_failed_message,
    render_checkin_partial_documents_message,
    render_checkin_ready_message,
    render_missing_id_sides_message,
)
from apps.core.timezone import property_local_now
from apps.communications.models import (
    GuestMessageChannel,
    GuestMessageDraft,
    GuestMessageIntent,
    GuestOutboundMessage,
    GuestOutboundMessageStatus,
)
from apps.reservations.document_expectations import expected_document_count, expected_document_slots
from apps.integrations.models import WhatsAppMessage
from apps.integrations.whatsapp.client import WhatsAppApiError, extract_outbound_wamid, send_text_message
from apps.integrations.whatsapp.integration_lookup import resolve_whatsapp_integration
from apps.reservations.document_intake_context import DocumentIntakeContext
from apps.reservations.document_intake_sides import find_missing_id_sides
from apps.reservations.guest_slots import is_unfilled_guest
from apps.reservations.models import DocumentIntakeJob, DocumentIntakeJobSource, Guest, Reservation

logger = logging.getLogger(__name__)

INCOMPLETE_REPLY_COOLDOWN = timedelta(hours=1)


def should_skip_duplicate_incomplete_reply(
    *,
    reservation: Reservation,
    job: DocumentIntakeJob,
) -> bool:
    """Avoid spamming the same missing-documents list when reconcile re-runs a stale batch."""
    last_draft = (
        GuestMessageDraft.objects.filter(
            reservation=reservation,
            hint=HINT_ID_MISSING_SIDES,
        )
        .order_by("-sent_at")
        .first()
    )
    if not last_draft or not last_draft.sent_at:
        return False
    if timezone.now() - last_draft.sent_at >= INCOMPLETE_REPLY_COOLDOWN:
        return False
    last_image_at = job.images.order_by("-created_at").values_list("created_at", flat=True).first()
    if last_image_at and last_image_at > last_draft.sent_at:
        return False
    return True


def document_apply_reply_enabled() -> bool:
    raw = os.getenv("WHATSAPP_DOCUMENT_APPLY_REPLY", "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


def guest_is_registered(guest: Guest) -> bool:
    if not is_unfilled_guest(guest):
        return True
    return guest.id_documents.exists()


def all_adult_guests_registered(reservation: Reservation) -> bool:
    slots = expected_document_slots(reservation)
    if not slots:
        return False
    return all(guest_is_registered(guest) for guest in slots)


def is_document_checkin_complete(reservation: Reservation) -> bool:
    """Adult ID photos complete (front/back as required) and guest rows filled."""
    if find_missing_id_sides(reservation):
        return False
    return all_adult_guests_registered(reservation)


def _normalize_draft_hint(hint: str) -> str:
    return " ".join((hint or "").strip().lower().split())


_CHECKIN_ACKNOWLEDGED_HINTS = frozenset(
    {
        HINT_CHECKIN_READY,
        HINT_OPERATOR_CHECKIN_COMPLETE,
    }
)


def checkin_ready_draft_sent_today(reservation: Reservation) -> bool:
    """Reception already sent check-in ready / operator complete today (property local day)."""
    now = property_local_now(reservation.property)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    hints = GuestMessageDraft.objects.filter(
        reservation=reservation,
        sent_at__gte=start_of_day,
    ).values_list("hint", flat=True)
    return any(_normalize_draft_hint(h) in _CHECKIN_ACKNOWLEDGED_HINTS for h in hints)


def is_whatsapp_autocheckin_waived(reservation: Reservation) -> bool:
    return reservation.whatsapp_autocheckin_waived_at is not None


def waive_whatsapp_autocheckin(reservation: Reservation) -> None:
    if reservation.whatsapp_autocheckin_waived_at is None:
        reservation.whatsapp_autocheckin_waived_at = timezone.now()
        reservation.save(update_fields=["whatsapp_autocheckin_waived_at", "updated_at"])


def is_guest_checkin_acknowledged(reservation: Reservation) -> bool:
    """Checked-in in DB, or reception already sent check-in complete today."""
    if reservation.status == Reservation.Status.CHECKED_IN:
        return True
    return checkin_ready_draft_sent_today(reservation)


def _send_whatsapp_text_reply(
    *,
    job: DocumentIntakeJob,
    body: str,
    intent: str,
    hint: str,
    mark_reply_sent: bool,
) -> dict:
    reservation = job.reservation
    wa_message = job.whatsapp_message
    wa_id = (wa_message.wa_id if wa_message else "").strip()
    if not wa_id:
        return {"status": "skipped", "reason": "no_wa_id"}

    if hint == HINT_ID_MISSING_SIDES and should_skip_duplicate_incomplete_reply(
        reservation=reservation,
        job=job,
    ):
        return {"status": "skipped", "reason": "duplicate_incomplete_reply"}

    integration_row, runtime = resolve_whatsapp_integration(reservation.tenant)
    if integration_row is None or runtime is None or not runtime.send_credentials_ok():
        return {"status": "skipped", "reason": "no_credentials"}

    try:
        response = send_text_message(
            phone_number_id=runtime.phone_number_id,
            access_token=runtime.access_token,
            to_wa_id=wa_id,
            body=body,
        )
    except WhatsAppApiError as exc:
        logger.warning("WhatsApp apply reply failed job_id=%s: %s", job.pk, exc)
        return {"status": "send_failed", "detail": str(exc)}

    outbound_wamid = extract_outbound_wamid(response)
    property_tenant_id = reservation.tenant_id
    if outbound_wamid:
        create_with_canonical(WhatsAppMessage,
            tenant_id=property_tenant_id,
            integration=integration_row,
            reservation=reservation,
            wamid=outbound_wamid,
            wa_id=wa_id,
            phone_number_id=runtime.phone_number_id,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            message_type="text",
            body=body,
            raw_payload=response,
        )

    draft = GuestMessageDraft.objects.create(
        tenant_id=property_tenant_id,
        reservation=reservation,
        intent=intent,
        hint=hint,
        language="",
        llm_body_text=body,
        final_body_text=body,
        channel=GuestMessageChannel.WHATSAPP,
        sent_at=timezone.now(),
    )
    create_with_canonical(GuestOutboundMessage,
        tenant_id=property_tenant_id,
        reservation=reservation,
        draft=draft,
        channel=GuestMessageChannel.WHATSAPP,
        body_text=body,
        status=GuestOutboundMessageStatus.SENT,
        to_phone=reservation.booker_phone or wa_id,
    )

    if mark_reply_sent:
        job.whatsapp_reply_sent = True
        job.save(update_fields=["whatsapp_reply_sent", "updated_at"])

    return {"status": "sent", "wamid": outbound_wamid}


def maybe_send_document_apply_whatsapp_reply(
    ctx: DocumentIntakeContext,
    *,
    applied: list,
) -> dict:
    job = ctx.job
    if not document_apply_reply_enabled():
        return {"status": "disabled"}
    if not applied:
        return {"status": "skipped", "reason": "nothing_applied"}
    if job.source != DocumentIntakeJobSource.WHATSAPP:
        return {"status": "skipped", "reason": "not_whatsapp_source"}
    if job.reservation_id is None:
        return {"status": "skipped", "reason": "no_reservation"}

    reservation = job.reservation
    from apps.integrations.whatsapp.guest_document_lifecycle import (
        check_guest_document_intake_automation,
    )

    allowed, reason = check_guest_document_intake_automation(
        reservation,
        block_documents_complete=False,
    )
    if not allowed:
        return {"status": "skipped", "reason": reason}

    missing_sides = find_missing_id_sides(reservation)
    if missing_sides:
        body = render_missing_id_sides_message(reservation, missing_sides)
        return _send_whatsapp_text_reply(
            job=job,
            body=body,
            intent=GuestMessageIntent.REPLY,
            hint=HINT_ID_MISSING_SIDES,
            mark_reply_sent=False,
        )

    if all_adult_guests_registered(reservation):
        if job.whatsapp_reply_sent:
            return {"status": "already_sent"}
        if DocumentIntakeJob.objects.filter(
            reservation_id=job.reservation_id,
            source=DocumentIntakeJobSource.WHATSAPP,
            whatsapp_reply_sent=True,
        ).exclude(pk=job.pk).exists():
            return {"status": "already_sent", "reason": "reservation_reply_sent"}
        body = render_checkin_ready_message(reservation)
        return _send_whatsapp_text_reply(
            job=job,
            body=body,
            intent=GuestMessageIntent.REPLY,
            hint="checkin ready",
            mark_reply_sent=True,
        )

    body = render_checkin_partial_documents_message(reservation)
    return _send_whatsapp_text_reply(
        job=job,
        body=body,
        intent=GuestMessageIntent.REPLY,
        hint="checkin partial documents",
        mark_reply_sent=False,
    )


def maybe_send_checkin_automation_failed_whatsapp_reply(job: DocumentIntakeJob) -> dict:
    if not document_apply_reply_enabled():
        return {"status": "disabled"}
    if job.source != DocumentIntakeJobSource.WHATSAPP:
        return {"status": "skipped", "reason": "not_whatsapp_source"}
    if job.reservation_id is None:
        return {"status": "skipped", "reason": "no_reservation"}
    if job.whatsapp_reply_sent:
        return {"status": "already_sent"}

    reservation = job.reservation
    from apps.integrations.whatsapp.guest_document_lifecycle import (
        check_guest_document_intake_automation,
    )

    allowed, reason = check_guest_document_intake_automation(
        reservation,
        block_documents_complete=False,
    )
    if not allowed:
        return {"status": "skipped", "reason": reason}

    body = render_checkin_automation_failed_message(reservation)
    return _send_whatsapp_text_reply(
        job=job,
        body=body,
        intent=GuestMessageIntent.REPLY,
        hint="checkin automation failed",
        mark_reply_sent=False,
    )
