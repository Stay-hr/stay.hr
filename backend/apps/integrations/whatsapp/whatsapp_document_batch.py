from __future__ import annotations

import logging
import mimetypes
import re
import uuid
import zlib

from celery import shared_task
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.db import connection, transaction
from django.utils import timezone

from apps.communications.canonical_store import create_with_canonical, link_raw_reservation
from apps.communications.guest_compose import (
    HINT_DOCUMENTS_BATCH_ADDITIONAL_PHOTO,
    HINT_DOCUMENTS_BATCH_COMPLETE_REPROMPT,
    HINT_ID_MISSING_SIDES,
    documents_batch_confirm_button_labels,
    render_document_intake_incomplete_message,
    render_documents_batch_additional_photo_message,
    render_documents_batch_complete_reprompt_message,
    render_documents_batch_confirm_message,
)
from apps.communications.models import GuestMessageIntent
from apps.communications.whatsapp_autocheckin_tasks import mark_autocheckin_engaged
from apps.integrations.models import WhatsAppMessage
from apps.integrations.whatsapp.client import (
    WhatsAppApiError,
    extract_outbound_wamid,
    send_interactive_button_message,
)
from apps.integrations.whatsapp.integration_lookup import resolve_whatsapp_integration
from apps.integrations.whatsapp.media_download import (
    WhatsAppMediaError,
    extract_media_from_message,
    fetch_whatsapp_media,
)
from apps.integrations.whatsapp.reservation_lookup import find_reservation_for_wa_id
from apps.reservations.models import (
    DocumentIntakeImage,
    DocumentIntakeJob,
    DocumentIntakeJobSource,
    DocumentIntakeJobStatus,
    WhatsAppDocumentBatchSession,
    WhatsAppDocumentBatchStatus,
)

logger = logging.getLogger(__name__)

QUIET_SECONDS = 10
AFTER_NO_SECONDS = 30
CONFIRM_TIMEOUT_SECONDS = 120
DUPLICATE_PROMPT_GUARD_SECONDS = 30
_TIMER_CACHE_PREFIX = "wa-doc-timer"
_TIMER_CACHE_TTL = 3600

_MEDIA_MESSAGE_TYPES = frozenset({"image", "document"})


def _extension_for_mime(mime_type: str) -> str:
    ext = mimetypes.guess_extension(mime_type.split(";")[0].strip()) or ""
    if ext == ".jpe":
        ext = ".jpg"
    if ext:
        return ext
    if mime_type.startswith("image/"):
        return ".jpg"
    if mime_type == "application/pdf":
        return ".pdf"
    return ".bin"


_ACTIVE_STATUSES = frozenset(
    {
        WhatsAppDocumentBatchStatus.COLLECTING,
        WhatsAppDocumentBatchStatus.PROCESSING,
        WhatsAppDocumentBatchStatus.AWAITING_CONFIRM,
        WhatsAppDocumentBatchStatus.AFTER_NO,
    }
)

_DOCS_ALL_YES_IDS = frozenset({"docs_all_yes"})
_DOCS_ALL_NO_IDS = frozenset({"docs_all_no"})
_DOCS_ALL_YES_TEXTS = frozenset({"da", "yes", "ja", "si", "sí", "oui"})
_DOCS_ALL_NO_TEXTS = frozenset({"ne", "no", "nein"})


