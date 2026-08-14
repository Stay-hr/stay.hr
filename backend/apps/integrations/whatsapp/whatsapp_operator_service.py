from __future__ import annotations

import logging
import mimetypes
import re
import zlib
from datetime import timedelta
from typing import Literal

from django.core.files.base import ContentFile
from django.db import connection, transaction
from django.utils import timezone

from apps.communications.canonical_store import create_with_canonical
from apps.communications.guest_compose import (
    HINT_ASK_ARRIVAL_TIME,
    HINT_CHECKIN_COMPLETE_SUPPLEMENT,
    HINT_OPERATOR_CHECKIN_COMPLETE,
    render_ask_arrival_time_message,
    render_checkin_complete_supplement_message,
    render_entrance_image_caption,
    render_operator_checkin_complete_message,
)
from apps.communications.guest_message_send import send_whatsapp_entrance_image_from_asset
from apps.communications.models import GuestMessageChannel, GuestMessageDraft, GuestMessageIntent
from apps.communications.guest_compose_defaults import (
    DOCUMENTS_BATCH_CONFIRM_NO,
    DOCUMENTS_BATCH_CONFIRM_YES,
    OPERATOR_DOCUMENTS_CONFIRM,
)
from apps.communications.guest_message_send import (
    default_email_subject,
    send_guest_email_with_timeline_record,
)
from apps.communications.models import (
    GuestMessageDraft,
    GuestMessageIntent,
    GuestOutboundMessageStatus,
)
from apps.integrations.models import IntegrationConfig, WhatsAppMessage
from apps.integrations.whatsapp.client import (
    WhatsAppApiError,
    extract_outbound_wamid,
    send_interactive_button_message,
    send_text_message,
)
from apps.integrations.whatsapp.media_download import (
    WhatsAppMediaError,
    extract_media_from_message,
    fetch_whatsapp_media,
)
from apps.integrations.whatsapp.operator_reservation_pick import (
    build_operator_reservation_pick_message,
)
from apps.integrations.whatsapp.reservation_lookup import (
    extract_booking_code_from_text,
    find_reservation_by_booking_code,
)
from apps.integrations.whatsapp.runtime_config import WhatsAppRuntimeConfig
from apps.integrations.whatsapp.whatsapp_operator import operator_name_for_wa_id
from apps.reservations.document_intake_audit import run_document_intake_matching_pipeline
from apps.reservations.document_intake_context import ensure_job_tenant_matches_reservation
from apps.reservations.guest_slots import ensure_guest_slots_for_intake
from apps.reservations.models import (
    DocumentIntakeImage,
    DocumentIntakeJob,
    DocumentIntakeJobSource,
    DocumentIntakeJobStatus,
    Reservation,
    WhatsAppOperatorSession,
    WhatsAppOperatorSessionStatus,
)

logger = logging.getLogger(__name__)

SESSION_TTL = timedelta(minutes=120)
SESSION_TTL_HOURS_LABEL = "2 h"
_MEDIA_MESSAGE_TYPES = frozenset({"image", "document"})
_CHECKIN_COMMANDS = frozenset({"check in", "checkin"})
OPERATOR_CHECKIN_BUTTON_ID = "op_check_in"
OPERATOR_CHECKIN_BUTTON_TITLE = "Check-in"
OPERATOR_DOCS_ALL_YES_ID = "op_docs_all_yes"
OPERATOR_DOCS_ALL_NO_ID = "op_docs_all_no"

_ACTIVE_COLLECT_STATUSES = frozenset(
    {
        WhatsAppOperatorSessionStatus.COLLECTING,
        WhatsAppOperatorSessionStatus.AWAITING_CONFIRM,
        WhatsAppOperatorSessionStatus.AWAITING_RES_PICK,
    }
)

_DOCS_ALL_YES_IDS = frozenset({OPERATOR_DOCS_ALL_YES_ID, "docs_all_yes"})
_DOCS_ALL_NO_IDS = frozenset({OPERATOR_DOCS_ALL_NO_ID, "docs_all_no"})
_DOCS_ALL_YES_TEXTS = frozenset({"da", "yes", "ja", "si", "sí", "oui"})
_DOCS_ALL_NO_TEXTS = frozenset({"ne", "no", "nein"})

GuestNotifyMode = Literal["default", "email_only", "skip"]


