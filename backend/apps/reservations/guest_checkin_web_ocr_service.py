"""Web guest check-in OCR upload, poll, and session sync."""

from __future__ import annotations

from django.conf import settings
from django.db import transaction

from apps.reservations.checkin_readiness import (
    SlotReadinessStatus,
    build_checkin_readiness,
    slot_validation_results,
)
from apps.reservations.document_intake_context import (
    DocumentIntakeContext,
    ensure_job_tenant_matches_reservation,
)
from apps.reservations.document_intake_service import (
    apply_document_intake_job,
    process_document_intake_job,
)
from apps.reservations.document_expectations import expected_document_slots
from apps.reservations.guest_checkin_events import (
    emit_guest_session_ready,
    emit_guest_slot_ready,
)
from apps.reservations.guest_checkin_ocr import (
    build_field_confidence,
    field_confidence_for_slot,
)
from apps.reservations.guest_checkin_session import touch_session_activity
from apps.reservations.models import (
    DocumentIntakeImage,
    DocumentIntakeJob,
    DocumentIntakeJobSource,
    DocumentIntakeJobStatus,
    GuestCheckInSession,
    Reservation,
)
from apps.reservations.tasks import process_document_intake_job_task

MAX_WEB_GUEST_FILES = 4


def max_web_guest_file_bytes() -> int:
    return int(getattr(settings, "DOCUMENT_PHOTO_MAX_BYTES", 8 * 1024 * 1024))


def collect_upload_files(request) -> list:
    files = request.FILES.getlist("files")
    if not files:
        for key in ("front", "back", "file"):
            uploaded = request.FILES.get(key)
            if uploaded is not None:
                files.append(uploaded)
    return files


def create_web_guest_intake_job(
    *,
    session: GuestCheckInSession,
    reservation: Reservation,
    position: int,
    files: list,
) -> DocumentIntakeJob:
    job = DocumentIntakeJob.objects.create(
        tenant_id=reservation.tenant_id,
        reservation=reservation,
        source=DocumentIntakeJobSource.WEB_GUEST,
        guest_checkin_slot_position=position,
        status=DocumentIntakeJobStatus.QUEUED,
        device_id=f"web-checkin:{session.token}",
    )
    ensure_job_tenant_matches_reservation(job, reservation)
    if job.tenant_id != reservation.tenant_id:
        job.save(update_fields=["tenant_id", "updated_at"])

    for idx, uploaded in enumerate(files):
        DocumentIntakeImage.objects.create(
            tenant_id=reservation.tenant_id,
            job=job,
            image=uploaded,
            sort_order=idx,
        )

    eager = getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False)
    if eager:
        process_document_intake_job(DocumentIntakeContext.from_job(job))
    else:
        try:
            process_document_intake_job_task.delay(job.pk)
        except Exception:
            process_document_intake_job(DocumentIntakeContext.from_job(job))

    return job


def _store_field_confidence_on_job(job: DocumentIntakeJob, *, position: int) -> None:
    reservation = job.reservation
    if reservation is None:
        return
    confidence = field_confidence_for_slot(reservation, position=position)
    if not confidence:
        return
    applied = list(job.applied_result or [])
    if not applied:
        applied = [{}]
    first = dict(applied[0]) if isinstance(applied[0], dict) else {}
    first["field_confidence"] = confidence
    applied[0] = first
    job.applied_result = applied
    job.save(update_fields=["applied_result", "updated_at"])


def sync_web_guest_apply_to_session(
    *,
    session: GuestCheckInSession,
    reservation: Reservation,
    position: int,
    job: DocumentIntakeJob,
) -> None:
    """Emit slot/session readiness events after WEB_GUEST OCR apply."""
    before_slots = {
        slot.position: slot.status for slot in slot_validation_results(reservation)
    }
    before_all_ready, _ = _readiness_flags(reservation)

    touch_session_activity(session)

    after_slots = slot_validation_results(reservation)
    for slot in after_slots:
        prev = before_slots.get(slot.position)
        if (
            prev != SlotReadinessStatus.READY
            and slot.status == SlotReadinessStatus.READY
        ):
            emit_guest_slot_ready(
                session=session,
                reservation=reservation,
                position=slot.position,
                guest_id=slot.guest_id,
            )

    after_all_ready, _ = _readiness_flags(reservation)
    if not before_all_ready and after_all_ready:
        emit_guest_session_ready(session=session, reservation=reservation)

    _store_field_confidence_on_job(job, position=position)


def _readiness_flags(reservation: Reservation) -> tuple[bool, object]:
    from apps.reservations.guest_checkin_orchestrator import readiness_snapshot

    return readiness_snapshot(reservation)


@transaction.atomic
def poll_and_apply_web_guest_job(
    *,
    session: GuestCheckInSession,
    reservation: Reservation,
    job: DocumentIntakeJob,
    position: int,
) -> DocumentIntakeJob:
    job.refresh_from_db()
    if job.status == DocumentIntakeJobStatus.DONE and not job.applied_result:
        ctx = DocumentIntakeContext.from_job(job)
        persons = (job.ocr_result or {}).get("persons") or []
        matches = job.matches or []
        if persons and matches:
            # Lock reservation for identity classify / apply race safety.
            Reservation.objects.select_for_update().get(pk=reservation.pk)
            applied = apply_document_intake_job(
                ctx,
                whatsapp_reply=False,
            )
            successful = [
                row
                for row in applied
                if isinstance(row, dict) and not row.get("identity_status")
            ]
            if successful:
                person = persons[0] if isinstance(persons[0], dict) else {}
                match = matches[0] if matches and isinstance(matches[0], dict) else {}
                confidence = build_field_confidence(
                    person=person,
                    ocr_result=job.ocr_result or {},
                    match=match,
                )
                merged = list(job.applied_result or [])
                if merged and isinstance(merged[0], dict) and not merged[0].get(
                    "identity_status"
                ):
                    merged[0] = {**merged[0], "field_confidence": confidence}
                    job.applied_result = merged
                    job.save(update_fields=["applied_result", "updated_at"])
                sync_web_guest_apply_to_session(
                    session=session,
                    reservation=reservation,
                    position=position,
                    job=job,
                )
        job.refresh_from_db()
    return job