def _normalize_reply_text(text: str) -> str:
    lowered = (text or "").strip().lower()
    lowered = re.sub(r"[_\-]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def is_documents_all_yes_reply(*, button_id: str = "", text: str = "") -> bool:
    if (button_id or "").strip() in _DOCS_ALL_YES_IDS:
        return True
    return _normalize_reply_text(text) in _DOCS_ALL_YES_TEXTS


def is_documents_all_no_reply(*, button_id: str = "", text: str = "") -> bool:
    if (button_id or "").strip() in _DOCS_ALL_NO_IDS:
        return True
    return _normalize_reply_text(text) in _DOCS_ALL_NO_TEXTS


def inbound_interactive_button_id(row: WhatsAppMessage) -> str:
    payload = row.raw_payload or {}
    message_type = (row.message_type or payload.get("type") or "").strip()
    if message_type == "interactive":
        interactive = payload.get("interactive") or {}
        if str(interactive.get("type") or "").strip() == "button_reply":
            return str((interactive.get("button_reply") or {}).get("id") or "").strip()
    if message_type == "button":
        return str((payload.get("button") or {}).get("payload") or "").strip()
    return ""


def _timer_cache_key(session_id: int, suffix: str) -> str:
    return f"{_TIMER_CACHE_PREFIX}:{session_id}:{suffix}"


def _revoke_scheduled(session_id: int, suffix: str) -> None:
    from config.celery import app

    cache_key = _timer_cache_key(session_id, suffix)
    task_id = cache.get(cache_key)
    if not task_id:
        return
    app.control.revoke(task_id, terminate=False)
    cache.delete(cache_key)


def _revoke_all_timers(session_id: int) -> None:
    for suffix in ("quiet", "timeout", "after-no"):
        _revoke_scheduled(session_id, suffix)


def _schedule_task(*, task, session_id: int, countdown: int, suffix: str):
    _revoke_scheduled(session_id, suffix)
    task_id = f"wa-doc-{suffix}-{session_id}-{uuid.uuid4().hex[:12]}"
    cache.set(_timer_cache_key(session_id, suffix), task_id, timeout=_TIMER_CACHE_TTL)
    return task.apply_async(args=[session_id], countdown=countdown, task_id=task_id)


def _cancel_confirm_timers(session_id: int) -> None:
    """Cancel confirm/after-no timers when new photos arrive; quiet is rescheduled separately."""
    _revoke_scheduled(session_id, "timeout")
    _revoke_scheduled(session_id, "after-no")


def _pg_advisory_xact_lock_guest_batch(tenant_id: int, reservation_id: int) -> None:
    """Serialize guest document collect/finalize per tenant+reservation."""
    key = zlib.crc32(f"wa-guest-batch:{tenant_id}:{reservation_id}".encode()) & 0x7FFFFFFF
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [key])


def get_active_document_batch_session(*, reservation_id: int):
    """Public wrapper for active guest document batch session (if any)."""
    return _get_active_session(reservation_id=reservation_id)


def handle_autocheckin_during_document_batch(
    *,
    reservation,
    integration_row,
    runtime,
    row: WhatsAppMessage,
) -> dict | None:
    """If a document batch is active, avoid autocheck-in loops; re-prompt or skip."""
    session = get_active_document_batch_session(reservation_id=reservation.pk)
    if session is None:
        return None

    if session.status == WhatsAppDocumentBatchStatus.AWAITING_CONFIRM:
        send_result = _prompt_and_await_confirm(session)
        return {
            "status": "batch_awaiting_confirm",
            "session_id": session.pk,
            "send": send_result,
        }

    if session.status in {
        WhatsAppDocumentBatchStatus.COLLECTING,
        WhatsAppDocumentBatchStatus.AFTER_NO,
    }:
        return {"status": "skipped", "reason": "batch_collecting", "session_id": session.pk}

    if session.status == WhatsAppDocumentBatchStatus.PROCESSING:
        return {"status": "skipped", "reason": "batch_processing", "session_id": session.pk}

    return None


def _get_active_session(*, reservation_id: int, for_update: bool = False):
    qs = WhatsAppDocumentBatchSession.objects.select_related(
        "job",
        "reservation",
        "reservation__property",
        "tenant",
    ).filter(
        reservation_id=reservation_id,
        status__in=_ACTIVE_STATUSES,
    )
    if for_update:
        qs = qs.select_for_update()
    return qs.order_by("-created_at", "id").first()


def _message_already_in_job(job: DocumentIntakeJob, message_id: int) -> bool:
    prefix = f"wa_{message_id}"
    for img in job.images.only("image"):
        name = (img.image.name or "").rsplit("/", 1)[-1]
        if name.startswith(prefix):
            return True
    return False


