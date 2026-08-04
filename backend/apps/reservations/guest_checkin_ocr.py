"""Web guest check-in OCR helpers (PR-B)."""

from __future__ import annotations

from typing import Any

from apps.reservations.document_intake_failure_reasons import OCRFailureReason
from apps.reservations.models import (
    DocumentIntakeJob,
    DocumentIntakeJobSource,
    DocumentIntakeJobStatus,
    GuestCheckInSession,
    Reservation,
)

_GUEST_PUBLIC_FIELDS = (
    "first_name",
    "last_name",
    "date_of_birth",
    "nationality",
    "sex",
    "document_number",
    "address",
    "document_type",
)

_PERSON_FIELD_BY_GUEST_FIELD = {
    "first_name": "given_names",
    "last_name": "surnames",
    "date_of_birth": "date_of_birth",
    "nationality": "nationality",
    "sex": "sex",
    "document_number": "document_number",
    "address": "address",
    "document_type": "document_type",
}

_NAME_REASONS = frozenset(
    {
        OCRFailureReason.UNKNOWN_PERSON.value,
        OCRFailureReason.FACE_ONLY.value,
    }
)
_DOCUMENT_REASONS = frozenset(
    {
        OCRFailureReason.NO_MRZ.value,
        OCRFailureReason.FRONT_NOT_FOUND.value,
        OCRFailureReason.BACK_NOT_FOUND.value,
    }
)


def _confidence_rank(value: str) -> int:
    return {"high": 3, "medium": 2, "low": 1}.get(value, 1)


def _min_confidence(*values: str) -> str:
    if not values:
        return "low"
    return min(values, key=_confidence_rank)


def _person_reasons(ocr_result: dict, person_index: int = 0) -> set[str]:
    telemetry = ocr_result.get("_telemetry") if isinstance(ocr_result, dict) else None
    if not isinstance(telemetry, dict):
        return set()
    persons = telemetry.get("persons")
    if not isinstance(persons, list):
        return set()
    for item in persons:
        if not isinstance(item, dict):
            continue
        if int(item.get("person_index", -1)) == person_index:
            reasons = item.get("reasons") or []
            return {str(reason) for reason in reasons if reason}
    return set()


def build_field_confidence(
    *,
    person: dict,
    ocr_result: dict,
    match: dict | None = None,
    person_index: int = 0,
) -> dict[str, str]:
    """Derive per-field confidence for public slot DTO (high / medium / low)."""
    match = match or {}
    base = {
        "high": "high",
        "medium": "medium",
        "low": "low",
        "none": "low",
    }.get(str(match.get("confidence") or "none"), "medium")
    reasons = _person_reasons(ocr_result, person_index=person_index)

    result: dict[str, str] = {}
    for guest_field in _GUEST_PUBLIC_FIELDS:
        person_field = _PERSON_FIELD_BY_GUEST_FIELD[guest_field]
        raw = person.get(person_field)
        value = str(raw or "").strip()
        conf = base
        if not value:
            conf = "low"
        elif guest_field in {"first_name", "last_name"} and reasons & _NAME_REASONS:
            conf = _min_confidence(conf, "low")
        elif guest_field in {"document_number", "date_of_birth", "nationality", "sex"} and reasons & _DOCUMENT_REASONS:
            conf = _min_confidence(conf, "medium")
        elif guest_field == "address" and not value:
            conf = "low"
        result[guest_field] = conf
    return result


def person_to_guest_preview(person: dict) -> dict[str, Any]:
    """Map OCR person dict to guest public field names for wizard pre-fill."""
    doc_type = str(person.get("document_type") or "national_id").lower()
    guest_doc_type = "passport" if doc_type == "passport" else "identity_card"
    sex_raw = str(person.get("sex") or "").strip().upper()
    if sex_raw in {"F", "FEMALE", "Ž", "Z"}:
        sex = "female"
    elif sex_raw in {"M", "MALE"}:
        sex = "male"
    else:
        sex = str(person.get("sex") or "").strip().lower()

    nat = str(person.get("nationality") or "").strip().upper()
    if len(nat) == 3:
        from apps.reservations.nationality_display import normalize_country_iso2

        iso2 = normalize_country_iso2(nat)
        nationality = iso2 or nat[:2]
    else:
        nationality = nat[:2]

    return {
        "first_name": str(person.get("given_names") or "").strip(),
        "last_name": str(person.get("surnames") or "").strip(),
        "email": "",
        "phone": "",
        "date_of_birth": str(person.get("date_of_birth") or "").strip() or None,
        "document_number": str(person.get("document_number") or "").strip(),
        "nationality": nationality,
        "sex": sex,
        "address": str(person.get("address") or "").strip(),
        "document_type": guest_doc_type,
    }


