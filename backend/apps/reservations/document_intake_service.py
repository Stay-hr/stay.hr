"""Process document intake jobs: OCR, match, apply to guests."""

from __future__ import annotations

import logging
import mimetypes
import time
from typing import Any

from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_date

from apps.ai.document_ocr import (
    DocumentOcrError,
    merge_persons,
    ocr_configured,
    run_document_batch_ocr,
    run_orphan_document_ocr,
)
from apps.reservations.document_intake_ocr_fixup import (
    fixup_document_ocr_result,
    normalize_document_number,
)
from apps.reservations.document_intake_context import DocumentIntakeContext
from apps.reservations.document_intake_identity import (
    REASON_ALREADY_PROCESSED,
    REASON_DUPLICATE_IDENTITY,
    REASON_MRZ_INCONSISTENT,
    classify_identity_collision,
    emit_identity_collision_audit,
    person_document_number,
    validate_person_against_mrz,
)
from apps.reservations.document_expectations import expected_document_count
from apps.reservations.document_intake_preprocess import (
    build_dropped_to_canonical_map,
    canonicalize_person_image_indices,
    dedupe_image_bytes,
    remap_ocr_indices_to_original,
    validate_no_dropped_references,
)
from apps.reservations.document_intake_pdf import (
    expand_bytes_for_ocr,
    remap_ocr_indices_to_source,
    source_indices_to_ocr_slots,
)
from apps.reservations.document_intake_face import crop_face_jpeg, _coerce_bbox_dict
from apps.reservations.document_intake_match import normalize_mrz_lines
from apps.reservations.document_intake_sides import find_id_document_for_side_merge
from apps.reservations.document_intake_telemetry import (
    attach_document_intake_telemetry,
    build_document_intake_telemetry,
)
from apps.reservations.guest_slots import ensure_guest_slots_for_intake
from apps.integrations.evisitor.residence_address import validate_evisitor_residence_address
from apps.reservations.mrz_parse import parse_sex_from_mrz
from apps.reservations.nationality_display import guest_nationality_iso2, normalize_country_iso2
from apps.reservations.document_photo_storage import (
    DOCUMENT_TYPE_NATIONAL_ID,
    DOCUMENT_TYPE_PASSPORT,
    document_photo_filename,
)
from apps.reservations.face_photo import guest_face_photo_url
from apps.reservations.models import (
    DocumentIntakeJob,
    DocumentIntakeJobStatus,
    DocumentScanLog,
    DocumentScanStatus,
    Guest,
    IdDocument,
    Reservation,
)

logger = logging.getLogger(__name__)


def _mime_for_path(path: str) -> str:
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "image/jpeg"


def should_run_orphan_pass(
    *,
    reservation: Reservation | None,
    persons: list[dict],
    images: list,
    ocr_result: dict | None = None,
) -> bool:
    """True when a second OCR pass on unassigned images may find missing guests."""
    from apps.reservations.document_intake_completeness import unassigned_image_indices

    if ocr_result and (ocr_result.get("_orphan_pass") or {}).get("ran"):
        return False
    if reservation is None:
        return False
    unassigned = unassigned_image_indices(persons=persons, image_count=len(images))
    expected_persons = expected_document_count(reservation)
    if len(unassigned) >= 2:
        return True
    return len(persons) < expected_persons and len(unassigned) >= 1


def _merge_ocr_images(existing: list, extra: list) -> list:
    by_index: dict[int, dict] = {}
    for item in existing + extra:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("index", -1))
        except (TypeError, ValueError):
            continue
        if idx >= 0:
            by_index[idx] = item
    return [by_index[k] for k in sorted(by_index)]