def _session_accepts_docs_reply(session: WhatsAppDocumentBatchSession) -> bool:
    if session.status == WhatsAppDocumentBatchStatus.AWAITING_CONFIRM:
        return True
    if session.status == WhatsAppDocumentBatchStatus.COLLECTING and (session.prompt_count or 0) > 0:
        return True
    return False


def _send_batch_text_ack(
    *,
    session: WhatsAppDocumentBatchSession,
    body: str,
    hint: str,
) -> dict:
    from apps.integrations.whatsapp.apply_reply import _send_whatsapp_text_reply

    return _send_whatsapp_text_reply(
        job=session.job,
        body=body,
        intent=GuestMessageIntent.REPLY,
        hint=hint,
        mark_reply_sent=False,
    )


def _send_batch_additional_photo_ack(session: WhatsAppDocumentBatchSession) -> dict:
    body = render_documents_batch_additional_photo_message(session.reservation)
    return _send_batch_text_ack(
        session=session,
        body=body,
        hint=HINT_DOCUMENTS_BATCH_ADDITIONAL_PHOTO,
    )


def assess_batch_after_quiet(session: WhatsAppDocumentBatchSession) -> dict:
    """OCR preview after quiet period; notify guest then re-prompt Ja/Ne."""
    with transaction.atomic():
        locked = (
            WhatsAppDocumentBatchSession.objects.select_for_update()
            .select_related("reservation", "job", "tenant", "reservation__property")
            .filter(pk=session.pk)
            .first()
        )
        if locked is None:
            return {"status": "missing"}
        if locked.status != WhatsAppDocumentBatchStatus.COLLECTING:
            return {"status": "skipped", "reason": "not_collecting"}
        locked.status = WhatsAppDocumentBatchStatus.PROCESSING
        locked.save(update_fields=["status", "updated_at"])
        session = locked

    reservation = session.reservation
    from apps.integrations.whatsapp.guest_document_lifecycle import (
        check_guest_document_intake_automation,
    )

    allowed, reason = check_guest_document_intake_automation(reservation)
    if not allowed:
        session.status = WhatsAppDocumentBatchStatus.COLLECTING
        session.save(update_fields=["status", "updated_at"])
        return {"status": "skipped", "reason": reason, "job_id": session.job_id}

    job = session.job
    integration_row, runtime = resolve_whatsapp_integration(reservation.tenant)
    if integration_row is None or runtime is None:
        session.status = WhatsAppDocumentBatchStatus.COLLECTING
        session.save(update_fields=["status", "updated_at"])
        return {"status": "skipped", "reason": "no_integration"}

    from apps.reservations.document_intake_audit import rematch_and_audit_job
    from apps.reservations.document_intake_completeness import evaluate_completeness
    from apps.reservations.document_intake_context import DocumentIntakeContext
    from apps.reservations.document_intake_service import process_document_intake_job

    ctx = DocumentIntakeContext.from_job(job)
    process_document_intake_job(ctx)
    job.refresh_from_db()
    ctx = DocumentIntakeContext.from_job(job)

    if job.status == DocumentIntakeJobStatus.FAILED:
        session.status = WhatsAppDocumentBatchStatus.COLLECTING
        session.save(update_fields=["status", "updated_at"])
        return {"status": "ocr_failed", "job_id": job.pk}

    session.refresh_from_db()
    if session.status != WhatsAppDocumentBatchStatus.PROCESSING:
        return {"status": "skipped", "reason": "session_interrupted", "job_id": job.pk}

    matches = rematch_and_audit_job(ctx)
    job.refresh_from_db()

    from apps.reservations.document_intake_apply_gate import safe_to_partial_apply
    from apps.reservations.document_intake_service import apply_document_intake_job

    if safe_to_partial_apply(job, matches):
        apply_document_intake_job(ctx, whatsapp_reply=False, allow_partial=True)
        job.refresh_from_db()

    persons = (job.ocr_result or {}).get("persons") or []
    images = list(job.images.order_by("sort_order", "id"))
    completeness = evaluate_completeness(
        reservation=reservation,
        persons=persons,
        matches=matches,
        images=images,
    )

    if completeness.is_complete:
        body = render_documents_batch_complete_reprompt_message(reservation)
        preview_reply = _send_batch_text_ack(
            session=session,
            body=body,
            hint=HINT_DOCUMENTS_BATCH_COMPLETE_REPROMPT,
        )
        preview = "complete"
        send_result = _prompt_and_await_confirm(session)
    else:
        body = render_document_intake_incomplete_message(
            reservation,
            completeness,
            image_count=len(images),
        )
        preview_reply = _send_batch_text_ack(
            session=session,
            body=body,
            hint=HINT_ID_MISSING_SIDES,
        )
        preview = "incomplete"
        session.status = WhatsAppDocumentBatchStatus.COLLECTING
        session.save(update_fields=["status", "updated_at"])
        send_result = {"status": "skipped", "reason": "awaiting_more_documents"}

    return {
        "status": "assessed",
        "preview": preview,
        "preview_reply": preview_reply,
        "prompt": send_result,
        "job_id": job.pk,
    }