def _normalize_command(text: str) -> str:
    lowered = (text or "").strip().lower()
    lowered = re.sub(r"[_\-]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def is_operator_checkin_command(text: str) -> bool:
    return _normalize_command(text) in _CHECKIN_COMMANDS


def is_operator_checkin_trigger(*, button_id: str = "", text: str = "") -> bool:
    if (button_id or "").strip() == OPERATOR_CHECKIN_BUTTON_ID:
        return True
    return is_operator_checkin_command(text)


def is_operator_docs_all_yes_reply(*, button_id: str = "", text: str = "") -> bool:
    if (button_id or "").strip() in _DOCS_ALL_YES_IDS:
        return True
    return _normalize_command(text) in _DOCS_ALL_YES_TEXTS


def is_operator_docs_all_no_reply(*, button_id: str = "", text: str = "") -> bool:
    if (button_id or "").strip() in _DOCS_ALL_NO_IDS:
        return True
    return _normalize_command(text) in _DOCS_ALL_NO_TEXTS


def _image_count_label(count: int) -> str:
    if count == 1:
        return "1 slika"
    if 2 <= count <= 4:
        return f"{count} slike"
    return f"{count} slika"


def _checkin_prompt_body(image_count: int) -> str:
    return (
        f"Primljeno {_image_count_label(image_count)}. "
        f"Pritisnite {OPERATOR_CHECKIN_BUTTON_TITLE} ako ste gotovi."
    )


def _operator_confirm_body() -> str:
    return OPERATOR_DOCUMENTS_CONFIRM.get("hr") or OPERATOR_DOCUMENTS_CONFIRM["en"]


def _operator_confirm_yes_label() -> str:
    return DOCUMENTS_BATCH_CONFIRM_YES.get("hr") or DOCUMENTS_BATCH_CONFIRM_YES["en"]


def _operator_confirm_no_label() -> str:
    return DOCUMENTS_BATCH_CONFIRM_NO.get("hr") or DOCUMENTS_BATCH_CONFIRM_NO["en"]


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


def _session_expired(session: WhatsAppOperatorSession) -> bool:
    return session.last_activity_at + SESSION_TTL < timezone.now()


def _mark_session_failed(session: WhatsAppOperatorSession) -> None:
    session.status = WhatsAppOperatorSessionStatus.FAILED
    session.save(update_fields=["status", "updated_at"])


def _pg_advisory_xact_lock_operator(tenant_id: int, operator_wa_id: str) -> None:
    """Serialize operator collect/check-in per tenant+wa_id (works when no session row exists yet)."""
    key = zlib.crc32(f"wa-op:{tenant_id}:{operator_wa_id}".encode()) & 0x7FFFFFFF
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [key])


def _message_already_in_operator_job(job: DocumentIntakeJob, message_id: int) -> bool:
    prefix = f"op_{message_id}"
    for img in job.images.only("image"):
        name = (img.image.name or "").rsplit("/", 1)[-1]
        if name.startswith(prefix):
            return True
    return False


def _image_filename_in_job(job: DocumentIntakeJob, filename: str) -> bool:
    for img in job.images.only("image"):
        name = (img.image.name or "").rsplit("/", 1)[-1]
        if name == filename:
            return True
    return False


def consolidate_operator_collect_sessions(
    *,
    tenant_id: int,
    operator_wa_id: str,
    canonical_session: WhatsAppOperatorSession,
) -> WhatsAppOperatorSession:
    """Merge stray active sessions into canonical job (race / legacy cleanup)."""
    others = list(
        WhatsAppOperatorSession.objects.filter(
            tenant_id=tenant_id,
            operator_wa_id=operator_wa_id,
            status__in=_ACTIVE_COLLECT_STATUSES,
        )
        .exclude(pk=canonical_session.pk)
        .select_related("job")
    )
    if not others:
        return canonical_session

    canonical_job = canonical_session.job
    for other in others:
        other_job = other.job
        for img in other_job.images.order_by("sort_order", "id"):
            name = (img.image.name or "").rsplit("/", 1)[-1]
            if _image_filename_in_job(canonical_job, name):
                continue
            img.image.open("rb")
            try:
                content = img.image.read()
            finally:
                img.image.close()
            sort_order = canonical_job.images.count()
            DocumentIntakeImage.objects.create(
                tenant_id=tenant_id,
                job=canonical_job,
                image=ContentFile(content, name=name),
                sort_order=sort_order,
            )
        logger.info(
            "Merged operator session #%s (job #%s) into session #%s",
            other.pk,
            other_job.pk,
            canonical_session.pk,
        )
        _mark_session_failed(other)

    canonical_session.last_activity_at = timezone.now()
    canonical_session.save(update_fields=["last_activity_at", "updated_at"])
    return canonical_session


def merge_images_into_operator_job(
    canonical_job: DocumentIntakeJob,
    source_jobs: list[DocumentIntakeJob],
) -> int:
    """Copy images from other operator jobs into canonical job (reconcile / replay)."""
    moved = 0
    tenant_id = canonical_job.tenant_id
    for job in source_jobs:
        if job.pk == canonical_job.pk:
            continue
        for img in job.images.order_by("sort_order", "id"):
            name = (img.image.name or "").rsplit("/", 1)[-1]
            if _image_filename_in_job(canonical_job, name):
                continue
            img.image.open("rb")
            try:
                content = img.image.read()
            finally:
                img.image.close()
            sort_order = canonical_job.images.count()
            DocumentIntakeImage.objects.create(
                tenant_id=tenant_id,
                job=canonical_job,
                image=ContentFile(content, name=name),
                sort_order=sort_order,
            )
            moved += 1
    return moved


def _get_operator_session_queryset(
    *,
    tenant_id: int,
    operator_wa_id: str,
    statuses: frozenset[str] | None = None,
    for_update: bool = False,
):
    qs = WhatsAppOperatorSession.objects.filter(
        tenant_id=tenant_id,
        operator_wa_id=operator_wa_id,
    ).select_related("job")
    if statuses is not None:
        qs = qs.filter(status__in=statuses)
    if for_update:
        qs = qs.select_for_update()
    return qs.order_by("-last_activity_at", "id")


