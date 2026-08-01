from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
import os
import queue
import time
import uuid
from datetime import date as date_type
from datetime import datetime
from datetime import timezone as dt_timezone

from django.conf import settings
from django.core.files.base import ContentFile
from django.db.models import Case, Count, DateTimeField, F, Prefetch, Q, When
from django.http import FileResponse, StreamingHttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import generics, serializers, status
from rest_framework.exceptions import NotFound
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.api.permissions import DenyAdminScopes, HasReceptionAccess
from apps.api.reception_serializers import (
    GuestCreateSerializer,
    GuestDetailSerializer,
    ReservationTimelineSerializer,
    ReservationUpdateSerializer,
)
from apps.api.request_context import installation_id_from_request
from apps.api.views import TenantAPIView
from apps.integrations.evisitor.eligibility import guest_requires_evisitor
from apps.integrations.evisitor.exceptions import (
    EvisitorApiError,
    EvisitorConfigError,
    EvisitorValidationError,
)
from apps.integrations.evisitor.service import submit_guest_checkin, submit_guest_checkout
from apps.properties.models import Property
from apps.properties.resolution import PropertyResolutionError, resolve_property_for_tenant
from apps.reservations.booking_pdf_import import parse_booking_pdf
from apps.reservations.booking_xls_import import upsert_reservation_from_xls_row
from apps.reservations.document_photo_storage import (
    DOCUMENT_TYPE_NATIONAL_ID,
    DOCUMENT_TYPE_PASSPORT,
    document_photo_filename,
    id_recognition_sample_filename,
)
from apps.reservations.face_photo import guest_face_photo_document
from apps.reservations.mrz_parse import normalize_residence_address, parse_sex_from_mrz
from apps.reservations.nationality_display import normalize_country_iso2
from apps.reservations.query import property_day_range
from apps.reservations.models import (
    DocumentScanLog,
    DocumentScanStatus,
    EvisitorGuestStatus,
    Guest,
    IdDocument,
    IdRecognitionSample,
    IdRecognitionSampleSource,
    Reservation,
    ReservationUnit,
)
from apps.reservations.statistics import (
    aggregate_guest_countries_statistics,
    aggregate_monthly_statistics,
)
from apps.reservations.sync_versions import (
    RESERVATION_VERSION_SCOPE_ALL,
    build_scoped_versions_payload,
    build_sync_versions_payload,
    sync_versions_etag,
    valid_reservation_version_scopes,
)
from apps.reservations.reservation_version_event_bus import (
    get_reservation_version_event_bus,
)
from apps.reservations.reservation_version_events import (
    check_sse_invariant,
    close_sse_stream,
    format_sse,
    get_sse_connection_stats,
    record_sse_stream_closed,
    register_sse_stream,
    touch_sse_stream,
)
from apps.core.system_status import build_system_status_payload


logger = logging.getLogger(__name__)


class ReceptionReadView(TenantAPIView):
    required_scopes = ["reception:read"]
    permission_classes = [HasReceptionAccess, DenyAdminScopes]


class ReceptionWriteView(TenantAPIView):
    required_scopes = ["reception:write"]
    permission_classes = [HasReceptionAccess, DenyAdminScopes]


def _reservation_queryset(tenant):
    return (
        Reservation.objects.for_tenant(tenant)
        .select_related("invoice")
        .annotate(guests_count=Count("guests", distinct=True))
        .prefetch_related(
            Prefetch("guests", queryset=Guest.objects.order_by("-is_primary", "id")),
            _guest_face_photo_prefetch(),
            Prefetch(
                "units",
                queryset=ReservationUnit.objects.select_related("unit").order_by(
                    "sort_order", "id"
                ),
            ),
        )
    )


def _guest_face_photo_prefetch() -> Prefetch:
    return Prefetch(
        "guests__id_documents",
        queryset=IdDocument.objects.filter(face_photo__isnull=False)
        .exclude(face_photo="")
        .order_by("-created_at"),
    )


def _guest_id_documents_prefetch() -> Prefetch:
    return Prefetch(
        "id_documents",
        queryset=IdDocument.objects.filter(face_photo__isnull=False)
        .exclude(face_photo="")
        .order_by("-created_at"),
    )


def _get_reservation(tenant, reservation_id: int) -> Reservation:
    reservation = (
        Reservation.objects.for_tenant(tenant).filter(pk=reservation_id).first()
    )
    if reservation is None:
        raise NotFound("Rezervacija nije pronađena.")
    return reservation


def _get_guest(tenant, reservation_id: int, guest_id: int) -> Guest:
    guest = (
        Guest.objects.for_tenant(tenant)
        .filter(pk=guest_id, reservation_id=reservation_id)
        .first()
    )
    if guest is None:
        raise NotFound("Gost nije pronađen.")
    return guest


class ReceptionHealthView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        return Response({"service": "reception", "status": "ok"})


class ReceptionSystemStatusView(ReceptionReadView):
    """Operational metrics — requires reception:read (not public like /health/)."""

    def get(self, request):
        return Response(build_system_status_payload())


def _sse_client_ip(request) -> str:
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