def _schedule_quiet_timer(session: WhatsAppDocumentBatchSession) -> None:
    _schedule_task(
        task=document_batch_quiet_elapsed,
        session_id=session.pk,
        countdown=QUIET_SECONDS,
        suffix="quiet",
    )


def _schedule_confirm_timeout(session: WhatsAppDocumentBatchSession) -> None:
    _schedule_task(
        task=document_batch_confirm_timeout,
        session_id=session.pk,
        countdown=CONFIRM_TIMEOUT_SECONDS,
        suffix="timeout",
    )


def _schedule_after_no_quiet(session: WhatsAppDocumentBatchSession) -> None:
    _schedule_task(
        task=document_batch_after_no_quiet,
        session_id=session.pk,
        countdown=AFTER_NO_SECONDS,
        suffix="after-no",
    )


def _send_batch_confirm_prompt(session: WhatsAppDocumentBatchSession) -> dict:
    reservation = session.reservation
    integration_row, runtime = resolve_whatsapp_integration(reservation.tenant)
    if integration_row is None or runtime is None or not runtime.send_credentials_ok():
        return {"status": "skipped", "reason": "no_credentials"}

    body = render_documents_batch_confirm_message(reservation)
    yes_label, no_label = documents_batch_confirm_button_labels(reservation)
    try:
        response = send_interactive_button_message(
            phone_number_id=runtime.phone_number_id,
            access_token=runtime.access_token,
            to_wa_id=session.wa_id,
            body=body,
            buttons=[("docs_all_yes", yes_label), ("docs_all_no", no_label)],
        )
    except WhatsAppApiError as exc:
        logger.warning("WhatsApp batch confirm failed session_id=%s: %s", session.pk, exc)
        return {"status": "send_failed", "detail": str(exc)}

    outbound_wamid = extract_outbound_wamid(response)
    if outbound_wamid:
        create_with_canonical(WhatsAppMessage,
            tenant_id=session.tenant_id,
            integration=integration_row,
            reservation=reservation,
            wamid=outbound_wamid,
            wa_id=session.wa_id,
            phone_number_id=runtime.phone_number_id,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            message_type="interactive",
            body=body,
            raw_payload=response,
        )

    return {"status": "sent", "wamid": outbound_wamid}


