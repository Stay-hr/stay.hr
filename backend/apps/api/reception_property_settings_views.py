"""Reception Property Settings API (ADR 0008 / PR-D0–F)."""

from __future__ import annotations

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.api.permissions import DenyAdminScopes, HasReceptionAccess
from apps.api.reception_views import ReceptionReadView, ReceptionWriteView
from apps.api.views import TenantAPIView
from apps.properties.automation_settings_service import (
    AutomationSettingsConflict,
    AutomationSettingsService,
)
from apps.properties.checkin_settings_service import (
    CheckinSettingsConflict,
    CheckinSettingsService,
)
from apps.properties.general_settings_service import (
    GeneralSettingsConflict,
    GeneralSettingsService,
)
from apps.properties.guest_settings_service import (
    GuestSettingsConflict,
    GuestSettingsService,
    parse_if_match_version,
    settings_version_etag,
)
from apps.properties.guest_settings_validation import GuestSettingsValidationError
from apps.properties.property_settings_service import (
    SETTINGS_SECTION_SLUGS,
    get_tenant_property_or_none,
    list_properties_for_tenant,
    build_settings_capabilities,
)
from apps.properties.section_settings_validation import SectionSettingsValidationError
from apps.properties.share_service import ShareService, ShareServiceError

SECTION_NOT_AVAILABLE = {
    "detail": "This settings section is not available yet.",
    "code": "settings_section_not_available",
}

SHIPPED_SETTINGS_SECTIONS = frozenset({"guest", "general", "checkin", "automation"})


def _actor_from_request(request) -> tuple[str | None, dict]:
    application = getattr(request, "api_application", None)
    user = getattr(request, "user", None)
    updated_by: dict = {}
    actor_id: str | None = None
    if application is not None:
        actor_id = f"app:{application.pk}"
        updated_by["api_application_id"] = application.pk
        updated_by["api_application_name"] = getattr(application, "name", "") or ""
    if user is not None and getattr(user, "is_authenticated", False):
        updated_by["user_id"] = getattr(user, "pk", None)
        if actor_id is None:
            actor_id = f"user:{user.pk}"
    return actor_id, updated_by


class ReceptionSettingsCapabilitiesView(ReceptionReadView, APIView):
    """GET /api/v1/reception/settings/ — capabilities + tabs (no hardcoded UI flags)."""

    def get(self, request):
        return Response(build_settings_capabilities())


class ReceptionPropertiesListView(ReceptionReadView, APIView):
    """GET /api/v1/reception/properties/ — property picker list."""

    def get(self, request):
        return Response({"results": list_properties_for_tenant(request.tenant)})