def process_document_intake_job(ctx: DocumentIntakeContext) -> None:
    job = ctx.job
    job_id = job.pk
    if job.status == DocumentIntakeJobStatus.APPLIED:
        return

    job.status = DocumentIntakeJobStatus.PROCESSING
    job.error_message = ""
    job.save(update_fields=["status", "error_message", "updated_at"])

    try:
        if not ocr_configured():
            raise DocumentOcrError("DOCUMENT_OCR_LLM_API_KEY is not configured")

        images = list(job.images.order_by("sort_order", "id"))
        if not images:
            raise DocumentOcrError("No images in job")

        bytes_list: list[bytes] = []
        mimes: list[str] = []
        for img in images:
            img.image.open("rb")
            try:
                bytes_list.append(img.image.read())
            finally:
                img.image.close()
            mimes.append(_mime_for_path(img.image.name))

        ocr_bytes, ocr_mimes, ocr_to_source = expand_bytes_for_ocr(bytes_list, mimes)

        unique_bytes, unique_to_ocr, dropped_dupes = dedupe_image_bytes(ocr_bytes)
        ocr_input_bytes = unique_bytes if unique_bytes else ocr_bytes
        ocr_input_mimes = (
            [ocr_mimes[i] for i in unique_to_ocr] if unique_to_ocr else ocr_mimes
        )

        ocr_result = fixup_document_ocr_result(
            run_document_batch_ocr(image_bytes_list=ocr_input_bytes, mime_types=ocr_input_mimes)
        )
        if unique_to_ocr and len(unique_to_ocr) < len(ocr_bytes):
            ocr_result = remap_ocr_indices_to_original(ocr_result, unique_to_ocr)
            preprocess = ocr_result.get("_preprocess") or {}
            if not isinstance(preprocess, dict):
                preprocess = {}
            preprocess["dropped_duplicate_indices"] = dropped_dupes
            ocr_result["_preprocess"] = preprocess
            if dropped_dupes:
                dropped_map = build_dropped_to_canonical_map(ocr_bytes, dropped_dupes)
                persons_raw = ocr_result.get("persons") or []
                if isinstance(persons_raw, list):
                    ocr_result["persons"] = canonicalize_person_image_indices(
                        persons_raw,
                        dropped_map,
                    )
                validate_no_dropped_references(ocr_result, dropped_dupes)
        ocr_result = remap_ocr_indices_to_source(ocr_result, ocr_to_source)
        preprocess = ocr_result.get("_preprocess") or {}
        if not isinstance(preprocess, dict):
            preprocess = {}
        preprocess["pdf_expand_source_count"] = len(bytes_list)
        preprocess["pdf_expand_ocr_count"] = len(ocr_bytes)
        ocr_result["_preprocess"] = preprocess

        persons = ocr_result.get("persons") or []
        if not isinstance(persons, list):
            persons = []

        reservation = ctx.reservation
        if reservation is None and job.reservation_id:
            reservation = Reservation.objects.prefetch_related("guests").get(pk=job.reservation_id)

        if should_run_orphan_pass(
            reservation=reservation,
            persons=persons,
            images=images,
            ocr_result=ocr_result,
        ):
            from apps.reservations.document_intake_completeness import unassigned_image_indices

            unassigned = unassigned_image_indices(persons=persons, image_count=len(images))
            ocr_orphan_indices = source_indices_to_ocr_slots(unassigned, ocr_to_source)
            orphan_result = fixup_document_ocr_result(
                run_orphan_document_ocr(
                    image_bytes_list=ocr_bytes,
                    mime_types=ocr_mimes,
                    orphan_indices=ocr_orphan_indices,
                )
            )
            orphan_result = remap_ocr_indices_to_source(orphan_result, ocr_to_source)
            extra_persons = orphan_result.get("persons") or []
            if extra_persons:
                persons = merge_persons(persons, extra_persons)
                ocr_result["persons"] = persons
            ocr_result["images"] = _merge_ocr_images(
                ocr_result.get("images") or [],
                orphan_result.get("images") or [],
            )
            ocr_result["_orphan_pass"] = {"ran": True, "indices": unassigned}

        from apps.reservations.document_intake_audit import audit_document_intake_persons

        persons, person_audit = audit_document_intake_persons(
            reservation=reservation,
            persons=persons,
            images=images,
            ocr_result=ocr_result,
        )
        if person_audit:
            ocr_result["persons"] = persons
            logger.info(
                "document intake person audit job_id=%s actions=%s",
                job_id,
                person_audit,
            )

        ocr_images = ocr_result.get("images") or []
        if isinstance(ocr_images, list):
            side_by_index = {
                int(item.get("index", -1)): str(item.get("side") or "")
                for item in ocr_images
                if isinstance(item, dict)
            }
            for img in images:
                side = side_by_index.get(img.sort_order, "")
                if side and img.detected_side != side:
                    img.detected_side = side
                    img.save(update_fields=["detected_side"])

        if persons:
            from apps.reservations.document_intake_audit import run_document_intake_matching_pipeline
            from apps.reservations.document_intake_web_guest import (
                is_web_guest_slot_forced_job,
                run_web_guest_matching_pipeline,
            )

            if is_web_guest_slot_forced_job(ctx):
                if reservation is None:
                    reservation = ctx.reservation
                matches = run_web_guest_matching_pipeline(ctx=ctx, persons=persons)
            elif ctx.is_reservation_scoped:
                if reservation is None:
                    reservation = ctx.reservation
                matches = run_document_intake_matching_pipeline(
                    tenant_id=ctx.effective_tenant_id,
                    reservation=reservation,
                    persons=persons,
                )
            else:
                matches = run_document_intake_matching_pipeline(
                    tenant_id=ctx.effective_tenant_id,
                    persons=persons,
                )
        else:
            matches = []

        telemetry = build_document_intake_telemetry(
            ocr_result=ocr_result,
            image_bytes_list=bytes_list,
            matches=matches,
            reservation=reservation if ctx.is_reservation_scoped else None,
            image_count=len(images),
            job_status=DocumentIntakeJobStatus.DONE,
        )
        ocr_result = attach_document_intake_telemetry(ocr_result, telemetry)

        job.ocr_result = ocr_result
        job.matches = matches
        job.status = DocumentIntakeJobStatus.DONE
        job.processed_at = timezone.now()
        job.save(
            update_fields=[
                "ocr_result",
                "matches",
                "status",
                "processed_at",
                "updated_at",
            ]
        )
    except DocumentOcrError as exc:
        _attach_failure_telemetry(
            ctx=ctx,
            job=job,
            bytes_list=locals().get("bytes_list"),
            images=locals().get("images"),
            reservation=locals().get("reservation"),
        )
        job.status = DocumentIntakeJobStatus.FAILED
        job.error_message = str(exc)
        job.processed_at = timezone.now()
        job.save(
            update_fields=[
                "ocr_result",
                "status",
                "error_message",
                "processed_at",
                "updated_at",
            ]
        )
    except Exception as exc:
        logger.exception("document intake job failed", extra={"job_id": job_id})
        _attach_failure_telemetry(
            ctx=ctx,
            job=job,
            bytes_list=locals().get("bytes_list"),
            images=locals().get("images"),
            reservation=locals().get("reservation"),
        )
        job.status = DocumentIntakeJobStatus.FAILED
        job.error_message = str(exc)[:500]
        job.processed_at = timezone.now()
        job.save(
            update_fields=[
                "ocr_result",
                "status",
                "error_message",
                "processed_at",
                "updated_at",
            ]
        )


