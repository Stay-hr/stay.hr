"""Public token-scoped guest web check-in API (no GuestSerializer)."""

from __future__ import annotations

from django.http import Http404
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.reservations.checkin_readiness import CheckInReadinessDTO, slot_validation_results
from apps.reservations.document_expectations import expected_document_slots
from apps.reservations.guest_checkin_ocr import (
    field_confidence_for_slot,
    job_belongs_to_checkin_session,
    serialize_public_job,
)
from apps.reservations.guest_checkin_orchestrator import (
    GuestCheckInOrchestrator,
    GuestCheckInOrchestratorError,
)
from apps.reservations.guest_checkin_session import get_session_by_token
from apps.reservations.guest_checkin_web_ocr_service import (
    MAX_WEB_GUEST_FILES,
    collect_upload_files,
    create_web_guest_intake_job,
    max_web_guest_file_bytes,
    poll_and_apply_web_guest_job,
)
from apps.reservations.models import DocumentIntakeJob, Guest

_GUEST_PUBLIC_FIELDS = (
    "first_name",
    "last_name",
    "email",
    "phone",
    "date_of_birth",
    "document_number",
    "nationality",
    "sex",
    "address",
    "date_of_issue",
    "date_of_expiry",
    "issuing_authority",
    "personal_id_number",
    "document_additional_number",
    "additional_personal_id_number",
    "document_code",
    "document_type",
    "document_country",
    "document_country_iso2",
    "document_country_iso3",
    "document_country_numeric",
)


def _serialize_guest_fields(guest: Guest) -> dict:
    payload: dict = {}
    for key in _GUEST_PUBLIC_FIELDS:
        value = getattr(guest, key, None)
        if hasattr(value, "isoformat"):
            payload[key] = value.isoformat() if value else None
        else:
            payload[key] = value or ""
    return payload


def _serialize_readiness(readiness: CheckInReadinessDTO) -> dict:
    return {
        "status": readiness.status,
        "effective_status": readiness.effective_status,
        "required_slots": readiness.required_slots,
        "ready_slots": readiness.ready_slots,
        "can_complete": readiness.can_complete,
        "waiting_positions": list(readiness.waiting_positions),
        "ops_version": readiness.ops_version,
        "expected_checkin_adults": readiness.expected_checkin_adults,
        "adults_count": readiness.adults_count,
        "slots": [
            {
                "position": slot.position,
                "guest_id": slot.guest_id,
                "status": slot.status,
                "missing_fields": list(slot.missing_fields),
                "field_errors": dict(slot.field_errors),
            }
            for slot in readiness.slots
        ],
    }


def _serialize_progress(readiness: CheckInReadinessDTO) -> dict:
    return {
        "status": readiness.status,
        "effective_status": readiness.effective_status,
        "required_slots": readiness.required_slots,
        "ready_slots": readiness.ready_slots,
        "can_complete": readiness.can_complete,
        "ops_version": readiness.ops_version,
        "expected_checkin_adults": readiness.expected_checkin_adults,
        "adults_count": readiness.adults_count,
    }


def _slot_payload_from_readiness(
    readiness: CheckInReadinessDTO,
    *,
    reservation,
    position: int,
) -> dict:
    slot = next((item for item in readiness.slots if item.position == position), None)
    if slot is None:
        return {}
    guests_by_id = {guest.pk: guest for guest in expected_document_slots(reservation)}
    guest = guests_by_id.get(slot.guest_id)
    return {
        "position": slot.position,
        "guest_id": slot.guest_id,
        "status": slot.status,
        "missing_fields": list(slot.missing_fields),
        "field_errors": dict(slot.field_errors),
        "guest": _serialize_guest_fields(guest) if guest is not None else {},
    }


def _serialize_session(
    *,
    reservation,
    session,
    readiness: CheckInReadinessDTO,
) -> dict:
    guests_by_id = {guest.pk: guest for guest in expected_document_slots(reservation)}
    slots = []
    for slot in readiness.slots:
        guest = guests_by_id.get(slot.guest_id)
        slots.append(
            {
                "position": slot.position,
                "guest_id": slot.guest_id,
                "status": slot.status,
                "missing_fields": list(slot.missing_fields),
                "field_errors": dict(slot.field_errors),
                "guest": _serialize_guest_fields(guest) if guest is not None else {},
            }
        )

    return {
        **_serialize_readiness(readiness),
        "booking_code": reservation.booking_code,
        "property_name": reservation.property.name,
        "check_in": reservation.check_in.isoformat(),
        "check_out": reservation.check_out.isoformat(),
        "opens_at": session.opens_at.isoformat(),
        "expires_at": session.expires_at.isoformat(),
        "slots": slots,
    }