def _get_latest_operator_session(
    *,
    tenant_id: int,
    operator_wa_id: str,
) -> WhatsAppOperatorSession | None:
    return _get_operator_session_queryset(
        tenant_id=tenant_id,
        operator_wa_id=operator_wa_id,
    ).first()


def _get_active_collect_session(
    *,
    tenant_id: int,
    operator_wa_id: str,
    for_update: bool = False,
) -> WhatsAppOperatorSession | None:
    qs = _get_operator_session_queryset(
        tenant_id=tenant_id,
        operator_wa_id=operator_wa_id,
        statuses=_ACTIVE_COLLECT_STATUSES,
        for_update=for_update,
    )
    sessions = list(qs[:5])
    if not sessions:
        return None

    session = max(sessions, key=lambda s: (s.job.images.count(), s.last_activity_at, s.pk))
    if _session_expired(session):
        _mark_session_failed(session)
        return None
    return session


def _blocked_finalize_message(
    *,
    tenant_id: int,
    operator_wa_id: str,
) -> str:
    latest = _get_latest_operator_session(tenant_id=tenant_id, operator_wa_id=operator_wa_id)
    if latest is None:
        return "Pošaljite fotografije dokumenta."

    if latest.status == WhatsAppOperatorSessionStatus.PROCESSING:
        return "Check-in je u tijeku, pričekajte…"

    if latest.status == WhatsAppOperatorSessionStatus.DONE:
        return "Check-in za ovu seriju je već obavljen."

    if latest.status == WhatsAppOperatorSessionStatus.FAILED and _session_expired(latest):
        return (
            f"Sesija istekla ({SESSION_TTL_HOURS_LABEL}). "
            "Pošaljite slike ponovo."
        )

    if latest.status in _ACTIVE_COLLECT_STATUSES and _session_expired(latest):
        return (
            f"Sesija istekla ({SESSION_TTL_HOURS_LABEL}). "
            "Pošaljite slike ponovo."
        )

    return "Pošaljite fotografije dokumenta."


def _send_operator_text(
    *,
    integration_row: IntegrationConfig,
    runtime: WhatsAppRuntimeConfig,
    operator_wa_id: str,
    body: str,
    reservation: Reservation | None = None,
) -> dict:
    try:
        response = send_text_message(
            phone_number_id=runtime.phone_number_id,
            access_token=runtime.access_token,
            to_wa_id=operator_wa_id,
            body=body,
        )
    except WhatsAppApiError as exc:
        logger.warning("Operator WhatsApp reply failed wa_id=%s: %s", operator_wa_id, exc)
        return {"status": "send_failed", "detail": str(exc)}

    outbound_wamid = extract_outbound_wamid(response)
    if outbound_wamid:
        create_with_canonical(WhatsAppMessage,
            tenant_id=integration_row.tenant_id,
            integration=integration_row,
            reservation=reservation,
            wamid=outbound_wamid,
            wa_id=operator_wa_id,
            phone_number_id=runtime.phone_number_id,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            message_type="text",
            body=body,
            raw_payload=response,
        )
    return {"status": "sent", "outbound_wamid": outbound_wamid}


def _send_operator_checkin_prompt(
    *,
    integration_row: IntegrationConfig,
    runtime: WhatsAppRuntimeConfig,
    operator_wa_id: str,
    image_count: int,
) -> dict:
    body = _checkin_prompt_body(image_count)
    try:
        response = send_interactive_button_message(
            phone_number_id=runtime.phone_number_id,
            access_token=runtime.access_token,
            to_wa_id=operator_wa_id,
            body=body,
            buttons=[(OPERATOR_CHECKIN_BUTTON_ID, OPERATOR_CHECKIN_BUTTON_TITLE)],
        )
    except WhatsAppApiError as exc:
        logger.warning("Operator check-in prompt failed wa_id=%s: %s", operator_wa_id, exc)
        return {"status": "send_failed", "detail": str(exc)}

    outbound_wamid = extract_outbound_wamid(response)
    if outbound_wamid:
        create_with_canonical(WhatsAppMessage,
            tenant_id=integration_row.tenant_id,
            integration=integration_row,
            wamid=outbound_wamid,
            wa_id=operator_wa_id,
            phone_number_id=runtime.phone_number_id,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            message_type="interactive",
            body=body,
            raw_payload=response,
        )
    return {"status": "sent", "outbound_wamid": outbound_wamid}


def _send_operator_docs_confirm_prompt(
    *,
    integration_row: IntegrationConfig,
    runtime: WhatsAppRuntimeConfig,
    operator_wa_id: str,
) -> dict:
    body = _operator_confirm_body()
    yes_label = _operator_confirm_yes_label()
    no_label = _operator_confirm_no_label()
    try:
        response = send_interactive_button_message(
            phone_number_id=runtime.phone_number_id,
            access_token=runtime.access_token,
            to_wa_id=operator_wa_id,
            body=body,
            buttons=[
                (OPERATOR_DOCS_ALL_YES_ID, yes_label),
                (OPERATOR_DOCS_ALL_NO_ID, no_label),
            ],
        )
    except WhatsAppApiError as exc:
        logger.warning("Operator docs confirm failed wa_id=%s: %s", operator_wa_id, exc)
        return {"status": "send_failed", "detail": str(exc)}

    outbound_wamid = extract_outbound_wamid(response)
    if outbound_wamid:
        create_with_canonical(WhatsAppMessage,
            tenant_id=integration_row.tenant_id,
            integration=integration_row,
            wamid=outbound_wamid,
            wa_id=operator_wa_id,
            phone_number_id=runtime.phone_number_id,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            message_type="interactive",
            body=body,
            raw_payload=response,
        )
    return {"status": "sent", "outbound_wamid": outbound_wamid}