def _attach_failure_telemetry(
    *,
    ctx: DocumentIntakeContext,
    job: DocumentIntakeJob,
    bytes_list: list[bytes] | None,
    images: list | None,
    reservation: Reservation | None,
) -> None:
    ocr_result = job.ocr_result if isinstance(job.ocr_result, dict) else {}
    telemetry = build_document_intake_telemetry(
        ocr_result=ocr_result,
        image_bytes_list=bytes_list,
        matches=[],
        reservation=reservation if ctx.is_reservation_scoped else None,
        image_count=len(images) if images else 0,
        job_status=DocumentIntakeJobStatus.FAILED,
    )
    job.ocr_result = attach_document_intake_telemetry(ocr_result, telemetry)


def _target_reservation_ids_from_matches(matches: list[dict]) -> set[int]:
    ids: set[int] = set()
    for match in matches:
        if match.get("reservation_id"):
            ids.add(int(match["reservation_id"]))
            continue
        for candidate in match.get("candidates") or []:
            if candidate.get("match_type") == "name":
                ids.add(int(candidate["reservation_id"]))
    return ids


def _prepare_intake_matches(
    *,
    ctx: DocumentIntakeContext,
    persons: list[dict],
    matches: list[dict],
) -> list[dict]:
    """Ensure guest slots and refresh matches before apply."""
    from apps.reservations.document_intake_audit import run_document_intake_matching_pipeline

    tenant_id = ctx.effective_tenant_id
    reservation_id = ctx.reservation.pk if ctx.is_reservation_scoped else None
    reservation_ids = _target_reservation_ids_from_matches(matches)
    if not reservation_ids and persons:
        reservation_ids = _target_reservation_ids_from_matches(
            run_document_intake_matching_pipeline(
                tenant_id=tenant_id,
                persons=persons,
                reservation_id=reservation_id,
            )
        )

    person_count = len(persons)
    for rid in reservation_ids:
        reservation = Reservation.objects.prefetch_related("guests").get(pk=rid)
        ensure_guest_slots_for_intake(
            tenant=reservation.tenant,
            reservation=reservation,
            min_count=person_count,
        )

    if ctx.is_reservation_scoped:
        return run_document_intake_matching_pipeline(
            tenant_id=tenant_id,
            reservation=ctx.reservation,
            persons=persons,
        )
    return run_document_intake_matching_pipeline(
        tenant_id=tenant_id,
        persons=persons,
        reservation_id=reservation_id,
    )