def _load_session_or_404(token):
    session = get_session_by_token(token)
    if session is None:
        raise Http404("Check-in session not found.")
    reservation = session.reservation
    return session, reservation


def _access_error_response(access) -> Response:
    payload: dict = {"status": access.gate_status}
    if access.opens_at is not None:
        payload["opens_at"] = access.opens_at.isoformat()
    return Response(payload, status=access.http_status)


def _serialize_web_guest_slot(*, reservation, position: int) -> dict:
    guests_by_id = {guest.pk: guest for guest in expected_document_slots(reservation)}
    slot = next(
        (item for item in slot_validation_results(reservation) if item.position == position),
        None,
    )
    if slot is None:
        return {}
    guest = guests_by_id.get(slot.guest_id)
    payload = {
        "position": position,
        "guest_id": slot.guest_id,
        "status": slot.status.value,
        "missing_fields": list(slot.missing_fields),
        "field_errors": dict(slot.field_errors),
        "guest": _serialize_guest_fields(guest) if guest is not None else {},
    }
    confidence = field_confidence_for_slot(reservation, position=position)
    if confidence:
        payload["field_confidence"] = confidence
    return payload


def _session_gate_or_response(session, reservation):
    readiness, access = GuestCheckInOrchestrator.get_readiness(session, reservation)
    if not access.allowed:
        return None, _access_error_response(access)
    return readiness, None


def _orchestrator_error_response(exc: GuestCheckInOrchestratorError, *, session=None) -> Response:
    if exc.code in {"not_open_yet", "completed", "expired", "revoked"}:
        payload: dict = {"status": exc.code}
        if exc.code == "not_open_yet" and session is not None and session.opens_at:
            payload["opens_at"] = session.opens_at.isoformat()
        if exc.message:
            payload["detail"] = exc.message
        return Response(payload, status=exc.http_status)
    payload = {"detail": exc.code, "status": exc.code}
    if exc.message:
        payload["detail"] = exc.message
        payload["error"] = exc.code
    payload.update(exc.payload)
    return Response(payload, status=exc.http_status)


class GuestCheckInSessionView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, token):
        session, reservation = _load_session_or_404(token)
        readiness, access = GuestCheckInOrchestrator.get_readiness(session, reservation)
        if not access.allowed:
            return _access_error_response(access)
        return Response(_serialize_session(reservation=reservation, session=session, readiness=readiness))


class GuestCheckInProgressView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, token):
        session, reservation = _load_session_or_404(token)
        readiness, access = GuestCheckInOrchestrator.get_readiness(session, reservation)
        if not access.allowed:
            return _access_error_response(access)
        return Response(_serialize_progress(readiness))


