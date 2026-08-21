from django.contrib import admin
from django.shortcuts import render
from django.urls import path, reverse
from django.utils.dateparse import parse_date

from apps.core.admin import TenantScopedAdminMixin
from apps.properties.models import Property
from apps.reservations.booking_xls_import import validate_booking_export_file
from apps.reservations.reports.booking_reconcile import compare_booking_export, recompare_from_snapshot
from apps.reservations.reports.booking_reconcile_apply import (
    BookingReconcileApplyItem,
    apply_booking_reconcile_fixes,
)
from apps.reservations.reports.booking_reconcile_types import (
    BookingFieldKey,
    BookingReconcileParams,
)
from apps.tenants.admin_scope import get_allowed_tenants
from apps.reservations.models import (
    DocumentScanLog,
    EvisitorSubmission,
    Guest,
    IdDocument,
    IdRecognitionSample,
    MonthlyStatisticsOverride,
    Reservation,
    ReservationUnit,
)


class GuestInline(admin.TabularInline):
    model = Guest
    extra = 0
    fields = ("first_name", "last_name", "is_primary", "evisitor_status", "email")


class ReservationUnitInline(admin.TabularInline):
    model = ReservationUnit
    extra = 0


@admin.register(Reservation)
class ReservationAdmin(TenantScopedAdminMixin, admin.ModelAdmin):
    list_display = (
        "booking_code",
        "property",
        "tenant",
        "check_in",
        "check_out",
        "status",
        "booker_name",
    )
    list_filter = ("tenant", "status", "property")
    search_fields = (
        "booking_code",
        "external_id",
        "booker_name",
        "booker_email",
        "buyer_company_name",
        "buyer_oib",
    )
    raw_id_fields = ("tenant", "property")
    date_hierarchy = "check_in"
    inlines = [ReservationUnitInline, GuestInline]

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "booking-reconcile/",
                self.admin_site.admin_view(self.booking_reconcile_view),
                name="reservations_reservation_booking_reconcile",
            ),
        ]
        return custom + urls

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context["booking_reconcile_url"] = reverse(
            "admin:reservations_reservation_booking_reconcile"
        )
        return super().changelist_view(request, extra_context=extra_context)

    def booking_reconcile_view(self, request):
        allowed_tenants = list(get_allowed_tenants(request))
        tenant = allowed_tenants[0] if len(allowed_tenants) == 1 else None
        if request.method == "POST" and not tenant and request.POST.get("tenant_id"):
            tenant = next(
                (t for t in allowed_tenants if str(t.id) == request.POST.get("tenant_id")),
                None,
            )

        properties = (
            Property.objects.filter(tenant=tenant).order_by("name")
            if tenant is not None
            else Property.objects.none()
        )
        selected_property = None
        result = None
        apply_results = None
        error = ""
        property_id = request.POST.get("property_id") or request.GET.get("property_id")
        if property_id and tenant is not None:
            selected_property = properties.filter(id=property_id).first()

        if request.method == "POST" and tenant is not None:
            action = request.POST.get("action")
            try:
                if not selected_property:
                    raise ValueError("Odaberite objekt.")
                if action == "compare":
                    upload = request.FILES.get("file")
                    if upload is None:
                        raise ValueError("Odaberite Booking .xls datoteku.")
                    content = upload.read()
                    validate_booking_export_file(filename=upload.name, content=content)
                    date_axis = request.POST.get("date_axis") or None
                    date_from = parse_date((request.POST.get("date_from") or "").strip() or "")
                    date_to = parse_date((request.POST.get("date_to") or "").strip() or "")
                    params = BookingReconcileParams(
                        tenant=tenant,
                        property=selected_property,
                        date_axis=date_axis if date_axis in {"check_out", "check_in"} else None,
                        date_from=date_from,
                        date_to_inclusive=date_to,
                        filename=upload.name,
                    )
                    result = compare_booking_export(
                        params=params,
                        content=content,
                        store_snapshot=True,
                    )
                elif action == "recompare":
                    snapshot_id = (request.POST.get("snapshot_id") or "").strip()
                    if not snapshot_id:
                        raise ValueError("Nema aktivnog usporedbe (snapshot).")
                    result = recompare_from_snapshot(snapshot_id=snapshot_id, store_snapshot=True)
                elif action == "apply":
                    snapshot_id = (request.POST.get("snapshot_id") or "").strip()
                    if not snapshot_id:
                        raise ValueError("Nema aktivnog usporedbe (snapshot).")
                    mode = request.POST.get("mode") or "fill_empty"
                    confirm_overwrite = request.POST.get("confirm_overwrite") == "on"
                    selected_codes = request.POST.getlist("selected_rows")
                    items = []
                    for code in selected_codes:
                        fields_raw = request.POST.getlist(f"fields_{code}")
                        field_keys = tuple(
                            BookingFieldKey(value)
                            for value in fields_raw
                            if value in {key.value for key in BookingFieldKey}
                        )
                        items.append(
                            BookingReconcileApplyItem(
                                booking_code=code,
                                fields=field_keys,
                                mode=mode if mode in {"fill_empty", "overwrite"} else None,
                            )
                        )
                    apply_results = apply_booking_reconcile_fixes(
                        tenant=tenant,
                        property=selected_property,
                        snapshot_id=snapshot_id,
                        items=tuple(items),
                        default_mode=mode if mode in {"fill_empty", "overwrite"} else "fill_empty",
                        confirm_overwrite=confirm_overwrite,
                        applied_by=f"admin:{request.user.username}",
                    )
            except Exception as exc:
                error = str(exc)

        context = {
            **self.admin_site.each_context(request),
            "title": "Usporedi Booking export",
            "tenant": tenant,
            "tenants": allowed_tenants,
            "properties": properties,
            "selected_property": selected_property,
            "result": result,
            "apply_results": apply_results,
            "error": error,
            "opts": self.model._meta,
        }
        return render(request, "admin/reservations/booking_reconcile.html", context)