def apply_document_intake_job(
    ctx: DocumentIntakeContext,
    *,
    selections: list[dict[str, Any]] | None = None,
    device_id: str = "",
    request=None,
    whatsapp_reply: bool = True,
    allow_partial: bool = False,
) -> list[dict[str, Any]]:
    """Apply OCR results to guests. selections override auto matches.

    Updates guest fields, document photos, and scan log only — does not submit eVisitor.
    eVisitor check-in stays a separate manual step (POST .../evisitor-submit/).
    """
    job = ctx.job
    if job.status not in {DocumentIntakeJobStatus.DONE, DocumentIntakeJobStatus.APPLIED}:
        raise ValueError("Job is not ready for apply")

    persons = job.ocr_result.get("persons") or []
    matches = list(job.matches or [])
    images = list(job.images.order_by("sort_order", "id"))

    selection_map = {
        int(item["person_index"]): item
        for item in (selections or [])
        if isinstance(item, dict) and "person_index" in item
    }

    previously_applied_guest_ids = {
        int(item["guest_id"])
        for item in (job.applied_result or [])
        if isinstance(item, dict) and item.get("guest_id")
    }

    applied: list[dict[str, Any]] = []
    identity_outcomes: list[dict[str, Any]] = []

    with transaction.atomic():
        if ctx.is_reservation_scoped and ctx.reservation is not None:
            Reservation.objects.select_for_update().get(pk=ctx.reservation.pk)
            # Refresh guests under lock for identity classify.
            ctx.reservation.refresh_from_db()
            _ = list(ctx.reservation.guests.all())

        if not selection_map:
            from apps.reservations.document_intake_web_guest import (
                is_web_guest_slot_forced_job,
                run_web_guest_matching_pipeline,
            )

            if is_web_guest_slot_forced_job(ctx):
                matches = run_web_guest_matching_pipeline(ctx=ctx, persons=persons)
            else:
                matches = _prepare_intake_matches(
                    ctx=ctx,
                    persons=persons,
                    matches=matches,
                )
            job.matches = matches
            job.save(update_fields=["matches", "updated_at"])

        completeness = None
        if allow_partial and ctx.is_reservation_scoped:
            from apps.reservations.document_intake_completeness import evaluate_completeness

            reservation = ctx.reservation
            completeness = evaluate_completeness(
                reservation=reservation,
                persons=persons,
                matches=matches,
                images=images,
            )

        for match in matches:
            idx = int(match.get("person_index", -1))
            if idx < 0 or idx >= len(persons):
                continue

            person = persons[idx] if isinstance(persons[idx], dict) else {}
            sel = selection_map.get(idx)
            reservation_id = None
            guest_id = None

            if sel:
                reservation_id = sel.get("reservation_id")
                guest_id = sel.get("guest_id")
            elif match.get("auto_apply"):
                reservation_id = match.get("reservation_id")
                guest_id = match.get("guest_id")
            elif match.get("identity_status") in {
                REASON_ALREADY_PROCESSED,
                REASON_DUPLICATE_IDENTITY,
            }:
                identity_outcomes.append(
                    {
                        "person_index": idx,
                        "guest_id": match.get("guest_id") or match.get("existing_guest_id"),
                        "reservation_id": match.get("reservation_id"),
                        "identity_status": match.get("identity_status"),
                        "reject_reason": match.get("reject_reason"),
                        "existing_guest_id": match.get("existing_guest_id"),
                        "existing_guest_name": match.get("existing_guest_name") or "",
                        "face_photo_saved": False,
                        "updated_fields": [],
                    }
                )
                continue

            if not reservation_id or not guest_id:
                continue

            if not sel and int(guest_id) in previously_applied_guest_ids:
                continue

            if allow_partial and completeness is not None and not sel:
                guest_id_int = int(guest_id)
                if any(g.guest_id == guest_id_int for g in completeness.missing_sides):
                    continue

            guest = Guest.objects.select_related("reservation").get(
                pk=guest_id,
                reservation_id=reservation_id,
                tenant_id=ctx.effective_tenant_id,
            )
            reservation = guest.reservation

            # Re-classify under lock (defense in depth for races / WEB_GUEST).
            collision = classify_identity_collision(
                reservation=reservation,
                person=person,
                target_guest_id=guest.pk,
            )
            if collision.status != "none":
                emit_identity_collision_audit(
                    reservation_id=reservation.pk,
                    job_id=job.pk,
                    existing_guest_id=collision.existing_guest_id,
                    target_guest_id=guest.pk,
                    document_number=person_document_number(person),
                    reason=collision.reason,
                )
                identity_outcomes.append(
                    {
                        "person_index": idx,
                        "guest_id": collision.existing_guest_id or guest.pk,
                        "reservation_id": reservation.pk,
                        "identity_status": collision.status,
                        "reject_reason": collision.reason,
                        "existing_guest_id": collision.existing_guest_id,
                        "existing_guest_name": (
                            collision.existing_guest.name
                            if collision.existing_guest is not None
                            else ""
                        ),
                        "face_photo_saved": False,
                        "updated_fields": [],
                    }
                )
                continue

            mrz_mismatches = validate_person_against_mrz(person)
            if mrz_mismatches:
                emit_identity_collision_audit(
                    reservation_id=reservation.pk,
                    job_id=job.pk,
                    existing_guest_id=guest.pk,
                    target_guest_id=guest.pk,
                    document_number=person_document_number(person),
                    reason=REASON_MRZ_INCONSISTENT,
                )
                identity_outcomes.append(
                    {
                        "person_index": idx,
                        "guest_id": guest.pk,
                        "reservation_id": reservation.pk,
                        "identity_status": REASON_MRZ_INCONSISTENT,
                        "reject_reason": REASON_MRZ_INCONSISTENT,
                        "mrz_mismatches": mrz_mismatches,
                        "face_photo_saved": False,
                        "updated_fields": [],
                    }
                )
                continue

            try:
                with transaction.atomic():
                    result = _apply_person_to_guest(
                        person=person,
                        person_index=idx,
                        guest=guest,
                        reservation=reservation,
                        images=images,
                        device_id=device_id or job.device_id,
                    )
            except IntegrityError:
                reservation.refresh_from_db()
                collision = classify_identity_collision(
                    reservation=reservation,
                    person=person,
                    target_guest_id=guest.pk,
                )
                if collision.status == "none":
                    collision = classify_identity_collision(
                        reservation=reservation,
                        person=person,
                        target_guest_id=None,
                    )
                reason = collision.reason or REASON_DUPLICATE_IDENTITY
                emit_identity_collision_audit(
                    reservation_id=reservation.pk,
                    job_id=job.pk,
                    existing_guest_id=collision.existing_guest_id,
                    target_guest_id=guest.pk,
                    document_number=person_document_number(person),
                    reason=reason,
                )
                identity_outcomes.append(
                    {
                        "person_index": idx,
                        "guest_id": collision.existing_guest_id or guest.pk,
                        "reservation_id": reservation.pk,
                        "identity_status": collision.status
                        if collision.status != "none"
                        else REASON_DUPLICATE_IDENTITY,
                        "reject_reason": reason,
                        "existing_guest_id": collision.existing_guest_id,
                        "existing_guest_name": (
                            collision.existing_guest.name
                            if collision.existing_guest is not None
                            else ""
                        ),
                        "face_photo_saved": False,
                        "updated_fields": [],
                    }
                )
                continue

            if request is not None:
                result["face_photo_url"] = guest_face_photo_url(guest, request)
            applied.append(result)

        outcomes = applied + identity_outcomes
        if outcomes:
            merged = list(job.applied_result or [])
            # Identity rejects keyed by person_index; successful applies by guest_id.
            by_key: dict[str, dict] = {}
            for item in merged:
                if not isinstance(item, dict):
                    continue
                if item.get("identity_status"):
                    by_key[f"person:{item.get('person_index')}"] = item
                elif item.get("guest_id"):
                    by_key[f"guest:{item['guest_id']}"] = item
            for item in outcomes:
                if item.get("identity_status"):
                    by_key[f"person:{item.get('person_index')}"] = item
                elif item.get("guest_id"):
                    by_key[f"guest:{item['guest_id']}"] = item
            job.applied_result = list(by_key.values())

        if allow_partial and ctx.is_reservation_scoped:
            if completeness is None:
                from apps.reservations.document_intake_completeness import evaluate_completeness

                reservation = ctx.reservation
                completeness = evaluate_completeness(
                    reservation=reservation,
                    persons=persons,
                    matches=matches,
                    images=images,
                )
            if completeness.is_complete and applied:
                job.status = DocumentIntakeJobStatus.APPLIED
            else:
                job.status = DocumentIntakeJobStatus.DONE
        elif applied and not identity_outcomes:
            job.status = DocumentIntakeJobStatus.APPLIED
        elif applied:
            job.status = DocumentIntakeJobStatus.APPLIED
        else:
            # Identity-only outcomes: keep DONE so poll can surface identity_status.
            job.status = DocumentIntakeJobStatus.DONE
        job.save(update_fields=["applied_result", "status", "updated_at"])

    if whatsapp_reply:
        from apps.integrations.whatsapp.apply_reply import maybe_send_document_apply_whatsapp_reply

        maybe_send_document_apply_whatsapp_reply(ctx, applied=applied)

    return applied + identity_outcomes