def _operator_help_text() -> str:
    return (
        "Operatorski check-in:\n"
        "1) Pošaljite slike dokumenata (osobna/putovnica).\n"
        f"2) Pritisnite gumb {OPERATOR_CHECKIN_BUTTON_TITLE} kad ste gotovi.\n"
        "3) Potvrdite Da da sustav obradi dokumente.\n\n"
        "Sustav pronađe rezervaciju, popuni gosta i pošalje potvrdu gostu "
        "na WhatsApp (ili email ako gost nema WA)."
    )


def _operator_checkin_complete_already_sent(reservation_id: int) -> bool:
    return GuestMessageDraft.objects.filter(
        reservation_id=reservation_id,
        hint=HINT_OPERATOR_CHECKIN_COMPLETE,
    ).exists()


def _send_checkin_complete_entrance_image(
    reservation: Reservation,
    *,
    hint: str = HINT_OPERATOR_CHECKIN_COMPLETE,
) -> dict:
    caption = render_entrance_image_caption(reservation)
    lang = (reservation.property.tenant.default_language or "en")[:2]
    image_draft = GuestMessageDraft.objects.create(
        tenant_id=reservation.tenant_id,
        reservation=reservation,
        intent=GuestMessageIntent.REPLY,
        hint=hint,
        llm_body_text=caption,
        final_body_text=caption,
        language=lang,
        channel=GuestMessageChannel.WHATSAPP,
    )
    try:
        send_whatsapp_entrance_image_from_asset(
            reservation=reservation,
            draft=image_draft,
            caption=caption,
            api_application=None,
        )
        return {"status": "sent"}
    except ValueError as exc:
        logger.warning(
            "Check-in complete entrance image failed reservation_id=%s: %s",
            reservation.pk,
            exc,
        )
        return {"status": "failed", "detail": str(exc)}


def send_whatsapp_ask_arrival_time(reservation: Reservation) -> dict:
    """Short message asking when the guest expects to arrive."""
    body = render_ask_arrival_time_message(reservation)
    from apps.integrations.whatsapp.evisitor_reply import _send_reservation_whatsapp_text

    return _send_reservation_whatsapp_text(
        reservation=reservation,
        body=body,
        hint=HINT_ASK_ARRIVAL_TIME,
    )


def send_whatsapp_checkin_arrival_supplement(reservation: Reservation) -> dict:
    """One-off follow-up: check-in time, entrance, parking, entrance photo (no WiFi)."""
    body = render_checkin_complete_supplement_message(reservation)
    from apps.integrations.whatsapp.evisitor_reply import _send_reservation_whatsapp_text

    wa_result = _send_reservation_whatsapp_text(
        reservation=reservation,
        body=body,
        hint=HINT_CHECKIN_COMPLETE_SUPPLEMENT,
    )
    if wa_result.get("status") != "sent":
        return wa_result
    entrance_image = _send_checkin_complete_entrance_image(
        reservation,
        hint=HINT_CHECKIN_COMPLETE_SUPPLEMENT,
    )
    return {**wa_result, "entrance_image": entrance_image}


def notify_guest_operator_checkin_complete(
    reservation: Reservation,
    *,
    guest_notify_mode: GuestNotifyMode = "default",
) -> dict:
    """Send complete check-in message to guest after WA autocheck-in or operator finalize."""
    if guest_notify_mode == "skip":
        return {"channel": "none", "status": "skipped", "reason": "guest_notify_skipped"}

    if _operator_checkin_complete_already_sent(reservation.pk):
        return {"channel": "none", "status": "already_sent"}

    if guest_notify_mode == "email_only":
        email_result = _send_guest_operator_checkin_email(reservation)
        if email_result.get("sent"):
            return {"channel": "email", "sent": True, "to": email_result.get("to")}
        return {
            "channel": "none",
            "status": "failed",
            "reason": email_result.get("reason") or "send_failed",
            "email": email_result,
        }

    body = render_operator_checkin_complete_message(reservation)
    from apps.integrations.whatsapp.guest_welcome_sequence import (
        send_guest_welcome_entrance_and_ask_arrival,
    )

    wa_result = send_guest_welcome_entrance_and_ask_arrival(
        reservation,
        body=body,
        hint=HINT_OPERATOR_CHECKIN_COMPLETE,
    )
    if wa_result.get("status") == "sent":
        return {"channel": "whatsapp", **wa_result}

    email_result = _send_guest_operator_checkin_email(reservation)
    if email_result.get("sent"):
        return {"channel": "email", "sent": True, "to": email_result.get("to"), "whatsapp": wa_result}

    reason = (
        wa_result.get("reason")
        or wa_result.get("detail")
        or email_result.get("reason")
        or "send_failed"
    )
    return {
        "channel": "none",
        "status": "failed",
        "reason": reason,
        "whatsapp": wa_result,
        "email": email_result,
    }