class GuestCheckInOccupancyView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def patch(self, request, token):
        session, reservation = _load_session_or_404(token)
        data = request.data if isinstance(request.data, dict) else {}
        if "expected_checkin_adults" not in data:
            return Response(
                {"detail": "expected_checkin_adults is required (integer or null)."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            result = GuestCheckInOrchestrator.patch_occupancy(
                session,
                reservation,
                expected_checkin_adults=data.get("expected_checkin_adults"),
                ops_version=data.get("ops_version"),
            )
        except GuestCheckInOrchestratorError as exc:
            if exc.code == "session_conflict":
                readiness, _ = GuestCheckInOrchestrator.get_readiness(session, reservation)
                payload = {
                    **_serialize_session(
                        reservation=reservation,
                        session=session,
                        readiness=readiness,
                    ),
                    "status": "session_conflict",
                    "error": "session_conflict",
                    "detail": exc.message or "session_conflict",
                }
                return Response(payload, status=exc.http_status)
            return _orchestrator_error_response(exc, session=session)

        return Response(
            _serialize_session(
                reservation=reservation,
                session=result.session,
                readiness=result.readiness,
            )
        )


class GuestCheckInSlotView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def patch(self, request, token, position: int):
        session, reservation = _load_session_or_404(token)
        try:
            result = GuestCheckInOrchestrator.patch_slot(
                session,
                reservation,
                position=position,
                fields=request.data if isinstance(request.data, dict) else {},
            )
        except GuestCheckInOrchestratorError as exc:
            return _orchestrator_error_response(exc, session=session)

        return Response(
            {
                **_serialize_progress(result.readiness),
                "slot": _slot_payload_from_readiness(
                    result.readiness, reservation=reservation, position=position
                ),
            }
        )


class GuestCheckInSlotCommitView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, token, position: int):
        session, reservation = _load_session_or_404(token)
        data = request.data if isinstance(request.data, dict) else {}
        try:
            result = GuestCheckInOrchestrator.commit_slot(
                session,
                reservation,
                position=position,
                ops_version=data.get("ops_version"),
            )
        except GuestCheckInOrchestratorError as exc:
            if exc.code == "session_conflict":
                readiness, _ = GuestCheckInOrchestrator.get_readiness(session, reservation)
                payload = {
                    **_serialize_progress(readiness),
                    "status": "session_conflict",
                    "error": "session_conflict",
                    "detail": exc.message or "session_conflict",
                    "ops_version": readiness.ops_version,
                }
                return Response(payload, status=exc.http_status)
            return _orchestrator_error_response(exc, session=session)

        return Response(
            {
                **_serialize_progress(result.readiness),
                "slot": _slot_payload_from_readiness(
                    result.readiness, reservation=reservation, position=position
                ),
            }
        )


class GuestCheckInCompleteView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, token):
        session, reservation = _load_session_or_404(token)
        data = request.data if isinstance(request.data, dict) else {}
        try:
            result = GuestCheckInOrchestrator.complete_session(
                session,
                reservation,
                ops_version=data.get("ops_version"),
                require_ops_version=True,
            )
        except GuestCheckInOrchestratorError as exc:
            return _orchestrator_error_response(exc, session=session)

        return Response(
            {
                "status": result.session.status,
                "effective_status": result.readiness.effective_status,
                "ops_version": result.readiness.ops_version,
                "completed_at": (
                    result.session.completed_at.isoformat()
                    if result.session.completed_at
                    else None
                ),
            },
            status=status.HTTP_200_OK,
        )


class GuestCheckInDocumentUploadView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, token, position: int):
        session, reservation = _load_session_or_404(token)
        readiness, error = _session_gate_or_response(session, reservation)
        if error is not None:
            return error

        files = collect_upload_files(request)
        if not files:
            return Response({"detail": "no_files"}, status=status.HTTP_400_BAD_REQUEST)
        if len(files) > MAX_WEB_GUEST_FILES:
            return Response({"detail": "too_many_files"}, status=status.HTTP_400_BAD_REQUEST)

        max_bytes = max_web_guest_file_bytes()
        for uploaded in files:
            if uploaded.size > max_bytes:
                return Response({"detail": "file_too_large"}, status=status.HTTP_400_BAD_REQUEST)

        slots = readiness.slots if readiness is not None else ()
        if position < 1 or position > len(slots):
            return Response({"detail": "invalid_position"}, status=status.HTTP_404_NOT_FOUND)

        job = create_web_guest_intake_job(
            session=session,
            reservation=reservation,
            position=position,
            files=files,
        )

        readiness, access = GuestCheckInOrchestrator.get_readiness(session, reservation)
        return Response(
            {
                **_serialize_progress(readiness),
                "job_id": job.pk,
                "job_status": job.status,
            },
            status=status.HTTP_201_CREATED,
        )


class GuestCheckInJobPollView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, token, job_id: int):
        session, reservation = _load_session_or_404(token)
        readiness, error = _session_gate_or_response(session, reservation)
        if error is not None:
            return error

        job = DocumentIntakeJob.objects.filter(pk=job_id).first()
        if job is None or not job_belongs_to_checkin_session(job, session=session):
            return Response({"detail": "job_not_found"}, status=status.HTTP_404_NOT_FOUND)

        position = int(job.guest_checkin_slot_position or 0)
        job = poll_and_apply_web_guest_job(
            session=session,
            reservation=reservation,
            job=job,
            position=position,
        )

        readiness, _ = GuestCheckInOrchestrator.get_readiness(session, reservation)
        payload = {
            **_serialize_progress(readiness),
            **serialize_public_job(job, reservation=reservation, position=position),
            "slot": _serialize_web_guest_slot(reservation=reservation, position=position),
        }
        http_status = status.HTTP_200_OK
        if payload.get("identity_status") == "duplicate_identity":
            http_status = status.HTTP_409_CONFLICT
        return Response(payload, status=http_status)