def _intake_image_side(img) -> str:
    side = str(getattr(img, "detected_side", "") or "").strip().lower()
    if side in {"front", "back", "passport"}:
        return side
    return "unknown"


def _resolve_side_images(
    images: list,
    front_idx: Any,
    back_idx: Any,
    *,
    is_passport: bool = False,
) -> tuple[Any | None, Any | None, bool, bool]:
    """Map person image indices to front/back rows; honor detected_side on intake images."""
    front_img = _image_at_index(images, front_idx)
    back_img = _image_at_index(images, back_idx)

    if front_img is not None and _intake_image_side(front_img) == "back" and back_img is None:
        back_img = front_img
        front_img = None
    elif back_img is not None and _intake_image_side(back_img) in {"front", "passport"} and front_img is None:
        front_img = back_img
        back_img = None
    elif (
        front_img is not None
        and back_img is not None
        and _intake_image_side(front_img) == "back"
        and _intake_image_side(back_img) in {"front", "passport"}
    ):
        front_img, back_img = back_img, front_img

    # Passport biodata is stored as front_photo; OCR often assigns the only image to back_index.
    if is_passport and front_img is None and back_img is not None:
        front_img = back_img
        back_img = None

    applying_front = front_img is not None
    applying_back = back_img is not None
    return front_img, back_img, applying_front, applying_back