def _send_guest_operator_checkin_email(reservation: Reservation) -> dict:
    body = render_operator_checkin_complete_message(reservation)
    subject = default_email_subject(reservation)
    outbound = send_guest_email_with_timeline_record(
        reservation,
        body,
        subject=subject,
        intent=GuestMessageIntent.REPLY,
        hint=HINT_OPERATOR_CHECKIN_COMPLETE,
    )
    if outbound.status != GuestOutboundMessageStatus.SENT:
        return {"sent": False, "reason": outbound.error_message or "send_failed"}
    return {"sent": True, "to": outbound.to_email}


def _collect_operator_image(
    *,
    row: WhatsAppMessage,
    integration_row: IntegrationConfig,
    runtime: WhatsAppRuntimeConfig,
) -> dict:
    media_id, mime_type, caption = extract_media_from_message(row.raw_payload or {})
    if not media_id:
        return {"status": "skipped", "reason": "no_media_id"}

    try:
        content, downloaded_mime = fetch_whatsapp_media(
            media_id=media_id,
            access_token=runtime.access_token,
        )
    except WhatsAppMediaError as exc:
        logger.warning("Operator media download failed message_id=%s: %s", row.pk, exc)
        _send_operator_text(
            integration_row=integration_row,
            runtime=runtime,
            operator_wa_id=row.wa_id,
            body=f"Preuzimanje slike nije uspjelo: {exc}",
        )
        return {"status": "download_failed", "detail": str(exc)}

    mime = downloaded_mime or mime_type
    filename = f"op_{row.pk}{_extension_for_mime(mime)}"

    duplicate = False
    with transaction.atomic():
        _pg_advisory_xact_lock_operator(row.tenant_id, row.wa_id)
        session = _get_active_collect_session(
            tenant_id=row.tenant_id,
            operator_wa_id=row.wa_id,
            for_update=True,
        )
        if session is None:
            job = DocumentIntakeJob.objects.create(
                tenant_id=row.tenant_id,
                source=DocumentIntakeJobSource.WHATSAPP_OPERATOR,
                status=DocumentIntakeJobStatus.QUEUED,
                device_id="whatsapp_operator",
                whatsapp_message=row,
            )
            session = WhatsAppOperatorSession.objects.create(
                tenant_id=row.tenant_id,
                operator_wa_id=row.wa_id,
                job=job,
                status=WhatsAppOperatorSessionStatus.COLLECTING,
            )
        else:
            job = session.job
            if session.status in {
                WhatsAppOperatorSessionStatus.AWAITING_CONFIRM,
                WhatsAppOperatorSessionStatus.AWAITING_RES_PICK,
            }:
                session.status = WhatsAppOperatorSessionStatus.COLLECTING
                job.reservation_id = None
                job.save(update_fields=["reservation_id", "updated_at"])

        session = consolidate_operator_collect_sessions(
            tenant_id=row.tenant_id,
            operator_wa_id=row.wa_id,
            canonical_session=session,
        )
        job = session.job

        if _message_already_in_operator_job(job, row.pk):
            duplicate = True
            session.last_activity_at = timezone.now()
            session.save(update_fields=["last_activity_at", "updated_at"])
        else:
            sort_order = job.images.count()
            DocumentIntakeImage.objects.create(
                tenant_id=row.tenant_id,
                job=job,
                image=ContentFile(content, name=filename),
                sort_order=sort_order,
            )
            session.last_activity_at = timezone.now()
            session.save(update_fields=["status", "last_activity_at", "updated_at"])

        if caption and not (row.body or "").strip():
            row.body = caption
            row.save(update_fields=["body"])

    from apps.integrations.whatsapp.whatsapp_operator_batch import schedule_operator_quiet_timer

    schedule_operator_quiet_timer(session)
    image_count = job.images.count()
    return {
        "status": "duplicate" if duplicate else "collected",
        "session_id": session.pk,
        "job_id": job.pk,
        "image_count": image_count,
    }


def _format_match_candidates(matches: list[dict]) -> str:
    lines: list[str] = []
    seen: set[tuple[int, int, str]] = set()

    def _append_candidate(candidate: dict, *, person_name: str) -> None:
        reservation_id = candidate.get("reservation_id")
        if reservation_id is None:
            return
        guest_id = int(candidate.get("guest_id") or 0)
        label = str(candidate.get("reservation_label") or "").strip()
        key = (int(reservation_id), guest_id, label)
        if key in seen:
            return
        seen.add(key)
        guest_name = str(candidate.get("guest_name") or "").strip()
        match_type = str(candidate.get("match_type") or "").strip()
        person_prefix = ""
        if person_name:
            short = person_name if len(person_name) <= 36 else person_name[:33] + "…"
            person_prefix = f"{short}: "
        guest_suffix = f" ({guest_name})" if guest_name else ""
        type_suffix = f" [{match_type}]" if match_type else ""
        lines.append(f"• {person_prefix}#{reservation_id} {label}{guest_suffix}{type_suffix}".strip())

    for match in matches:
        if not isinstance(match, dict):
            continue
        person_name = str(match.get("person_name") or "").strip()

        if match.get("reservation_id") is not None:
            _append_candidate(
                {
                    "reservation_id": match.get("reservation_id"),
                    "guest_id": match.get("guest_id"),
                    "reservation_label": match.get("reservation_label"),
                    "guest_name": match.get("guest_name"),
                    "match_type": "resolved",
                },
                person_name=person_name,
            )

        nested = [c for c in (match.get("candidates") or []) if isinstance(c, dict)]
        name_candidates = [
            c for c in nested if c.get("match_type") in {"name", "document_number"}
        ]
        other_candidates = [c for c in nested if c not in name_candidates]
        for candidate in name_candidates + other_candidates:
            _append_candidate(candidate, person_name=person_name)

    return "\n".join(lines[:12])