def _prompt_and_await_confirm(session: WhatsAppDocumentBatchSession) -> dict:
    if (
        session.prompt_count
        and session.prompt_count > 0
        and session.prompt_sent_at is not None
        and (timezone.now() - session.prompt_sent_at).total_seconds() < DUPLICATE_PROMPT_GUARD_SECONDS
    ):
        return {"status": "skipped", "reason": "duplicate_prompt_guard"}

    send_result = _send_batch_confirm_prompt(session)
    now = timezone.now()
    session.status = WhatsAppDocumentBatchStatus.AWAITING_CONFIRM
    session.prompt_sent_at = now
    session.prompt_count = (session.prompt_count or 0) + 1
    session.after_no_at = None
    session.save(
        update_fields=[
            "status",
            "prompt_sent_at",
            "prompt_count",
            "after_no_at",
            "updated_at",
        ]
    )
    _schedule_confirm_timeout(session)
    return send_result


def _run_finalize(session: WhatsAppDocumentBatchSession) -> dict:
    _revoke_all_timers(session.pk)

    if session.status in (
        WhatsAppDocumentBatchStatus.PROCESSING,
        WhatsAppDocumentBatchStatus.DONE,
    ):
        return {"status": "already_finalized", "session_id": session.pk}

    reservation = session.reservation
    integration_row, runtime = resolve_whatsapp_integration(reservation.tenant)
    if integration_row is None or runtime is None:
        return {"status": "skipped", "reason": "no_integration"}

    session.status = WhatsAppDocumentBatchStatus.PROCESSING
    session.save(update_fields=["status", "updated_at"])

    from apps.integrations.whatsapp.document_intake_finalize import finalize_document_intake_job
    from apps.reservations.document_intake_context import DocumentIntakeContext

    result = finalize_document_intake_job(
        DocumentIntakeContext.from_job(session.job),
        channel="guest",
        wa_id=session.wa_id,
        integration_row=integration_row,
        runtime=runtime,
        session=session,
    )

    finalize_status = result.get("status")
    if finalize_status not in {"incomplete", "ocr_failed"}:
        from apps.core.tasks import notify_guest_message_inbound

        notify_guest_message_inbound.delay(
            session.reservation_id,
            channel="whatsapp",
            body_preview="Dokumenti primljeni — pregledaj OCR",
        )
        if session.status != WhatsAppDocumentBatchStatus.DONE:
            session.status = WhatsAppDocumentBatchStatus.DONE
            session.save(update_fields=["status", "updated_at"])

    return {
        "status": finalize_status or "finalized",
        "session_id": session.pk,
        "job_id": session.job_id,
        "finalize": result,
    }