def latest_applied_web_guest_job(
    reservation: Reservation,
    *,
    position: int,
) -> DocumentIntakeJob | None:
    return (
        DocumentIntakeJob.objects.filter(
            reservation=reservation,
            source=DocumentIntakeJobSource.WEB_GUEST,
            guest_checkin_slot_position=position,
            status=DocumentIntakeJobStatus.APPLIED,
        )
        .order_by("-processed_at", "-id")
        .first()
    )


def field_confidence_for_slot(reservation: Reservation, position: int) -> dict[str, str]:
    job = latest_applied_web_guest_job(reservation, position=position)
    if job is None:
        return {}
    applied = job.applied_result or []
    if applied and isinstance(applied[0], dict):
        stored = applied[0].get("field_confidence")
        if isinstance(stored, dict):
            return {str(k): str(v) for k, v in stored.items()}
    persons = (job.ocr_result or {}).get("persons") or []
    if not persons or not isinstance(persons[0], dict):
        return {}
    matches = job.matches or []
    match = matches[0] if matches and isinstance(matches[0], dict) else {}
    return build_field_confidence(
        person=persons[0],
        ocr_result=job.ocr_result or {},
        match=match,
    )


def job_belongs_to_checkin_session(
    job: DocumentIntakeJob,
    *,
    session: GuestCheckInSession,
) -> bool:
    if job.source != DocumentIntakeJobSource.WEB_GUEST:
        return False
    if job.reservation_id != session.reservation_id:
        return False
    position = job.guest_checkin_slot_position
    if not position:
        return False
    return True


def serialize_public_job(
    job: DocumentIntakeJob,
    *,
    reservation: Reservation,
    position: int,
) -> dict[str, Any]:
    ocr_result = job.ocr_result if isinstance(job.ocr_result, dict) else {}
    persons = ocr_result.get("persons") if isinstance(ocr_result.get("persons"), list) else []
    person = persons[0] if persons and isinstance(persons[0], dict) else {}
    matches = job.matches if isinstance(job.matches, list) else []
    match = matches[0] if matches and isinstance(matches[0], dict) else {}
    applied_rows = job.applied_result if isinstance(job.applied_result, list) else []
    applied0 = applied_rows[0] if applied_rows and isinstance(applied_rows[0], dict) else {}

    payload: dict[str, Any] = {
        "job_id": job.pk,
        "status": job.status,
        "position": position,
        "error_message": job.error_message or "",
        "processed_at": job.processed_at.isoformat() if job.processed_at else None,
    }

    identity_status = (
        applied0.get("identity_status")
        or match.get("identity_status")
        or ""
    )
    if identity_status:
        payload["identity_status"] = identity_status
        existing_id = applied0.get("existing_guest_id") or match.get("existing_guest_id")
        if existing_id:
            payload["existing_guest_id"] = int(existing_id)
        existing_name = (
            applied0.get("existing_guest_name") or match.get("existing_guest_name") or ""
        )
        if existing_name:
            payload["existing_guest_name"] = existing_name
        if identity_status == "duplicate_identity" and existing_id:
            payload["detail"] = f"Document already belongs to guest #{existing_id}"
        elif identity_status == "already_processed":
            payload["detail"] = "Document already processed for this guest"
        elif identity_status == "mrz_inconsistent":
            payload["detail"] = "OCR fields inconsistent with MRZ"

    if job.status in {
        DocumentIntakeJobStatus.DONE,
        DocumentIntakeJobStatus.APPLIED,
    } and person and not identity_status:
        payload["guest_preview"] = person_to_guest_preview(person)
        payload["field_confidence"] = build_field_confidence(
            person=person,
            ocr_result=ocr_result,
            match=match,
        )
    elif job.status == DocumentIntakeJobStatus.APPLIED:
        applied = job.applied_result or []
        if applied and isinstance(applied[0], dict):
            stored = applied[0].get("field_confidence")
            if isinstance(stored, dict):
                payload["field_confidence"] = stored

    if job.status == DocumentIntakeJobStatus.APPLIED:
        payload["applied"] = True
    return payload