def _auto_matches_from_job(job: DocumentIntakeJob) -> list[dict]:
    return [
        m
        for m in (job.matches or [])
        if isinstance(m, dict) and m.get("auto_apply") and m.get("guest_id")
    ]


def _reservation_ids_from_auto_matches(auto_matches: list[dict]) -> set[int]:
    return {int(m["reservation_id"]) for m in auto_matches if m.get("reservation_id")}


def _rematch_operator_job_for_reservation(
    job: DocumentIntakeJob,
    reservation: Reservation,
) -> list[dict]:
    persons = (job.ocr_result or {}).get("persons") or []
    if not isinstance(persons, list):
        persons = []
    ensure_guest_slots_for_intake(
        tenant=reservation.tenant,
        reservation=reservation,
        min_count=len(persons),
    )
    ensure_job_tenant_matches_reservation(job, reservation)
    matches = run_document_intake_matching_pipeline(
        tenant_id=reservation.tenant_id,
        reservation=reservation,
        persons=persons,
    )
    job.reservation_id = reservation.pk
    job.matches = matches
    job.save(update_fields=["reservation_id", "tenant_id", "matches", "updated_at"])
    return matches


def _enter_awaiting_reservation_pick(
    *,
    session: WhatsAppOperatorSession,
    job: DocumentIntakeJob,
    integration_row: IntegrationConfig,
    runtime: WhatsAppRuntimeConfig,
    operator_wa_id: str,
) -> dict:
    session.status = WhatsAppOperatorSessionStatus.AWAITING_RES_PICK
    session.last_activity_at = timezone.now()
    session.save(update_fields=["status", "last_activity_at", "updated_at"])
    body = build_operator_reservation_pick_message(job)
    _send_operator_text(
        integration_row=integration_row,
        runtime=runtime,
        operator_wa_id=operator_wa_id,
        body=body,
    )
    return {
        "status": "awaiting_reservation_pick",
        "session_id": session.pk,
        "job_id": job.pk,
    }


def _continue_operator_apply_and_checkin(
    *,
    session: WhatsAppOperatorSession,
    job: DocumentIntakeJob,
    row: WhatsAppMessage,
    integration_row: IntegrationConfig,
    runtime: WhatsAppRuntimeConfig,
) -> dict:
    from apps.integrations.whatsapp.document_intake_finalize import finalize_document_intake_job
    from apps.reservations.document_intake_context import DocumentIntakeContext

    result = finalize_document_intake_job(
        DocumentIntakeContext.from_job(job),
        channel="operator",
        wa_id=row.wa_id,
        integration_row=integration_row,
        runtime=runtime,
        session=session,
    )
    if result.get("status") == "ambiguous_reservation":
        return _enter_awaiting_reservation_pick(
            session=session,
            job=job,
            integration_row=integration_row,
            runtime=runtime,
            operator_wa_id=row.wa_id,
        )
    return result


def _get_awaiting_res_pick_session(
    *,
    tenant_id: int,
    operator_wa_id: str,
    for_update: bool = False,
) -> WhatsAppOperatorSession | None:
    return (
        _get_operator_session_queryset(
            tenant_id=tenant_id,
            operator_wa_id=operator_wa_id,
            statuses=frozenset({WhatsAppOperatorSessionStatus.AWAITING_RES_PICK}),
            for_update=for_update,
        ).first()
    )