@shared_task
def on_whatsapp_document_received(message_id: int) -> dict:
    row = (
        WhatsAppMessage.objects.select_related("integration", "tenant", "reservation")
        .filter(pk=message_id, direction=WhatsAppMessage.Direction.INBOUND)
        .first()
    )
    if row is None:
        return {"status": "missing"}

    if row.message_type not in _MEDIA_MESSAGE_TYPES:
        return {"status": "skipped", "reason": "not_media"}

    media_id, mime_type, caption = extract_media_from_message(row.raw_payload or {})
    if not media_id:
        return {"status": "skipped", "reason": "no_media_id"}

    if row.reservation_id is None:
        reservation = find_reservation_for_wa_id(tenant_id=row.tenant_id, wa_id=row.wa_id)
        if reservation is not None:
            link_raw_reservation(row, reservation)
    else:
        reservation = row.reservation

    if reservation is None:
        return {"status": "skipped", "reason": "no_reservation"}

    from apps.integrations.whatsapp.guest_document_lifecycle import (
        check_guest_document_intake_automation,
    )

    allowed, reason = check_guest_document_intake_automation(reservation)
    if not allowed:
        return {"status": "skipped", "reason": reason}

    from apps.integrations.whatsapp.autocheckin_maintenance import (
        send_autocheckin_maintenance_reply,
        whatsapp_autocheckin_maintenance_enabled,
    )

    if whatsapp_autocheckin_maintenance_enabled():
        integration_row, runtime = resolve_whatsapp_integration(reservation.tenant)
        if integration_row is not None and runtime is not None:
            return send_autocheckin_maintenance_reply(
                row=row,
                integration_row=integration_row,
                runtime=runtime,
                reservation=reservation,
            )
        return {"status": "maintenance", "reason": "no_integration"}

    integration_row, runtime = resolve_whatsapp_integration(reservation.tenant)
    if integration_row is None or runtime is None:
        return {"status": "skipped", "reason": "no_integration"}

    with transaction.atomic():
        _pg_advisory_xact_lock_guest_batch(row.tenant_id, reservation.pk)
        session = _get_active_session(reservation_id=reservation.pk, for_update=True)
        if session is not None and _message_already_in_job(session.job, row.pk):
            return {"status": "duplicate", "session_id": session.pk, "job_id": session.job_id}

        if session is None:
            intake_tenant_id = reservation.tenant_id
            job = DocumentIntakeJob.objects.create(
                tenant_id=intake_tenant_id,
                reservation=reservation,
                whatsapp_message=row,
                source=DocumentIntakeJobSource.WHATSAPP,
                status=DocumentIntakeJobStatus.QUEUED,
                device_id="whatsapp",
            )
            session = WhatsAppDocumentBatchSession.objects.create(
                tenant_id=intake_tenant_id,
                reservation=reservation,
                job=job,
                wa_id=row.wa_id,
                status=WhatsAppDocumentBatchStatus.COLLECTING,
            )
            mark_autocheckin_engaged(reservation)
        elif session.job.whatsapp_message_id is None:
            session.job.whatsapp_message = row
            session.job.save(update_fields=["whatsapp_message", "updated_at"])

    try:
        content, downloaded_mime = fetch_whatsapp_media(
            media_id=media_id,
            access_token=runtime.access_token,
        )
    except WhatsAppMediaError as exc:
        logger.warning("WhatsApp media download failed message_id=%s: %s", row.pk, exc)
        return {"status": "download_failed", "detail": str(exc)}

    mime = downloaded_mime or mime_type
    filename = f"wa_{row.pk}{_extension_for_mime(mime)}"
    now = timezone.now()
    was_awaiting_confirm = False

    with transaction.atomic():
        _pg_advisory_xact_lock_guest_batch(row.tenant_id, reservation.pk)
        session = _get_active_session(reservation_id=reservation.pk, for_update=True)
        if session is None:
            return {"status": "skipped", "reason": "no_session"}
        if _message_already_in_job(session.job, row.pk):
            return {"status": "duplicate", "session_id": session.pk, "job_id": session.job_id}

        was_awaiting_confirm = session.status == WhatsAppDocumentBatchStatus.AWAITING_CONFIRM

        sort_order = session.job.images.count()
        DocumentIntakeImage.objects.create(
            tenant_id=reservation.tenant_id,
            job=session.job,
            image=ContentFile(content, name=filename),
            sort_order=sort_order,
        )
        if caption and not (row.body or "").strip():
            row.body = caption
            row.save(update_fields=["body"])

        session.last_media_at = now
        session.status = WhatsAppDocumentBatchStatus.COLLECTING
        session.after_no_at = None
        update_fields = ["last_media_at", "status", "after_no_at", "updated_at"]
        if was_awaiting_confirm:
            session.confirm_interrupted_at = now
            update_fields.append("confirm_interrupted_at")
        session.save(update_fields=update_fields)

    _cancel_confirm_timers(session.pk)
    _schedule_quiet_timer(session)

    ack_result: dict | None = None
    if was_awaiting_confirm:
        ack_result = _send_batch_additional_photo_ack(session)

    result = {
        "status": "collected",
        "session_id": session.pk,
        "job_id": session.job_id,
        "image_count": session.job.images.count(),
    }
    if was_awaiting_confirm:
        result["confirm_interrupted"] = True
        result["ack"] = ack_result
    return result