def _apply_person_to_guest(
    *,
    person: dict,
    person_index: int,
    guest: Guest,
    reservation: Reservation,
    images: list,
    device_id: str,
) -> dict[str, Any]:
    started = time.perf_counter()
    doc_type = str(person.get("document_type") or "national_id").lower()
    tip = "passport" if doc_type == "passport" else "national_id"

    front_idx = person.get("front_image_index")
    back_idx = person.get("back_image_index")
    front_img, back_img, applying_front, applying_back = _resolve_side_images(
        images, front_idx, back_idx, is_passport=(tip == "passport"),
    )

    raw_payload = _build_scan_payload(person=person, device_id=device_id, tip=tip)
    guest_updates, suggested = _guest_updates_from_payload(raw_payload)

    scan_status = DocumentScanStatus.OK if guest_updates else DocumentScanStatus.FAILED
    error_message = "" if guest_updates else "Ne mogu mapirati OCR u polja gosta."

    scan_log = DocumentScanLog.objects.create(
        tenant_id=guest.tenant_id,
        reservation_id=reservation.pk,
        guest=guest,
        status=scan_status,
        method="OCR",
        device_id=device_id,
        scanned_at=timezone.now(),
        duration_ms=int((time.perf_counter() - started) * 1000),
        raw_payload=raw_payload,
        suggested_fields=suggested,
        corrected_fields={},
        error_message=error_message,
    )

    id_document_id = None
    if guest_updates:
        for field, value in guest_updates.items():
            setattr(guest, field, value)
        guest.save(update_fields=list(guest_updates.keys()) + ["updated_at", "name"])

    id_document = find_id_document_for_side_merge(
        guest=guest,
        tip=tip,
        applying_front=applying_front,
        applying_back=applying_back,
    )
    if id_document is None:
        id_document = IdDocument.objects.create(
            guest=guest,
            image_path="",
            extracted_payload={"source": "document_intake", "person": person},
        )
    else:
        payload = id_document.extracted_payload if isinstance(id_document.extracted_payload, dict) else {}
        payload = {**payload, "source": "document_intake", "person": person}
        id_document.extracted_payload = payload
    id_document_id = id_document.id
    id_document._passport_photo = tip == "passport"

    if front_img is not None:
        front_name = document_photo_filename(
            guest_id=guest.id,
            document_type=tip,
            side="front",
        )
        front_img.image.open("rb")
        try:
            id_document.front_photo.save(front_name, front_img.image, save=False)
        finally:
            front_img.image.close()

    if back_img is not None:
        back_name = document_photo_filename(
            guest_id=guest.id,
            document_type=tip,
            side="back",
        )
        back_img.image.open("rb")
        try:
            id_document.back_photo.save(back_name, back_img.image, save=False)
        finally:
            back_img.image.close()

    face_content = None
    if front_img is not None and front_img.image:
        face_content = crop_face_jpeg(
            front_img.image.path,
            _coerce_bbox_dict(person.get("face_bbox")),
        )

    if face_content is not None:
        id_document.face_photo.save(f"guest_{guest.pk}_face.jpg", face_content, save=False)

    id_document.save()

    guest.document_type = "Putovnica" if tip == "passport" else "Osobna iskaznica"
    guest.save(update_fields=["document_type", "updated_at", "name"])

    _sync_reservation_country_from_guest(guest=guest, reservation=reservation)

    return {
        "person_index": person_index,
        "guest_id": guest.pk,
        "reservation_id": reservation.pk,
        "guest_name": guest.name or f"{guest.first_name} {guest.last_name}".strip(),
        "scan_log_id": scan_log.pk,
        "scan_status": scan_status,
        "id_document_id": id_document_id,
        "updated_fields": list(guest_updates.keys()),
        "face_photo_saved": face_content is not None,
    }