def _handle_operator_reservation_pick(
    *,
    row: WhatsAppMessage,
    integration_row: IntegrationConfig,
    runtime: WhatsAppRuntimeConfig,
    action_text: str,
) -> dict:
    with transaction.atomic():
        _pg_advisory_xact_lock_operator(row.tenant_id, row.wa_id)
        session = _get_awaiting_res_pick_session(
            tenant_id=row.tenant_id,
            operator_wa_id=row.wa_id,
            for_update=True,
        )
        if session is None:
            return {"status": "skipped", "reason": "not_awaiting_res_pick"}

        if _session_expired(session):
            _mark_session_failed(session)
            body = (
                f"Sesija istekla ({SESSION_TTL_HOURS_LABEL}). "
                "Pošaljite slike ponovo."
            )
            _send_operator_text(
                integration_row=integration_row,
                runtime=runtime,
                operator_wa_id=row.wa_id,
                body=body,
            )
            return {"status": "session_expired"}

        code = extract_booking_code_from_text(action_text)
        if not code:
            stripped = (action_text or "").strip()
            code = stripped if stripped else None
        if not code:
            _send_operator_text(
                integration_row=integration_row,
                runtime=runtime,
                operator_wa_id=row.wa_id,
                body="Pošaljite #rezervacije ili Booking broj.",
            )
            return {"status": "awaiting_reservation_pick", "reason": "no_code"}

        reservation = find_reservation_by_booking_code(tenant_id=row.tenant_id, code=code)
        if reservation is None:
            _send_operator_text(
                integration_row=integration_row,
                runtime=runtime,
                operator_wa_id=row.wa_id,
                body="Nisam pronašao rezervaciju s tim brojem.",
            )
            return {"status": "reservation_not_found", "code": code}

        job = session.job
        _rematch_operator_job_for_reservation(job, reservation)
        job.refresh_from_db()

        auto_matches = _auto_matches_from_job(job)
        if not auto_matches:
            body = (
                f"Na #{reservation.pk} ne mogu mapirati sve osobe s dokumenta.\n\n"
                f"{build_operator_reservation_pick_message(job)}"
            )[:1024]
            session.last_activity_at = timezone.now()
            session.save(update_fields=["last_activity_at", "updated_at"])
            _send_operator_text(
                integration_row=integration_row,
                runtime=runtime,
                operator_wa_id=row.wa_id,
                body=body,
            )
            return {"status": "pick_rematch_failed", "job_id": job.pk}

        session.status = WhatsAppOperatorSessionStatus.PROCESSING
        session.save(update_fields=["status", "updated_at"])

    return _continue_operator_apply_and_checkin(
        session=session,
        job=job,
        row=row,
        integration_row=integration_row,
        runtime=runtime,
    )


def _request_operator_checkin(
    *,
    row: WhatsAppMessage,
    integration_row: IntegrationConfig,
    runtime: WhatsAppRuntimeConfig,
) -> dict:
    with transaction.atomic():
        _pg_advisory_xact_lock_operator(row.tenant_id, row.wa_id)
        session = _get_active_collect_session(
            tenant_id=row.tenant_id,
            operator_wa_id=row.wa_id,
            for_update=True,
        )
        if session is None:
            body = _blocked_finalize_message(
                tenant_id=row.tenant_id,
                operator_wa_id=row.wa_id,
            )
            _send_operator_text(
                integration_row=integration_row,
                runtime=runtime,
                operator_wa_id=row.wa_id,
                body=body,
            )
            return {"status": "no_session"}

        if session.status == WhatsAppOperatorSessionStatus.AWAITING_CONFIRM:
            awaiting_finalize = True
        else:
            awaiting_finalize = False

        job = session.job
        if job.images.count() == 0:
            _send_operator_text(
                integration_row=integration_row,
                runtime=runtime,
                operator_wa_id=row.wa_id,
                body="Nema slika u sesiji. Pošaljite fotografije dokumenta pa check-in.",
            )
            return {"status": "no_images"}

    if awaiting_finalize:
        return _finalize_operator_checkin(
            row=row,
            integration_row=integration_row,
            runtime=runtime,
        )

    from apps.integrations.whatsapp.whatsapp_operator_batch import send_operator_collect_prompt_for_session

    return send_operator_collect_prompt_for_session(session.pk)


def _decline_operator_docs_confirm(
    *,
    row: WhatsAppMessage,
    integration_row: IntegrationConfig,
    runtime: WhatsAppRuntimeConfig,
) -> dict:
    with transaction.atomic():
        _pg_advisory_xact_lock_operator(row.tenant_id, row.wa_id)
        session = _get_active_collect_session(
            tenant_id=row.tenant_id,
            operator_wa_id=row.wa_id,
            for_update=True,
        )
        if session is None or session.status != WhatsAppOperatorSessionStatus.AWAITING_CONFIRM:
            return {"status": "skipped", "reason": "not_awaiting_confirm"}

        session.status = WhatsAppOperatorSessionStatus.COLLECTING
        session.last_activity_at = timezone.now()
        session.save(update_fields=["status", "last_activity_at", "updated_at"])

    _send_operator_text(
        integration_row=integration_row,
        runtime=runtime,
        operator_wa_id=row.wa_id,
        body="Pošaljite još slike, zatim ponovo Check-in.",
    )
    return {"status": "collecting", "session_id": session.pk}