class ReceptionPropertySettingsGuestView(TenantAPIView, APIView):
    """GET|PATCH /api/v1/reception/properties/{id}/settings/guest/"""

    permission_classes = [HasReceptionAccess, DenyAdminScopes]
    http_method_names = ["get", "patch", "head", "options"]

    def get_permissions(self):
        if self.request.method in ("PATCH", "PUT", "POST"):
            self.required_scopes = ["reception:write"]
        else:
            self.required_scopes = ["reception:read"]
        return [permission() for permission in self.permission_classes]

    def get(self, request, property_id: int):
        prop = get_tenant_property_or_none(request.tenant, property_id)
        if prop is None:
            return Response(
                {"detail": "Property not found.", "code": "property_not_found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        dto = GuestSettingsService.get(prop)
        response = Response(dto)
        response["ETag"] = settings_version_etag(dto["settings_version"])
        return response

    def patch(self, request, property_id: int):
        prop = get_tenant_property_or_none(request.tenant, property_id)
        if prop is None:
            return Response(
                {"detail": "Property not found.", "code": "property_not_found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        if_match = request.META.get("HTTP_IF_MATCH")
        expected = parse_if_match_version(if_match)
        if not if_match or expected is None:
            return Response(
                {
                    "detail": "If-Match header with settings ETag is required.",
                    "code": "if_match_required",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        actor_id, updated_by = _actor_from_request(request)
        try:
            result = GuestSettingsService.patch(
                prop,
                request.data if isinstance(request.data, dict) else {},
                expected_version=expected,
                actor_id=actor_id,
                updated_by=updated_by,
            )
        except GuestSettingsValidationError as exc:
            return Response(
                {
                    "detail": "Validation failed.",
                    "code": "guest_settings_invalid",
                    "errors": exc.errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        except GuestSettingsConflict as exc:
            response = Response(
                {
                    "detail": "Settings were updated by someone else. Reload and retry.",
                    "code": "settings_version_conflict",
                    "guest_settings": exc.dto,
                },
                status=status.HTTP_409_CONFLICT,
            )
            response["ETag"] = settings_version_etag(exc.dto["settings_version"])
            return response

        response = Response(result.dto)
        response["ETag"] = settings_version_etag(result.dto["settings_version"])
        return response


class ReceptionPropertySettingsGuestPreviewView(ReceptionReadView, APIView):
    """GET …/settings/guest/preview/?lang=&on_date= — PortalRenderer preview."""

    def get(self, request, property_id: int):
        prop = get_tenant_property_or_none(request.tenant, property_id)
        if prop is None:
            return Response(
                {"detail": "Property not found.", "code": "property_not_found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        lang = request.query_params.get("lang") or request.query_params.get("language")
        on_date = request.query_params.get("on_date")
        payload = GuestSettingsService.preview(prop, language=lang, on_date=on_date)
        return Response(payload)


def _require_if_match(request) -> tuple[int | None, Response | None]:
    if_match = request.META.get("HTTP_IF_MATCH")
    expected = parse_if_match_version(if_match)
    if not if_match or expected is None:
        return None, Response(
            {
                "detail": "If-Match header with settings ETag is required.",
                "code": "if_match_required",
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    return expected, None


class ReceptionPropertySettingsGeneralView(TenantAPIView, APIView):
    """GET|PATCH /api/v1/reception/properties/{id}/settings/general/"""

    permission_classes = [HasReceptionAccess, DenyAdminScopes]
    http_method_names = ["get", "patch", "head", "options"]

    def get_permissions(self):
        if self.request.method in ("PATCH", "PUT", "POST"):
            self.required_scopes = ["reception:write"]
        else:
            self.required_scopes = ["reception:read"]
        return [permission() for permission in self.permission_classes]

    def get(self, request, property_id: int):
        prop = get_tenant_property_or_none(request.tenant, property_id)
        if prop is None:
            return Response(
                {"detail": "Property not found.", "code": "property_not_found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        dto = GeneralSettingsService.get(prop)
        response = Response(dto)
        response["ETag"] = settings_version_etag(dto["settings_version"])
        return response

    def patch(self, request, property_id: int):
        prop = get_tenant_property_or_none(request.tenant, property_id)
        if prop is None:
            return Response(
                {"detail": "Property not found.", "code": "property_not_found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        expected, err = _require_if_match(request)
        if err is not None:
            return err

        actor_id, updated_by = _actor_from_request(request)
        try:
            result = GeneralSettingsService.patch(
                prop,
                request.data if isinstance(request.data, dict) else {},
                expected_version=expected,
                actor_id=actor_id,
                updated_by=updated_by,
            )
        except SectionSettingsValidationError as exc:
            return Response(
                {
                    "detail": "Validation failed.",
                    "code": "general_settings_invalid",
                    "errors": exc.errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        except GeneralSettingsConflict as exc:
            response = Response(
                {
                    "detail": "Settings were updated by someone else. Reload and retry.",
                    "code": "settings_version_conflict",
                    "general_settings": exc.dto,
                },
                status=status.HTTP_409_CONFLICT,
            )
            response["ETag"] = settings_version_etag(exc.dto["settings_version"])
            return response

        response = Response(result.dto)
        response["ETag"] = settings_version_etag(result.dto["settings_version"])
        return response


class ReceptionPropertySettingsCheckinView(TenantAPIView, APIView):
    """GET|PATCH /api/v1/reception/properties/{id}/settings/checkin/"""

    permission_classes = [HasReceptionAccess, DenyAdminScopes]
    http_method_names = ["get", "patch", "head", "options"]

    def get_permissions(self):
        if self.request.method in ("PATCH", "PUT", "POST"):
            self.required_scopes = ["reception:write"]
        else:
            self.required_scopes = ["reception:read"]
        return [permission() for permission in self.permission_classes]

    def get(self, request, property_id: int):
        prop = get_tenant_property_or_none(request.tenant, property_id)
        if prop is None:
            return Response(
                {"detail": "Property not found.", "code": "property_not_found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        dto = CheckinSettingsService.get(prop)
        response = Response(dto)
        response["ETag"] = settings_version_etag(dto["settings_version"])
        return response

    def patch(self, request, property_id: int):
        prop = get_tenant_property_or_none(request.tenant, property_id)
        if prop is None:
            return Response(
                {"detail": "Property not found.", "code": "property_not_found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        expected, err = _require_if_match(request)
        if err is not None:
            return err

        actor_id, updated_by = _actor_from_request(request)
        try:
            result = CheckinSettingsService.patch(
                prop,
                request.data if isinstance(request.data, dict) else {},
                expected_version=expected,
                actor_id=actor_id,
                updated_by=updated_by,
            )
        except SectionSettingsValidationError as exc:
            return Response(
                {
                    "detail": "Validation failed.",
                    "code": "checkin_settings_invalid",
                    "errors": exc.errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        except CheckinSettingsConflict as exc:
            response = Response(
                {
                    "detail": "Settings were updated by someone else. Reload and retry.",
                    "code": "settings_version_conflict",
                    "checkin_settings": exc.dto,
                },
                status=status.HTTP_409_CONFLICT,
            )
            response["ETag"] = settings_version_etag(exc.dto["settings_version"])
            return response

        response = Response(result.dto)
        response["ETag"] = settings_version_etag(result.dto["settings_version"])
        return response


class ReceptionPropertySettingsAutomationView(TenantAPIView, APIView):
    """GET|PATCH /api/v1/reception/properties/{id}/settings/automation/"""

    permission_classes = [HasReceptionAccess, DenyAdminScopes]
    http_method_names = ["get", "patch", "head", "options"]

    def get_permissions(self):
        if self.request.method in ("PATCH", "PUT", "POST"):
            self.required_scopes = ["reception:write"]
        else:
            self.required_scopes = ["reception:read"]
        return [permission() for permission in self.permission_classes]

    def get(self, request, property_id: int):
        prop = get_tenant_property_or_none(request.tenant, property_id)
        if prop is None:
            return Response(
                {"detail": "Property not found.", "code": "property_not_found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        dto = AutomationSettingsService.get(prop)
        response = Response(dto)
        response["ETag"] = settings_version_etag(dto["settings_version"])
        return response

    def patch(self, request, property_id: int):
        prop = get_tenant_property_or_none(request.tenant, property_id)
        if prop is None:
            return Response(
                {"detail": "Property not found.", "code": "property_not_found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        expected, err = _require_if_match(request)
        if err is not None:
            return err

        actor_id, updated_by = _actor_from_request(request)
        try:
            result = AutomationSettingsService.patch(
                prop,
                request.data if isinstance(request.data, dict) else {},
                expected_version=expected,
                actor_id=actor_id,
                updated_by=updated_by,
            )
        except SectionSettingsValidationError as exc:
            return Response(
                {
                    "detail": "Validation failed.",
                    "code": "automation_settings_invalid",
                    "errors": exc.errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        except AutomationSettingsConflict as exc:
            response = Response(
                {
                    "detail": "Settings were updated by someone else. Reload and retry.",
                    "code": "settings_version_conflict",
                    "automation_settings": exc.dto,
                },
                status=status.HTTP_409_CONFLICT,
            )
            response["ETag"] = settings_version_etag(exc.dto["settings_version"])
            return response

        response = Response(result.dto)
        response["ETag"] = settings_version_etag(result.dto["settings_version"])
        return response


class ReceptionPropertySettingsSectionStubView(TenantAPIView, APIView):
    """Reserved / not-yet-shipped section endpoints return a stable 404 stub.

    Shipped sections (guest/general/checkin/automation) are registered on dedicated paths.
    """

    permission_classes = [HasReceptionAccess, DenyAdminScopes]
    http_method_names = ["get", "patch", "head", "options"]

    def get_permissions(self):
        if self.request.method in ("PATCH", "PUT", "POST"):
            self.required_scopes = ["reception:write"]
        else:
            self.required_scopes = ["reception:read"]
        return [permission() for permission in self.permission_classes]

    def get(self, request, property_id: int, section: str):
        return self._stub(request, property_id, section)

    def patch(self, request, property_id: int, section: str):
        return self._stub(request, property_id, section)

    def _stub(self, request, property_id: int, section: str):
        if section not in SETTINGS_SECTION_SLUGS:
            return Response(
                {"detail": "Unknown settings section.", "code": "settings_section_unknown"},
                status=status.HTTP_404_NOT_FOUND,
            )
        if section in SHIPPED_SETTINGS_SECTIONS:
            return Response(
                {
                    "detail": f"Use the {section} settings endpoint.",
                    "code": "settings_section_moved",
                },
                status=status.HTTP_404_NOT_FOUND,
            )
        if get_tenant_property_or_none(request.tenant, property_id) is None:
            return Response(
                {"detail": "Property not found.", "code": "property_not_found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(SECTION_NOT_AVAILABLE, status=status.HTTP_404_NOT_FOUND)


class ReceptionPropertySettingsShareView(ReceptionWriteView, APIView):
    """POST …/settings/share/ — ShareService (kind + target); requires reception:write."""

    def post(self, request, property_id: int):
        prop = get_tenant_property_or_none(request.tenant, property_id)
        if prop is None:
            return Response(
                {"detail": "Property not found.", "code": "property_not_found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        actor_id, updated_by = _actor_from_request(request)
        payload = request.data if isinstance(request.data, dict) else {}
        try:
            result = ShareService.share(
                prop,
                payload,
                actor_id=actor_id,
                updated_by=updated_by,
            )
        except ShareServiceError as exc:
            return Response(
                {"detail": exc.detail, "code": exc.code},
                status=exc.http_status,
            )

        return Response(result.to_dict(), status=status.HTTP_200_OK)