class ReceptionSyncVersionsView(ReceptionReadView):
    def get(self, request):
        year_param = request.query_params.get("year")
        if year_param is None or str(year_param).strip() == "":
            year = timezone.localdate().year
        else:
            try:
                year = int(year_param)
            except (TypeError, ValueError):
                return Response(
                    {"detail": "year mora biti cijeli broj."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if year < 2000 or year > 2100:
                return Response(
                    {"detail": "year izvan dopuštenog raspona."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        reservation_id_param = request.query_params.get("reservation_id")
        reservation_id = None
        if reservation_id_param is not None and str(reservation_id_param).strip() != "":
            try:
                reservation_id = int(reservation_id_param)
            except (TypeError, ValueError):
                return Response(
                    {"detail": "reservation_id mora biti cijeli broj."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if reservation_id < 1:
                return Response(
                    {"detail": "reservation_id mora biti pozitivan."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        scope_param = request.query_params.get("scope")
        scope = None
        if scope_param is not None and str(scope_param).strip() != "":
            scope = str(scope_param).strip().lower()
            if scope not in valid_reservation_version_scopes():
                return Response(
                    {"detail": "scope nije podržan."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if reservation_id is None:
                return Response(
                    {"detail": "reservation_id je obavezan kad je scope naveden."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        if scope is not None:
            payload = build_scoped_versions_payload(
                request.tenant,
                reservation_id,
                scope,
            )
        else:
            payload = build_sync_versions_payload(
                request.tenant,
                year,
                reservation_id=reservation_id,
                include_versions=reservation_id is not None,
            )
        if payload is None:
            return Response(status=status.HTTP_404_NOT_FOUND)

        etag = sync_versions_etag(payload)
        if request.META.get("HTTP_IF_NONE_MATCH") == etag:
            return Response(status=status.HTTP_304_NOT_MODIFIED)

        response = Response(payload)
        response["ETag"] = etag
        return response


SSE_HEARTBEAT_SECONDS = 25
# WSGI sync cannot observe a readable-closed socket without a write. Disconnect
# is detected on the next heartbeat/event yield (BrokenPipe / GeneratorExit),
# so close follows within one heartbeat interval after the client is gone.
#
# Permanent lifecycle instrumentation (ADR 0005): stream_id registry, opened/closed
# logs, check_sse_invariant, X-SSE-Stream-Id — keep through Redis (2a) and Uvicorn (2b).


class ReceptionReservationVersionStreamView(ReceptionReadView):
    """SSE stream for scoped reservation version changes (v2 transport).

    Lifecycle instrumentation (registry, invariant, structured logs) is permanent —
    do not strip when introducing Redis EventBus or a dedicated Uvicorn SSE service.
    """

    def get(self, request):
        reservation_id_param = request.query_params.get("reservation_id")
        scope_param = request.query_params.get("scope")

        if reservation_id_param is None or str(reservation_id_param).strip() == "":
            return Response(
                {"detail": "reservation_id je obavezan."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if scope_param is None or str(scope_param).strip() == "":
            return Response(
                {"detail": "scope je obavezan."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            reservation_id = int(reservation_id_param)
        except (TypeError, ValueError):
            return Response(
                {"detail": "reservation_id mora biti cijeli broj."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if reservation_id < 1:
            return Response(
                {"detail": "reservation_id mora biti pozitivan."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        scope = str(scope_param).strip().lower()
        if scope == RESERVATION_VERSION_SCOPE_ALL:
            return Response(
                {"detail": "scope=all nije podržan za stream."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if scope not in valid_reservation_version_scopes():
            return Response(
                {"detail": "scope nije podržan."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        payload = build_scoped_versions_payload(request.tenant, reservation_id, scope)
        if payload is None:
            return Response(status=status.HTTP_404_NOT_FOUND)

        max_workers = int(os.environ.get("GUNICORN_WORKERS", "8"))
        active_sse = int(get_sse_connection_stats().get("active_connections") or 0)
        reserve = max(max_workers - 1, 1)
        if active_sse >= reserve:
            logger.warning(
                "sse_stream_rejected reservation_id=%s scope=%s active=%s reserve=%s",
                reservation_id,
                scope,
                active_sse,
                reserve,
            )
            return Response(
                {"detail": "SSE capacity saturated; use poll fallback."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        event_bus = get_reservation_version_event_bus()
        event_queue = event_bus.subscribe(reservation_id, scope)
        initial_version = payload["versions"][scope]
        worker_pid = os.getpid()
        client_ip = _sse_client_ip(request)
        stream_id = uuid.uuid4().hex
        opened_at = datetime.now(dt_timezone.utc).isoformat()
        register_sse_stream(
            stream_id,
            reservation_id=reservation_id,
            scope=scope,
            worker_pid=worker_pid,
            opened_at=opened_at,
        )
        check_sse_invariant()

        def event_stream():
            started_at = time.monotonic()
            close_reason = "unknown"
            logger.info(
                "sse_stream_opened stream_id=%s reservation_id=%s scope=%s "
                "worker_pid=%s client=%s opened_at=%s",
                stream_id,
                reservation_id,
                scope,
                worker_pid,
                client_ip,
                opened_at,
            )
            try:
                yield format_sse(
                    "connected",
                    {
                        "stream_id": stream_id,
                        "reservation_id": reservation_id,
                        "scope": scope,
                        "version": initial_version,
                        "opened_at": opened_at,
                    },
                )
                touch_sse_stream(stream_id)
                while True:
                    try:
                        event = event_queue.get(timeout=SSE_HEARTBEAT_SECONDS)
                    except queue.Empty:
                        # Heartbeat write is the proactive disconnect probe under WSGI.
                        yield ": heartbeat\n\n"
                        touch_sse_stream(stream_id)
                        continue
                    yield format_sse("reservation_version_changed", event)
                    touch_sse_stream(stream_id)
            except GeneratorExit:
                # WSGI/test client closed the streaming response (or upstream abort).
                close_reason = "client_disconnect"
                raise
            except (BrokenPipeError, ConnectionResetError):
                close_reason = "client_disconnect"
            except OSError as exc:
                # EPIPE / ECONNRESET when writing heartbeat to a gone client.
                if getattr(exc, "errno", None) in {32, 104}:
                    close_reason = "client_disconnect"
                else:
                    close_reason = "exception"
                    logger.exception(
                        "sse_stream_error stream_id=%s reservation_id=%s scope=%s",
                        stream_id,
                        reservation_id,
                        scope,
                    )
            except Exception:
                close_reason = "exception"
                logger.exception(
                    "sse_stream_error stream_id=%s reservation_id=%s scope=%s",
                    stream_id,
                    reservation_id,
                    scope,
                )
            finally:
                duration_s = time.monotonic() - started_at
                closed_at = datetime.now(dt_timezone.utc).isoformat()
                logger.info(
                    "sse_stream_closed stream_id=%s reservation_id=%s scope=%s "
                    "worker_pid=%s duration_s=%s opened_at=%s closed_at=%s "
                    "close_reason=%s",
                    stream_id,
                    reservation_id,
                    scope,
                    worker_pid,
                    int(duration_s),
                    opened_at,
                    closed_at,
                    close_reason,
                )
                record_sse_stream_closed(duration_s)
                event_bus.unsubscribe(reservation_id, scope, event_queue)
                close_sse_stream(stream_id, close_reason)
                check_sse_invariant()

        response = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        response["X-SSE-Stream-Id"] = stream_id
        return response


class ReceptionMonthlyStatisticsView(ReceptionReadView):
    def get(self, request):
        year_param = request.query_params.get("year")
        if year_param is None or str(year_param).strip() == "":
            year = timezone.localdate().year
        else:
            try:
                year = int(year_param)
            except (TypeError, ValueError):
                return Response(
                    {"detail": "year mora biti cijeli broj."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if year < 2000 or year > 2100:
                return Response(
                    {"detail": "year izvan dopuštenog raspona."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        return Response(aggregate_monthly_statistics(request.tenant, year))


class ReceptionGuestCountriesStatisticsView(ReceptionReadView):
    def get(self, request):
        year_param = request.query_params.get("year")
        if year_param is None or str(year_param).strip() == "":
            year = timezone.localdate().year
        else:
            try:
                year = int(year_param)
            except (TypeError, ValueError):
                return Response(
                    {"detail": "year mora biti cijeli broj."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if year < 2000 or year > 2100:
                return Response(
                    {"detail": "year izvan dopuštenog raspona."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        return Response(aggregate_guest_countries_statistics(request.tenant, year))


class ReservationTimelineListView(ReceptionReadView, generics.ListAPIView):
    serializer_class = ReservationTimelineSerializer

    def _include_canceled(self) -> bool:
        raw = (self.request.query_params.get("include_canceled") or "").strip().lower()
        return raw in ("1", "true", "yes")

    def get_queryset(self):
        queryset = _reservation_queryset(self.request.tenant).order_by("check_in", "id")

        status_param = self.request.query_params.get("status")
        if status_param:
            queryset = queryset.filter(status=status_param)

        day = self._parse_date("date")
        if day:
            queryset = queryset.filter(check_in__lte=day, check_out__gt=day)

        check_in_from = self._parse_date("check_in_from")
        if check_in_from:
            queryset = queryset.filter(check_in__gte=check_in_from)

        check_in_to = self._parse_date("check_in_to")
        if check_in_to:
            queryset = queryset.filter(check_in__lte=check_in_to)

        booked_from = self._parse_date("booked_from")
        booked_to = self._parse_date("booked_to")
        canceled_from = self._parse_date("canceled_from")
        canceled_to = self._parse_date("canceled_to")
        has_booked_range = booked_from and booked_to
        has_canceled_range = canceled_from and canceled_to
        combined_mode = self._include_canceled() or (
            has_booked_range and has_canceled_range
        )

        if has_booked_range and combined_mode:
            booked_start, booked_end = property_day_range(booked_from, booked_to)
            booked_q = Q(
                booked_at__isnull=False,
                booked_at__gte=booked_start,
                booked_at__lt=booked_end,
            ) & ~Q(status=Reservation.Status.CANCELED)
            if has_canceled_range:
                cancel_start, cancel_end = property_day_range(
                    canceled_from, canceled_to
                )
            else:
                cancel_start, cancel_end = booked_start, booked_end
            canceled_q = Q(
                status=Reservation.Status.CANCELED,
                canceled_at__isnull=False,
                canceled_at__gte=cancel_start,
                canceled_at__lt=cancel_end,
            )
            queryset = (
                queryset.filter(booked_q | canceled_q)
                .annotate(
                    activity_at=Case(
                        When(status=Reservation.Status.CANCELED, then=F("canceled_at")),
                        default=F("booked_at"),
                        output_field=DateTimeField(),
                    )
                )
                .order_by("-activity_at", "-id")
            )
        elif has_booked_range:
            start, end = property_day_range(booked_from, booked_to)
            queryset = (
                queryset.filter(
                    booked_at__isnull=False,
                    booked_at__gte=start,
                    booked_at__lt=end,
                )
                .exclude(status=Reservation.Status.CANCELED)
                .order_by("-booked_at", "-id")
            )
        elif has_canceled_range:
            start, end = property_day_range(canceled_from, canceled_to)
            queryset = queryset.filter(
                status=Reservation.Status.CANCELED,
                canceled_at__isnull=False,
                canceled_at__gte=start,
                canceled_at__lt=end,
            ).order_by("-canceled_at", "-id")
        else:
            period_from = self._parse_date("period_from")
            period_to = self._parse_date("period_to")
            if period_from and period_to:
                queryset = queryset.filter(
                    Q(status=Reservation.Status.CHECKED_IN)
                    | Q(
                        check_in__gte=period_from,
                        check_in__lte=period_to,
                    )
                    | Q(
                        check_out__gte=period_from,
                        check_out__lte=period_to,
                    )
                )

        search = self.request.query_params.get("search", "").strip()
        if search:
            queryset = queryset.filter(
                Q(external_id__icontains=search)
                | Q(units__room_name__icontains=search)
                | Q(guests__first_name__icontains=search)
                | Q(guests__last_name__icontains=search)
            ).distinct()

        return queryset

    def _parse_date(self, key: str) -> date_type | None:
        raw_value = self.request.query_params.get(key)
        if not raw_value:
            return None
        return parse_date(raw_value)


class ReservationDetailView(TenantAPIView, generics.RetrieveUpdateAPIView):
    permission_classes = [HasReceptionAccess, DenyAdminScopes]

    def get_permissions(self):
        if self.request.method in ("PATCH", "PUT"):
            self.required_scopes = ["reception:write"]
        else:
            self.required_scopes = ["reception:read"]
        return [permission() for permission in self.permission_classes]

    def get_serializer_class(self):
        if self.request.method in ("PUT", "PATCH"):
            return ReservationUpdateSerializer
        return ReservationTimelineSerializer

    def get_queryset(self):
        return _reservation_queryset(self.request.tenant).order_by("id")

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        old_status = instance.status
        update_serializer = self.get_serializer(instance, data=request.data, partial=partial)
        update_serializer.is_valid(raise_exception=True)
        self.perform_update(update_serializer)
        instance.refresh_from_db()
        if old_status != instance.status:
            from apps.communications.guest_email import queue_guest_booking_canceled_email
            from apps.core.tasks import notify_reservation_status_changed

            notify_reservation_status_changed.delay(
                instance.pk,
                old_status,
                instance.status,
                installation_id_from_request(request),
            )
            if instance.status == Reservation.Status.CANCELED:
                queue_guest_booking_canceled_email(instance.pk, old_status=old_status)
        detail = self.get_queryset().get(pk=instance.pk)
        output = ReservationTimelineSerializer(detail, context=self.get_serializer_context())
        return Response(output.data)


class GuestCheckInRegenerateView(ReceptionWriteView, APIView):
    """Revoke the active guest check-in session and issue a new link (staff)."""

    def post(self, request, pk: int):
        reservation = _get_reservation(request.tenant, pk)
        from apps.reservations.guest_checkin_orchestrator import GuestCheckInOrchestrator
        from apps.reservations.guest_checkin_progress import checkin_progress_for_reservation
        from apps.reservations.models import GuestCheckInSessionCreatedFrom

        result = GuestCheckInOrchestrator.regenerate_link(
            reservation,
            created_from=GuestCheckInSessionCreatedFrom.RECEPTION_MANUAL,
        )
        return Response(
            {
                "url": result.url,
                "token": str(result.session.token),
                "checkin_progress": checkin_progress_for_reservation(reservation),
            },
            status=status.HTTP_200_OK,
        )


class ReservationGuestListCreateView(ReceptionWriteView, generics.CreateAPIView):
    serializer_class = GuestCreateSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["reservation"] = _get_reservation(
            self.request.tenant, self.kwargs["reservation_id"]
        )
        context["tenant"] = self.request.tenant
        return context

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        guest = serializer.save()
        output = GuestDetailSerializer(guest, context=self.get_serializer_context())
        return Response(output.data, status=status.HTTP_201_CREATED)


class ReservationGuestDetailView(TenantAPIView, generics.RetrieveUpdateAPIView):
    serializer_class = GuestDetailSerializer
    lookup_url_kwarg = "guest_id"
    permission_classes = [HasReceptionAccess, DenyAdminScopes]

    def get_permissions(self):
        if self.request.method in ("PATCH", "PUT"):
            self.required_scopes = ["reception:write"]
        else:
            self.required_scopes = ["reception:read"]
        return [permission() for permission in self.permission_classes]

    def get_queryset(self):
        return (
            Guest.objects.for_tenant(self.request.tenant)
            .filter(reservation_id=self.kwargs["reservation_id"])
            .prefetch_related(_guest_id_documents_prefetch())
            .order_by("id")
        )


class GuestFacePhotoView(ReceptionReadView, APIView):
    def get(self, request, reservation_id: int, guest_id: int):
        guest = _get_guest(request.tenant, reservation_id, guest_id)
        doc = guest_face_photo_document(guest)
        if doc is None or not doc.face_photo:
            raise NotFound("Fotografija gosta nije dostupna.")
        content_type, _ = mimetypes.guess_type(doc.face_photo.name)
        return FileResponse(
            doc.face_photo.open("rb"),
            content_type=content_type or "image/jpeg",
        )


class ReservationConfirmationPdfView(ReceptionReadView, APIView):
    def get(self, request, pk: int):
        reservation = _get_reservation(request.tenant, pk)
        if not reservation.confirmation_pdf:
            raise NotFound("PDF potvrda nije dostupna.")
        content_type, _ = mimetypes.guess_type(reservation.confirmation_pdf.name)
        return FileResponse(
            reservation.confirmation_pdf.open("rb"),
            content_type=content_type or "application/pdf",
        )


class DocumentScanIngestView(ReceptionWriteView, APIView):
    parser_classes = [JSONParser]

    def post(self, request, reservation_id: int, guest_id: int):
        _get_reservation(request.tenant, reservation_id)
        guest = _get_guest(request.tenant, reservation_id, guest_id)

        started = time.perf_counter()
        status_value = DocumentScanStatus.FAILED
        error_message = ""
        raw_payload = self._parse_json_field(request.data)
        (
            normalized_suggested,
            guest_updates,
            face_photo_b64,
            signature_b64,
            scanned_at,
            method,
            device_id,
        ) = self._build_guest_updates_from_document_scan_payload(raw_payload=raw_payload)

        suggested_fields = normalized_suggested

        if guest_updates:
            status_value = DocumentScanStatus.OK
        else:
            status_value = DocumentScanStatus.FAILED
            error_message = "Ne mogu mapirati payload u polja gosta."

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        duration_ms = self._parse_int(request.data.get("duration_ms")) or elapsed_ms

        scan_log = DocumentScanLog.objects.create(
            tenant=request.tenant,
            reservation_id=reservation_id,
            guest=guest,
            status=status_value,
            method=method,
            device_id=device_id,
            scanned_at=scanned_at,
            duration_ms=duration_ms,
            raw_payload=raw_payload,
            suggested_fields=suggested_fields,
            corrected_fields={},
            error_message=error_message,
        )

        if status_value == DocumentScanStatus.OK and guest_updates:
            for field, value in guest_updates.items():
                setattr(guest, field, value)
            guest.save(update_fields=list(guest_updates.keys()) + ["updated_at", "name"])

        id_document_id = None
        if status_value == DocumentScanStatus.OK and (face_photo_b64 or signature_b64):
            id_document = IdDocument.objects.create(
                guest=guest, image_path="", extracted_payload={}
            )
            id_document_id = id_document.id

            if face_photo_b64:
                content = self._decode_b64_image(face_photo_b64)
                if content:
                    id_document.face_photo.save(f"guest_{guest_id}_face.jpg", content, save=True)

            if signature_b64:
                content = self._decode_b64_image(signature_b64)
                if content:
                    id_document.signature_photo.save(
                        f"guest_{guest_id}_signature.jpg", content, save=True
                    )

        return Response(
            {
                "scan_log_id": scan_log.id,
                "scan_status": status_value,
                "duration_ms": duration_ms,
                "id_document_id": id_document_id,
                "suggested_fields": suggested_fields,
                "raw_payload": raw_payload,
                "error": error_message,
            }
        )

    def _decode_b64_image(self, value: str):
        try:
            raw = (value or "").strip()
            if not raw:
                return None
            if raw.startswith("data:"):
                raw = raw.split(",", 1)[1]
            decoded = base64.b64decode(raw, validate=False)
            if not decoded:
                return None
            return ContentFile(decoded)
        except Exception:
            return None

    def _build_guest_updates_from_document_scan_payload(self, raw_payload: dict):
        meta = raw_payload.get("metapodaci") if isinstance(raw_payload.get("metapodaci"), dict) else {}
        guest_data = (
            raw_payload.get("podaci_gosta")
            if isinstance(raw_payload.get("podaci_gosta"), dict)
            else {}
        )
        biom = (
            raw_payload.get("biometrija")
            if isinstance(raw_payload.get("biometrija"), dict)
            else {}
        )

        method = str(meta.get("metoda_ocitanja", "")).strip().upper()
        if method not in {"OCR", "NFC"}:
            method = ""

        device_id = str(meta.get("uredaj_id", "")).strip()

        scanned_at = timezone.now()
        scanned_raw = str(meta.get("vrijeme_skeniranja", "")).strip()
        if scanned_raw:
            try:
                scanned_at = timezone.datetime.fromisoformat(scanned_raw.replace("Z", "+00:00"))
            except ValueError:
                pass

        def as_str(key: str) -> str:
            val = guest_data.get(key)
            return str(val).strip() if val is not None else ""

        updates = {}
        first_name = as_str("ime")
        last_name = as_str("prezime")
        if first_name:
            updates["first_name"] = first_name
        if last_name:
            updates["last_name"] = last_name

        doc_no = as_str("broj_dokumenta")
        if doc_no:
            updates["document_number"] = doc_no

        sex = as_str("spol")
        if sex:
            updates["sex"] = sex

        oib = as_str("oib")
        if oib:
            updates["personal_id_number"] = oib

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
        if adresa:
            updates["address"] = normalize_residence_address(adresa)

        tip = str(meta.get("tip_dokumenta", "")).strip().lower()
        if tip == "passport":
            updates["document_type"] = "Putovnica"
        elif tip == "national_id":
            updates["document_type"] = "Osobna iskaznica"

        mrz = str(raw_payload.get("sirovi_mrz", "")).strip()
        if mrz:
            updates["mrz_raw_text"] = mrz
            updates["mrz_verified"] = True
            if not updates.get("sex"):
                mrz_sex = parse_sex_from_mrz(mrz)
                if mrz_sex:
                    updates["sex"] = mrz_sex

        face_photo_b64 = str(biom.get("fotografija_b64", "")).strip()
        signature_b64 = str(biom.get("potpis_b64", "")).strip()

        suggested_fields = {
            "first_name": first_name,
            "last_name": last_name,
            "document_number": doc_no,
            "nationality": updates.get("nationality"),
            "date_of_birth": dob,
            "address": updates.get("address") or adresa,
        }
        suggested_fields = {k: v for k, v in suggested_fields.items() if v}

        return suggested_fields, updates, face_photo_b64, signature_b64, scanned_at, method, device_id

    def _parse_json_field(self, value):
        if isinstance(value, dict):
            return value
        if value is None or value == "":
            return {}
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                return {}
        return {}

    def _parse_int(self, value):
        try:
            if value in (None, ""):
                return None
            return int(value)
        except (TypeError, ValueError):
            return None


def _max_photo_bytes() -> int:
    return int(getattr(settings, "DOCUMENT_PHOTO_MAX_BYTES", 8 * 1024 * 1024))


def _validate_photo_file(value) -> object:
    max_bytes = _max_photo_bytes()
    if value.size > max_bytes:
        raise serializers.ValidationError(f"Datoteka je prevelika (max {max_bytes} bajtova).")
    return value


class DocumentPhotosUploadSerializer(serializers.Serializer):
    document_type = serializers.ChoiceField(
        choices=[DOCUMENT_TYPE_PASSPORT, DOCUMENT_TYPE_NATIONAL_ID],
    )
    front = serializers.FileField()
    back = serializers.FileField(required=False, allow_null=True)

    def validate_front(self, value):
        return _validate_photo_file(value)

    def validate_back(self, value):
        if value is None:
            return value
        return _validate_photo_file(value)

    def validate(self, attrs):
        document_type = attrs["document_type"]
        back = attrs.get("back")
        if document_type == DOCUMENT_TYPE_NATIONAL_ID and not back:
            raise serializers.ValidationError(
                {"back": "Stražnja strana je obavezna za osobnu iskaznicu."}
            )
        return attrs


_GUEST_DOCUMENT_TYPE_LABELS = {
    DOCUMENT_TYPE_PASSPORT: "Putovnica",
    DOCUMENT_TYPE_NATIONAL_ID: "Osobna iskaznica",
}


def _active_id_document_for_guest(guest: Guest) -> IdDocument:
    doc = guest.id_documents.order_by("-created_at", "-id").first()
    if doc is None:
        doc = IdDocument.objects.create(guest=guest, image_path="")
    return doc


class DocumentPhotosUploadView(ReceptionWriteView, APIView):
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, reservation_id: int, guest_id: int):
        _get_reservation(request.tenant, reservation_id)
        guest = _get_guest(request.tenant, reservation_id, guest_id)

        serializer = DocumentPhotosUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        id_document = _active_id_document_for_guest(guest)
        front_saved = False
        back_saved = False

        doc_type = data["document_type"]
        id_document._passport_photo = doc_type == DOCUMENT_TYPE_PASSPORT

        front = data["front"]
        front_name = document_photo_filename(
            guest_id=guest.id,
            document_type=doc_type,
            side="front",
        )
        id_document.front_photo.save(front_name, front, save=False)
        front_saved = True

        back = data.get("back")
        if back is not None:
            back_name = document_photo_filename(
                guest_id=guest.id,
                document_type=doc_type,
                side="back",
            )
            id_document.back_photo.save(back_name, back, save=False)
            back_saved = True

        id_document.save(update_fields=["front_photo", "back_photo", "updated_at"])

        guest.document_type = _GUEST_DOCUMENT_TYPE_LABELS[data["document_type"]]
        guest.save(update_fields=["document_type", "updated_at", "name"])

        return Response(
            {
                "id_document_id": id_document.id,
                "document_type": data["document_type"],
                "front_saved": front_saved,
                "back_saved": back_saved,
            },
            status=status.HTTP_200_OK,
        )


_ID_SCAN_SAMPLE_SOURCES = {
    IdRecognitionSampleSource.MRZ_PLUS,
    IdRecognitionSampleSource.MRZ_LEGACY,
}


class IdScanSampleUploadSerializer(serializers.Serializer):
    image = serializers.FileField()
    document_type = serializers.ChoiceField(
        choices=[DOCUMENT_TYPE_PASSPORT, DOCUMENT_TYPE_NATIONAL_ID],
    )
    source = serializers.ChoiceField(choices=sorted(_ID_SCAN_SAMPLE_SOURCES))
    raw_mrz = serializers.CharField(required=False, allow_blank=True, default="")
    ocr_text = serializers.CharField(required=False, allow_blank=True, default="")
    device_id = serializers.CharField(required=False, allow_blank=True, default="")
    parsed_snapshot = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_image(self, value):
        return _validate_photo_file(value)

    def validate_parsed_snapshot(self, value):
        if value is None or value == "":
            return {}
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise serializers.ValidationError("parsed_snapshot nije valjani JSON.") from exc
            if not isinstance(parsed, dict):
                raise serializers.ValidationError("parsed_snapshot mora biti JSON objekt.")
            return parsed
        raise serializers.ValidationError("parsed_snapshot mora biti JSON objekt.")


class IdScanSampleUploadView(ReceptionWriteView, APIView):
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, reservation_id: int, guest_id: int):
        _get_reservation(request.tenant, reservation_id)
        guest = _get_guest(request.tenant, reservation_id, guest_id)

        serializer = IdScanSampleUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        image = data["image"]
        content_sha256 = hashlib.sha256(image.read()).hexdigest()
        image.seek(0)

        sample = IdRecognitionSample(
            tenant=request.tenant,
            reservation_id=reservation_id,
            guest=guest,
            source=data["source"],
            document_type=data["document_type"],
            raw_mrz=str(data.get("raw_mrz", "")).strip(),
            ocr_text=str(data.get("ocr_text", "")).strip(),
            device_id=str(data.get("device_id", "")).strip(),
            parsed_snapshot=data.get("parsed_snapshot") or {},
            content_sha256=content_sha256,
        )
        filename = id_recognition_sample_filename(
            guest_id=guest.id,
            source=data["source"],
        )
        sample.image.save(filename, image, save=False)
        sample.save()

        return Response(
            {"sample_id": sample.id, "content_sha256": content_sha256},
            status=status.HTTP_201_CREATED,
        )


_EVISITOR_FIELD_LABELS_HR = {
    "first_name": "Ime",
    "last_name": "Prezime",
    "sex": "Spol",
    "date_of_birth": "Datum rođenja",
    "nationality": "Državljanstvo",
    "document_type": "Tip dokumenta",
    "document_number": "Broj dokumenta",
    "facility": "Šifra objekta",
    "stay_dates": "Datumi boravka",
}


def _evisitor_validation_message(exc: EvisitorValidationError, field_errors: dict) -> str:
    if not field_errors:
        return str(exc)
    lines = [
        f"{_EVISITOR_FIELD_LABELS_HR.get(key, key)}: {msg}"
        for key, msg in field_errors.items()
    ]
    return "Podaci nisu potpuni za eVisitor prijavu.\n" + "\n".join(lines)


class EvisitorSubmitView(ReceptionWriteView, APIView):
    def post(self, request, reservation_id: int, guest_id: int):
        _get_reservation(request.tenant, reservation_id)
        guest = _get_guest(request.tenant, reservation_id, guest_id)

        force_retry = bool(request.data.get("force_retry")) if isinstance(
            request.data, dict
        ) else False

        if (
            guest.evisitor_status
            in (EvisitorGuestStatus.SENT, EvisitorGuestStatus.CHECKOUT_FAILED)
            and not force_retry
        ):
            from apps.integrations.whatsapp.evisitor_reply import (
                maybe_send_evisitor_registered_whatsapp_reply,
            )

            wa_result = maybe_send_evisitor_registered_whatsapp_reply(guest.reservation)
            response_payload = {
                "status": guest.evisitor_status,
                "registration_id": str(guest.evisitor_registration_id or ""),
                "message": "Gost je već prijavljen u eVisitor.",
            }
            if wa_result.get("status") not in ("skipped", "disabled"):
                response_payload["whatsapp"] = wa_result
            return Response(response_payload)

        if guest.evisitor_status == EvisitorGuestStatus.CHECKOUT_FAILED:
            return Response(
                {
                    "status": EvisitorGuestStatus.CHECKOUT_FAILED,
                    "registration_id": str(guest.evisitor_registration_id or ""),
                    "message": (
                        "Gost je prijavljen u eVisitor; odjava nije uspjela. "
                        "Koristite evisitor-checkout za ponovnu odjavu."
                    ),
                },
                status=status.HTTP_409_CONFLICT,
            )

        if not guest_requires_evisitor(guest):
            return Response(
                {
                    "status": "not_required",
                    "message": "eVisitor prijava nije potrebna za goste mlađe od 18 godina.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            submission = submit_guest_checkin(guest, force_retry=force_retry)
        except EvisitorValidationError as exc:
            field_errors = exc.field_errors or {}
            return Response(
                {
                    "status": "validation_failed",
                    "message": _evisitor_validation_message(exc, field_errors),
                    "field_errors": field_errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        except EvisitorConfigError as exc:
            return Response(
                {
                    "status": "config_error",
                    "message": str(exc),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        except EvisitorApiError as exc:
            from apps.integrations.evisitor.messages import resolve_evisitor_error_message

            user_message = resolve_evisitor_error_message(
                user_message=exc.user_message or "",
                system_message=exc.system_message or "",
                fallback=str(exc),
            )
            return Response(
                {
                    "status": EvisitorGuestStatus.FAILED,
                    "user_message": user_message,
                    "system_message": exc.system_message,
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )

        payload = {
            "status": submission.status,
            "registration_id": str(submission.registration_id),
            "submitted_at": submission.submitted_at,
        }
        if submission.response_payload.get("recovered"):
            payload["recovered"] = True
            payload["message"] = submission.response_payload.get("message") or (
                "Gost je već prijavljen u eVisitoru; status usklađen."
            )

        from apps.integrations.whatsapp.evisitor_reply import (
            maybe_send_evisitor_registered_whatsapp_reply,
        )

        guest.reservation.refresh_from_db()
        wa_result = maybe_send_evisitor_registered_whatsapp_reply(guest.reservation)
        if wa_result.get("status") not in ("skipped", "disabled"):
            payload["whatsapp"] = wa_result

        return Response(payload)


class EvisitorCheckoutView(ReceptionWriteView, APIView):
    """Idempotent per-guest eVisitor CheckOut.

    sent / checkout_failed → attempt CheckOut
    checked_out → 200 no-op
    failed / not_sent / pending → 409
    """

    def post(self, request, reservation_id: int, guest_id: int):
        _get_reservation(request.tenant, reservation_id)
        guest = _get_guest(request.tenant, reservation_id, guest_id)
        status_value = (guest.evisitor_status or "").strip() or EvisitorGuestStatus.NOT_SENT

        if status_value == EvisitorGuestStatus.CHECKED_OUT:
            submission = (
                guest.evisitor_submissions.filter(status=EvisitorGuestStatus.CHECKED_OUT)
                .order_by("-created_at")
                .first()
            )
            return Response(
                {
                    "status": EvisitorGuestStatus.CHECKED_OUT,
                    "registration_id": str(guest.evisitor_registration_id or ""),
                    "submitted_at": getattr(submission, "submitted_at", None),
                    "message": "Gost je već odjavljen u eVisitoru.",
                }
            )

        if status_value not in (
            EvisitorGuestStatus.SENT,
            EvisitorGuestStatus.CHECKOUT_FAILED,
        ):
            return Response(
                {
                    "status": status_value,
                    "message": "Gost nije uspješno prijavljen u eVisitor (nije moguća odjava).",
                    "code": "guest_not_checked_in",
                },
                status=status.HTTP_409_CONFLICT,
            )

        try:
            submission = submit_guest_checkout(guest)
        except EvisitorValidationError as exc:
            field_errors = exc.field_errors or {}
            return Response(
                {
                    "status": "validation_failed",
                    "message": _evisitor_validation_message(exc, field_errors),
                    "field_errors": field_errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        except EvisitorConfigError as exc:
            return Response(
                {
                    "status": "config_error",
                    "message": str(exc),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        except EvisitorApiError as exc:
            from apps.integrations.evisitor.messages import resolve_evisitor_error_message

            user_message = resolve_evisitor_error_message(
                user_message=exc.user_message or "",
                system_message=exc.system_message or "",
                fallback=str(exc),
            )
            guest.refresh_from_db()
            return Response(
                {
                    "status": EvisitorGuestStatus.CHECKOUT_FAILED,
                    "user_message": user_message,
                    "system_message": exc.system_message,
                    "registration_id": str(guest.evisitor_registration_id or ""),
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(
            {
                "status": submission.status,
                "registration_id": str(submission.registration_id or ""),
                "submitted_at": submission.submitted_at,
            }
        )


MAX_BOOKING_PDF_BYTES = 5 * 1024 * 1024


def _validate_booking_pdf_file(value) -> object:
    if value.size > MAX_BOOKING_PDF_BYTES:
        raise serializers.ValidationError(
            f"PDF je prevelik (max {MAX_BOOKING_PDF_BYTES} bajtova)."
        )
    content_type = (getattr(value, "content_type", "") or "").lower()
    if content_type and content_type not in {"application/pdf", "application/octet-stream"}:
        raise serializers.ValidationError("Datoteka mora biti PDF.")
    return value


def _reservation_booking_number(reservation: Reservation) -> str:
    return (reservation.booking_code or reservation.external_id or "").strip()


class BookingPdfImportSerializer(serializers.Serializer):
    file = serializers.FileField()
    property_slug = serializers.CharField(required=False, allow_blank=True, default="")
    reservation_id = serializers.IntegerField(required=False, allow_null=True, default=None)
    confirm_booking_mismatch = serializers.BooleanField(required=False, default=False)

    def validate_file(self, value):
        return _validate_booking_pdf_file(value)


class BookingPdfImportView(ReceptionWriteView, APIView):
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        serializer = BookingPdfImportSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        upload = data["file"]
        content = upload.read()
        if not content.startswith(b"%PDF"):
            raise serializers.ValidationError({"file": "Datoteka nije valjani PDF."})

        context_reservation = None
        reservation_id = data.get("reservation_id")
        if reservation_id is not None:
            context_reservation = _get_reservation(request.tenant, reservation_id)

        property_slug = (data.get("property_slug") or "").strip()
        try:
            prop = resolve_property_for_tenant(
                request.tenant,
                slug=property_slug or None,
                reservation=context_reservation,
            )
        except PropertyResolutionError as exc:
            raise serializers.ValidationError(exc.message_dict) from exc

        try:
            row = parse_booking_pdf(content)
        except ValueError as exc:
            raise serializers.ValidationError({"file": str(exc)}) from exc

        if context_reservation is not None:
            context_booking_number = _reservation_booking_number(context_reservation)
            pdf_booking_number = (row.external_id or "").strip()
            if (
                context_booking_number
                and pdf_booking_number
                and context_booking_number != pdf_booking_number
                and not data.get("confirm_booking_mismatch")
            ):
                return Response(
                    {
                        "code": "booking_number_mismatch",
                        "detail": (
                            f"Broj rezervacije u PDF-u ({pdf_booking_number}) razlikuje se "
                            f"od broja na ovoj stranici ({context_booking_number})."
                        ),
                        "pdf_booking_number": pdf_booking_number,
                        "context_booking_number": context_booking_number,
                        "context_reservation_id": context_reservation.id,
                    },
                    status=status.HTTP_409_CONFLICT,
                )

        result = upsert_reservation_from_xls_row(
            tenant=request.tenant,
            property=prop,
            row=row,
            existing_mode="overwrite",
            authoritative_pdf=True,
        )
        if result.skipped:
            return Response(
                {
                    "detail": result.skip_reason or "Uvoz preskočen.",
                    "skip_reason": result.skip_reason,
                    "reservation_id": result.reservation_id,
                },
                status=status.HTTP_409_CONFLICT,
            )

        reservation = _get_reservation(request.tenant, result.reservation_id)
        if reservation.confirmation_pdf:
            reservation.confirmation_pdf.delete(save=False)
        reservation.confirmation_pdf.save(
            f"{row.external_id}.pdf",
            ContentFile(content),
            save=True,
        )

        from apps.reservations.channel_availability_sync import (
            queue_reservation_channel_availability_sync,
        )

        queue_reservation_channel_availability_sync(reservation.pk)

        reservation = _reservation_queryset(request.tenant).filter(pk=reservation.pk).first()
        payload = ReservationTimelineSerializer(
            reservation,
            context={"request": request},
        ).data
        payload["created"] = result.created
        payload["skip_reason"] = result.skip_reason
        return Response(payload, status=status.HTTP_201_CREATED if result.created else status.HTTP_200_OK)