@shared_task
def document_batch_quiet_elapsed(session_id: int) -> dict:
    session = (
        WhatsAppDocumentBatchSession.objects.select_related("reservation", "job", "tenant")
        .filter(pk=session_id)
        .first()
    )
    if session is None:
        return {"status": "missing"}

    if session.status != WhatsAppDocumentBatchStatus.COLLECTING:
        return {"status": "skipped", "reason": "not_collecting"}

    if session.last_media_at is not None:
        elapsed = (timezone.now() - session.last_media_at).total_seconds()
        if elapsed < QUIET_SECONDS:
            remaining = max(1, int(QUIET_SECONDS - elapsed))
            _schedule_task(
                task=document_batch_quiet_elapsed,
                session_id=session.pk,
                countdown=remaining,
                suffix="quiet",
            )
            return {"status": "rescheduled", "countdown": remaining}

    if not session.job.images.exists():
        return {"status": "skipped", "reason": "no_images"}

    return assess_batch_after_quiet(session)


@shared_task
def document_batch_confirm_timeout(session_id: int) -> dict:
    session = (
        WhatsAppDocumentBatchSession.objects.select_related("reservation", "job", "tenant")
        .filter(pk=session_id)
        .first()
    )
    if session is None:
        return {"status": "missing"}

    if session.status != WhatsAppDocumentBatchStatus.AWAITING_CONFIRM:
        return {"status": "skipped", "reason": "not_awaiting_confirm"}

    return _run_finalize(session)


@shared_task
def document_batch_after_no_quiet(session_id: int) -> dict:
    session = (
        WhatsAppDocumentBatchSession.objects.select_related("reservation", "job", "tenant")
        .filter(pk=session_id)
        .first()
    )
    if session is None:
        return {"status": "missing"}

    if session.status != WhatsAppDocumentBatchStatus.AFTER_NO:
        return {"status": "skipped", "reason": "not_after_no"}

    if session.last_media_at and session.after_no_at and session.last_media_at > session.after_no_at:
        return {"status": "skipped", "reason": "new_media"}

    send_result = _prompt_and_await_confirm(session)
    return {"status": "prompted", "send": send_result}


@shared_task
def finalize_whatsapp_document_batch(session_id: int) -> dict:
    session = (
        WhatsAppDocumentBatchSession.objects.select_related("reservation", "job", "tenant")
        .filter(pk=session_id)
        .first()
    )
    if session is None:
        return {"status": "missing"}

    return _run_finalize(session)


def handle_whatsapp_document_batch_reply(message_id: int) -> dict:
    row = (
        WhatsAppMessage.objects.select_related("tenant", "reservation")
        .filter(pk=message_id, direction=WhatsAppMessage.Direction.INBOUND)
        .first()
    )
    if row is None or row.reservation_id is None:
        return {"status": "skipped", "reason": "no_reservation"}

    from apps.integrations.whatsapp.guest_document_lifecycle import (
        check_guest_document_intake_automation,
    )

    allowed, reason = check_guest_document_intake_automation(row.reservation)
    if not allowed:
        return {"status": "skipped", "reason": reason}

    button_id = inbound_interactive_button_id(row)
    action_text = (row.body or "").strip()
    if not action_text and button_id:
        payload = row.raw_payload or {}
        interactive = payload.get("interactive") or {}
        action_text = str((interactive.get("button_reply") or {}).get("title") or "").strip()

    session = _get_active_session(reservation_id=row.reservation_id)
    if session is None or not _session_accepts_docs_reply(session):
        return {"status": "skipped", "reason": "no_active_confirm"}

    if is_documents_all_yes_reply(button_id=button_id, text=action_text):
        return finalize_whatsapp_document_batch(session.pk)

    if is_documents_all_no_reply(button_id=button_id, text=action_text):
        if session.status != WhatsAppDocumentBatchStatus.AWAITING_CONFIRM:
            return {"status": "skipped", "reason": "no_active_confirm"}
        _revoke_all_timers(session.pk)
        session.status = WhatsAppDocumentBatchStatus.AFTER_NO
        session.after_no_at = timezone.now()
        session.save(update_fields=["status", "after_no_at", "updated_at"])
        _schedule_after_no_quiet(session)
        return {"status": "after_no", "session_id": session.pk}

    return {"status": "skipped", "reason": "not_docs_reply"}