def _sync_reservation_country_from_guest(*, guest: Guest, reservation: Reservation) -> None:
    """Set booker_country / primary guest so Hospira can show the flag."""
    guest_fields: list[str] = []
    if not guest.is_primary and not reservation.guests.filter(is_primary=True).exists():
        guest.is_primary = True
        guest_fields.append("is_primary")

    iso2 = guest_nationality_iso2(guest)
    reservation_fields: list[str] = []
    if iso2:
        if guest.is_primary or not (reservation.booker_country or "").strip():
            reservation.booker_country = iso2
            reservation_fields.append("booker_country")

    if guest_fields:
        guest.save(update_fields=guest_fields + ["updated_at"])
    if reservation_fields:
        reservation.save(update_fields=reservation_fields + ["updated_at"])


def _image_at_index(images: list, index: Any):
    if index is None:
        return None
    try:
        idx = int(index)
    except (TypeError, ValueError):
        return None
    if idx < 0 or idx >= len(images):
        return None
    return images[idx]


def _build_scan_payload(*, person: dict, device_id: str, tip: str) -> dict:
    mrz = normalize_mrz_lines(person)
    nat = str(person.get("nationality") or "").upper()[:3]
    issue = nat

    return {
        "metapodaci": {
            "metoda_ocitanja": "OCR",
            "vrijeme_skeniranja": timezone.now().isoformat(),
            "uredaj_id": device_id,
            "tip_dokumenta": tip,
        },
        "podaci_gosta": {
            "ime": str(person.get("given_names") or "").strip(),
            "prezime": str(person.get("surnames") or "").strip(),
            "broj_dokumenta": str(person.get("document_number") or "").strip(),
            "drzava_izdavanja": issue,
            "datum_rodenja": str(person.get("date_of_birth") or "").strip(),
            "datum_isteka": str(person.get("date_of_expiry") or "").strip(),
            "spol": str(person.get("sex") or "").strip(),
            "drzavljanstvo": nat,
            "adresa": str(person.get("address") or "").strip(),
        },
        "biometrija": {"fotografija_b64": "", "potpis_b64": ""},
        "sirovi_mrz": mrz,
    }


def _guest_updates_from_payload(raw_payload: dict) -> tuple[dict, dict]:
    guest_data = raw_payload.get("podaci_gosta") or {}

    def as_str(key: str) -> str:
        val = guest_data.get(key)
        return str(val).strip() if val is not None else ""

    updates: dict = {}
    first_name = as_str("ime")
    last_name = as_str("prezime")
    if first_name:
        updates["first_name"] = first_name
    if last_name:
        updates["last_name"] = last_name

    doc_no = normalize_document_number(as_str("broj_dokumenta"))
    if doc_no:
        updates["document_number"] = doc_no

    sex = as_str("spol")
    if sex:
        updates["sex"] = sex

    dob = as_str("datum_rodenja")
    if dob:
        parsed = parse_date(dob)
        if parsed:
            updates["date_of_birth"] = parsed

    doe = as_str("datum_isteka")
    if doe:
        parsed = parse_date(doe)
        if parsed:
            updates["date_of_expiry"] = parsed

    nat_raw = as_str("drzavljanstvo").upper()
    iso2 = normalize_country_iso2(nat_raw)
    if iso2:
        updates["nationality"] = iso2

    issue_iso3 = as_str("drzava_izdavanja").upper()[:3]
    if issue_iso3:
        updates["document_country_iso3"] = issue_iso3
        issue_iso2 = normalize_country_iso2(issue_iso3)
        if issue_iso2:
            updates["document_country_iso2"] = issue_iso2

    adresa = as_str("adresa")
    address_for_suggest = adresa
    if adresa:
        address_result = validate_evisitor_residence_address(adresa)
        if address_result.valid:
            updates["address"] = address_result.normalized_address
            address_for_suggest = address_result.normalized_address
        # Invalid: do not persist bad address; keep prior guest.address.

    meta = raw_payload.get("metapodaci") or {}
    tip = str(meta.get("tip_dokumenta") or "").lower()
    if tip == "passport":
        updates["document_type"] = "Putovnica"
    elif tip == "national_id":
        updates["document_type"] = "Osobna iskaznica"

    mrz = str(raw_payload.get("sirovi_mrz") or "").strip()
    if mrz:
        updates["mrz_raw_text"] = mrz
        updates["mrz_verified"] = True
        if not updates.get("sex"):
            mrz_sex = parse_sex_from_mrz(mrz)
            if mrz_sex:
                updates["sex"] = mrz_sex

    suggested = {
        "first_name": first_name,
        "last_name": last_name,
        "document_number": doc_no,
        "nationality": iso2,
        "date_of_birth": dob,
        "address": address_for_suggest,
    }
    suggested = {k: v for k, v in suggested.items() if v}
    return updates, suggested