@admin.register(ReservationUnit)
class ReservationUnitAdmin(TenantScopedAdminMixin, admin.ModelAdmin):
    list_display = ("reservation", "room_name", "unit", "sort_order", "tenant")
    list_filter = ("tenant",)
    raw_id_fields = ("tenant", "reservation", "unit")


@admin.register(Guest)
class GuestAdmin(TenantScopedAdminMixin, admin.ModelAdmin):
    list_display = ("name", "reservation", "tenant", "is_primary", "evisitor_status", "email")
    list_filter = ("tenant",)
    search_fields = ("name", "email", "reservation__booking_code")
    raw_id_fields = ("tenant", "reservation")


@admin.register(IdDocument)
class IdDocumentAdmin(TenantScopedAdminMixin, admin.ModelAdmin):
    tenant_field = "guest__tenant"
    list_display = ("id", "guest", "created_at")
    raw_id_fields = ("guest",)


@admin.register(IdRecognitionSample)
class IdRecognitionSampleAdmin(TenantScopedAdminMixin, admin.ModelAdmin):
    list_display = (
        "id",
        "guest",
        "source",
        "document_type",
        "device_id",
        "created_at",
        "tenant",
    )
    list_filter = ("source", "document_type", "tenant")
    search_fields = ("raw_mrz", "device_id", "guest__name")
    raw_id_fields = ("tenant", "reservation", "guest")
    readonly_fields = ("content_sha256", "created_at")


@admin.register(DocumentScanLog)
class DocumentScanLogAdmin(TenantScopedAdminMixin, admin.ModelAdmin):
    list_display = ("id", "guest", "status", "method", "created_at", "tenant")
    list_filter = ("status", "tenant")
    raw_id_fields = ("tenant", "reservation", "guest")


@admin.register(MonthlyStatisticsOverride)
class MonthlyStatisticsOverrideAdmin(TenantScopedAdminMixin, admin.ModelAdmin):
    """Jedan zapis = cijeli mjesec; API ne zbraja rezervacije za taj tenant/godina/mjesec."""

    list_display = (
        "tenant",
        "year",
        "month",
        "revenue",
        "nights",
        "commission",
        "currency",
        "updated_at",
    )
    list_filter = ("tenant", "year")
    ordering = ("-year", "-month")
    search_fields = ("notes",)
    raw_id_fields = ("tenant",)
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "tenant",
                    "year",
                    "month",
                    "revenue",
                    "commission",
                    "nights",
                    "currency",
                    "notes",
                ),
                "description": (
                    "Ako postoji zapis za tenant, godinu i mjesec, mjesečna statistika "
                    "koristi ove vrijednosti umjesto zbroja iz rezervacija."
                ),
            },
        ),
    )


@admin.register(EvisitorSubmission)
class EvisitorSubmissionAdmin(TenantScopedAdminMixin, admin.ModelAdmin):
    list_display = ("guest", "status", "registration_id", "submitted_at", "tenant")
    list_filter = ("status", "tenant")
    raw_id_fields = ("tenant", "guest")


from apps.reservations.booking_payout_admin import (  # noqa: E402
    extend_reservation_admin,
)

extend_reservation_admin(ReservationAdmin)