def _finalize_operator_checkin(
    *,
    row: WhatsAppMessage,
    integration_row: IntegrationConfig,
    runtime: WhatsAppRuntimeConfig,
) -> dict:
    with transaction.atomic():
        _pg_advisory_xact_lock_operator(row.tenant_id, row.wa_id)
        session = (
            _get_operator_session_queryset(
                tenant_id=row.tenant_id,
                operator_wa_id=row.wa_id,
                statuses=frozenset({WhatsAppOperatorSessionStatus.AWAITING_CONFIRM}),
                for_update=True,
            ).first()
        )
        if session is None:
            processing_or_done = _get_operator_session_queryset(
                tenant_id=row.tenant_id,
                operator_wa_id=row.wa_id,
                statuses=frozenset(
                    {
                        WhatsAppOperatorSessionStatus.PROCESSING,
                        WhatsAppOperatorSessionStatus.DONE,
                    }
                ),
                for_update=False,
            ).first()
            if processing_or_done is not None:
                if processing_or_done.status == WhatsAppOperatorSessionStatus.PROCESSING:
                    body = "Check-in je u tijeku, pričekajte…"
                else:
                    body = "Check-in za ovu seriju je već obavljen."
                _send_operator_text(
                    integration_row=integration_row,
                    runtime=runtime,
                    operator_wa_id=row.wa_id,
                    body=body,
                )
                return {
                    "status": "blocked",
                    "reason": processing_or_done.status,
                }

            body = _blocked_finalize_message(
                tenant_id=row.tenant_id,
                operator_wa_id=row.wa_id,
            )
            _send_operator_text(
                integration_row=integration_row,
                runtime=runtime,
                operator_wa_id=row.wa_id,
                body=body,
            )
            return {"status": "no_session"}

        if _session_expired(session):
            _mark_session_failed(session)
            body = (
                f"Sesija istekla ({SESSION_TTL_HOURS_LABEL}). "
                "Pošaljite slike ponovo."
            )
            _send_operator_text(
                integration_row=integration_row,
                runtime=runtime,
                operator_wa_id=row.wa_id,
                body=body,
            )
            return {"status": "session_expired"}

        job = session.job
        if job.images.count() == 0:
            _send_operator_text(
                integration_row=integration_row,
                runtime=runtime,
                operator_wa_id=row.wa_id,
                body="Nema slika u sesiji. Pošaljite fotografije dokumenta pa check-in.",
            )
            return {"status": "no_images"}

        session.status = WhatsAppOperatorSessionStatus.PROCESSING
        session.save(update_fields=["status", "updated_at"])

    return _continue_operator_apply_and_checkin(
        session=session,
        job=job,
        row=row,
        integration_row=integration_row,
        runtime=runtime,
    )


def handle_operator_inbound(
    *,
    row: WhatsAppMessage,
    integration_row: IntegrationConfig,
    runtime: WhatsAppRuntimeConfig,
    action_text: str,
    button_id: str = "",
) -> dict:
    from apps.integrations.whatsapp.operator_arrival_confirm import handle_operator_arrival_confirm_inbound

    if row.message_type in _MEDIA_MESSAGE_TYPES:
        return _collect_operator_image(
            row=row,
            integration_row=integration_row,
            runtime=runtime,
        )

    pick_session = _get_awaiting_res_pick_session(
        tenant_id=row.tenant_id,
        operator_wa_id=row.wa_id,
    )
    if pick_session is not None:
        if is_operator_docs_all_no_reply(button_id=button_id, text=action_text):
            with transaction.atomic():
                _pg_advisory_xact_lock_operator(row.tenant_id, row.wa_id)
                session = _get_awaiting_res_pick_session(
                    tenant_id=row.tenant_id,
                    operator_wa_id=row.wa_id,
                    for_update=True,
                )
                if session is not None:
                    session.status = WhatsAppOperatorSessionStatus.COLLECTING
                    session.last_activity_at = timezone.now()
                    session.save(update_fields=["status", "last_activity_at", "updated_at"])
                    job = session.job
                    job.reservation_id = None
                    job.save(update_fields=["reservation_id", "updated_at"])
            _send_operator_text(
                integration_row=integration_row,
                runtime=runtime,
                operator_wa_id=row.wa_id,
                body="Pošaljite još slike, zatim ponovo Check-in.",
            )
            return {"status": "collecting", "session_id": pick_session.pk}
        if is_operator_checkin_trigger(button_id=button_id, text=action_text) or is_operator_docs_all_yes_reply(
            button_id=button_id, text=action_text
        ):
            _send_operator_text(
                integration_row=integration_row,
                runtime=runtime,
                operator_wa_id=row.wa_id,
                body="Pošaljite #rezervacije ili Booking broj.",
            )
            return {"status": "awaiting_reservation_pick"}
        return _handle_operator_reservation_pick(
            row=row,
            integration_row=integration_row,
            runtime=runtime,
            action_text=action_text,
        )

    if is_operator_docs_all_no_reply(button_id=button_id, text=action_text):
        return _decline_operator_docs_confirm(
            row=row,
            integration_row=integration_row,
            runtime=runtime,
        )

    if is_operator_docs_all_yes_reply(button_id=button_id, text=action_text):
        return _finalize_operator_checkin(
            row=row,
            integration_row=integration_row,
            runtime=runtime,
        )

    arrival_result = handle_operator_arrival_confirm_inbound(
        row=row,
        integration_row=integration_row,
        runtime=runtime,
        action_text=action_text,
        button_id=button_id,
    )
    if arrival_result is not None:
        return arrival_result

    if is_operator_checkin_trigger(button_id=button_id, text=action_text):
        return _request_operator_checkin(
            row=row,
            integration_row=integration_row,
            runtime=runtime,
        )

    _send_operator_text(
        integration_row=integration_row,
        runtime=runtime,
        operator_wa_id=row.wa_id,
        body=_operator_help_text(),
    )
    return {"status": "help_sent"}