def format_ocr_summary(ocr_result: dict[str, Any]) -> str:
    """Plain-text dump of OCR output for reception review / manual parsing."""
    if not ocr_result:
        return ""

    lines: list[str] = []

    images = ocr_result.get("images") or []
    if isinstance(images, list):
        for img in images:
            if not isinstance(img, dict):
                continue
            idx = img.get("index", "?")
            side = img.get("side") or "unknown"
            lines.append(f"=== Slika {idx} ({side}) ===")
            ocr_text = (img.get("ocr_text") or "").strip()
            if ocr_text:
                lines.append(ocr_text)
            mrz = img.get("mrz_lines") or []
            if isinstance(mrz, list) and mrz:
                lines.append("MRZ:")
                lines.extend(str(line) for line in mrz if str(line).strip())
            lines.append("")

    persons = ocr_result.get("persons") or []
    if isinstance(persons, list):
        for i, person in enumerate(persons):
            if not isinstance(person, dict):
                continue
            lines.append(f"=== Osoba {i + 1} (parsirano) ===")
            field_labels = (
                ("given_names", "Ime"),
                ("surnames", "Prezime"),
                ("document_number", "Broj dokumenta"),
                ("nationality", "Nacionalnost"),
                ("date_of_birth", "Datum rođenja"),
                ("date_of_expiry", "Vrijedi do"),
                ("sex", "Spol"),
                ("address", "Adresa"),
                ("document_type", "Tip dokumenta"),
            )
            for key, label in field_labels:
                value = (person.get(key) or "").strip() if person.get(key) is not None else ""
                if value:
                    lines.append(f"{label}: {value}")
            mrz = person.get("mrz_lines") or []
            if isinstance(mrz, list) and mrz:
                lines.append("MRZ (osoba):")
                lines.extend(str(line) for line in mrz if str(line).strip())
            lines.append("")

    return "\n".join(lines).strip()


def completeness_to_dict(completeness) -> dict[str, Any]:
    return {
        "is_complete": completeness.is_complete,
        "missing_guests": [
            {
                "guest_id": item.guest_id,
                "guest_name": item.guest_name,
                "adult_ordinal": item.adult_ordinal,
            }
            for item in completeness.missing_guests
        ],
        "missing_sides": [
            {
                "guest_id": item.guest_id,
                "guest_name": item.guest_name,
                "side": item.side,
                "is_passport": item.is_passport,
            }
            for item in completeness.missing_sides
        ],
        "unmatched_persons": [
            {
                "person_index": item.person_index,
                "display_name": item.display_name,
            }
            for item in completeness.unmatched_persons
        ],
        "unassigned_image_indices": list(completeness.unassigned_image_indices),
        "unassigned_image_count": len(completeness.unassigned_image_indices),
        "ocr_under_extracted": completeness.ocr_under_extracted,
    }


def job_to_dict(job: DocumentIntakeJob, *, request=None) -> dict[str, Any]:
    ctx = DocumentIntakeContext.from_job(job)
    ocr_result = job.ocr_result or {}
    data: dict[str, Any] = {
        "job_id": job.pk,
        "status": job.status,
        "image_count": job.images.count(),
        "error_message": job.error_message or "",
        "processed_at": job.processed_at.isoformat() if job.processed_at else None,
        "ocr_result": ocr_result,
        "ocr_summary": format_ocr_summary(ocr_result),
        "matches": job.matches or [],
        "applied": job.applied_result or [],
        "created_at": job.created_at.isoformat(),
        "source": job.source or "",
        "reservation_id": job.reservation_id,
        "whatsapp_message_id": job.whatsapp_message_id,
    }
    persons = ocr_result.get("persons") if isinstance(ocr_result.get("persons"), list) else []
    if ctx.is_reservation_scoped:
        reservation = ctx.reservation
        from apps.reservations.document_intake_completeness import evaluate_completeness

        images = list(job.images.order_by("sort_order", "id"))
        completeness = evaluate_completeness(
            reservation=reservation,
            persons=persons,
            matches=job.matches or [],
            images=images,
        )
        data["completeness"] = completeness_to_dict(completeness)
        data["unassigned_image_count"] = len(completeness.unassigned_image_indices)
        data["unassigned_image_indices"] = list(completeness.unassigned_image_indices)
    if request is not None and job.applied_result:
        for item in data["applied"]:
            guest_id = item.get("guest_id")
            reservation_id = item.get("reservation_id")
            if not guest_id or not reservation_id:
                continue
            try:
                guest = Guest.objects.get(pk=guest_id, reservation_id=reservation_id)
                item["face_photo_url"] = guest_face_photo_url(guest, request)
            except Guest.DoesNotExist:
                pass
    return data
