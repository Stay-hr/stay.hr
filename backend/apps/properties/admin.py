from django.contrib import admin
from django.utils.html import format_html

from apps.core.admin import TenantScopedAdminMixin
from apps.properties.admin_forms import PropertyAdminForm
from apps.properties.models import (
    PhotoOutbox,
    Property,
    Unit,
    UnitBed,
    UnitBathroom,
    UnitPhoto,
    UnitPhotoLink,
)


class UnitBedInline(admin.TabularInline):
    model = UnitBed
    extra = 1
    min_num = 0
    fields = ("bed_type", "count", "sort_order")
    ordering = ("sort_order", "id")
    verbose_name = "Bed"
    verbose_name_plural = "Standard Arrangement (Booking.com beds)"

    def get_queryset(self, request):
        return super().get_queryset(request).order_by("sort_order", "id")


class UnitBathroomInline(admin.StackedInline):
    model = UnitBathroom
    extra = 0
    min_num = 0
    fields = ("is_private", "is_inside_room", "sort_order")
    ordering = ("sort_order", "id")
    verbose_name = "Bathroom"
    verbose_name_plural = "Bathrooms (Booking.com)"

    def get_queryset(self, request):
        return super().get_queryset(request).order_by("sort_order", "id")


class UnitInline(admin.TabularInline):
    model = Unit
    extra = 0
    show_change_link = True
    fields = (
        "code",
        "name",
        "capacity_max_guests",
        "capacity_adults",
        "capacity_children",
        "capacity_infants",
        "is_active",
    )


@admin.register(Property)
class PropertyAdmin(TenantScopedAdminMixin, admin.ModelAdmin):
    form = PropertyAdminForm
    list_display = (
        "name",
        "slug",
        "tenant",
        "tourist_tax_zone",
        "tourist_tax_category",
        "unit_count",
        "updated_at",
    )
    list_filter = (
        "tenant",
        "self_service_mode",
        "tourist_tax_zone",
        "tourist_tax_category",
    )
    search_fields = ("name", "slug", "tenant__name", "tenant__slug")
    prepopulated_fields = {"slug": ("name",)}
    raw_id_fields = ("tenant",)
    autocomplete_fields = ("tourist_tax_zone", "tourist_tax_category")
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "tenant",
                    "name",
                    "slug",
                    "address",
                    "contact",
                    "branding",
                    "guest_info",
                    "timezone",
                    "language",
                ),
            },
        ),
        (
            "WiFi za goste",
            {
                "fields": ("wifi_ssid", "wifi_password"),
                "description": (
                    "WiFi podaci u porukama dobrodošlice nakon check-in-a "
                    "(WhatsApp i email). Sprema se u guest_info."
                ),
            },
        ),
        (
            "Dolazak / odlazak",
            {
                "fields": (
                    "check_in_time",
                    "check_in_latest_time",
                    "check_out_time",
                    "after_hours_arrival_policy",
                    "after_hours_contact_phone",
                    "guest_arrival_auto_reply_enabled",
                    "guest_parking_auto_reply_enabled",
                    "self_service_mode",
                    "self_service_config",
                ),
                "description": (
                    "Prozor dolaska za auto-odgovore gostu. "
                    "check_in_latest_time = zadnji sat samostalnog ulaska; "
                    "after_hours policy određuje odgovor izvan prozora. "
                    "self_service_mode = prikaz key-guide kartice na guest portalu."
                ),
            },
        ),
        (
            "Parking za goste",
            {
                "fields": (
                    "parking_has_private",
                    "parking_zone_label",
                    "parking_price_per_day",
                    "parking_currency",
                    "parking_price_notes",
                    "parking_reservation_required",
                    "parking_ev_charging",
                    "parking_large_vehicles_allowed",
                    "parking_custom_hr",
                    "parking_custom_en",
                ),
                "description": "Strukturirani podaci za auto-odgovore o parkiranju. Sprema se u guest_info.",
            },
        ),
        (
            "WhatsApp autocheck-in",
            {
                "fields": (
                    "whatsapp_autocheckin_enabled",
                    "whatsapp_autocheckin_time",
                    "whatsapp_autocheckin_email_lead_minutes",
                ),
                "description": (
                    "Dnevna welcome poruka gostima s check-inom danas (property lokalno vrijeme). "
                    "Intro email s wa.me linkom šalje se lead_minutes prije welcome vremena. "
                    "Orchestration koristi whatsapp_welcome_* (null = naslijedi / legacy time)."
                ),
            },
        ),
        (
            "Messaging schedules (ADR 0010)",
            {
                "fields": (
                    "pre_arrival_days_before",
                    "pre_arrival_send_time",
                    "pre_arrival_schedule_strategy",
                    "whatsapp_welcome_days_before",
                    "whatsapp_welcome_send_time",
                    "whatsapp_welcome_schedule_strategy",
                ),
                "description": (
                    "Property override za TIME materialization. Prazno/null = Tenant → Platform. "
                    "WELCOME SoT: whatsapp_welcome_* (welcome_* columns unread; Korak 2 drop deferred). "
                    "Strategije: FIXED_TIME, FIRST_AFTER, IMMEDIATE."
                ),
            },
        ),
        (
            "Financijski izvještaj",
            {
                "fields": ("financial_report_recipients",),
                "description": (
                    "Email primatelji za mjesečni financijski izvještaj (odjava u prethodnom mjesecu). "
                    "Zarezom odvojene adrese. Celery šalje 1. u mjesecu ako je "
                    "PROPERTY_FINANCIAL_REPORT_EMAIL_ENABLED=true."
                ),
            },
        ),
        (
            "Turistička pristojba",
            {
                "fields": ("tourist_tax_zone", "tourist_tax_category"),
                "description": (
                    "Postavite zonu i kategoriju smještaja prema odluci Grada Šibenika."
                ),
            },
        ),
    )
    inlines = [UnitInline]

    @admin.display(description="Units")
    def unit_count(self, obj):
        return obj.units.count()


class UnitPhotoInline(admin.TabularInline):
    model = UnitPhoto
    extra = 0
    show_change_link = True
    fields = (
        "status",
        "is_primary",
        "sort_order",
        "original_filename",
        "content_checksum",
        "storage_ref",
    )
    readonly_fields = fields
    ordering = ("sort_order", "id")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Unit)
class UnitAdmin(TenantScopedAdminMixin, admin.ModelAdmin):
    list_display = (
        "code",
        "name",
        "property",
        "tenant",
        "default_nightly_rate",
        "capacity_max_guests",
        "capacity_adults",
        "capacity_children",
        "capacity_infants",
        "beds_summary",
        "bathrooms_summary",
        "is_active",
    )
    list_filter = ("tenant", "is_active", "property")
    search_fields = ("code", "name", "property__name", "property__slug")
    raw_id_fields = ("tenant", "property")
    readonly_fields = ("beds_summary_display", "bathrooms_summary_display", "created_at", "updated_at")
    inlines = [UnitBedInline, UnitBathroomInline, UnitPhotoInline]
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "tenant",
                    "property",
                    "code",
                    "name",
                    "is_active",
                ),
            },
        ),
        (
            "Booking.com occupancy",
            {
                "fields": (
                    "capacity_max_guests",
                    "capacity_adults",
                    "capacity_children",
                    "capacity_infants",
                ),
                "description": (
                    "Maximum guests, adults, children and infants — per room type, "
                    "not per property."
                ),
            },
        ),
        (
            "Pricing",
            {
                "fields": (
                    "default_nightly_rate",
                    "nightly_rate_currency",
                ),
            },
        ),
        (
            "Summary",
            {
                "fields": ("beds_summary_display", "bathrooms_summary_display"),
            },
        ),
        (
            "Timestamps",
            {
                "fields": ("created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("property", "tenant")
            .prefetch_related("beds", "bathrooms")
        )

    @admin.display(description="Beds")
    def beds_summary(self, obj):
        text = obj.get_beds_display()
        return text if text else "—"

    @admin.display(description="Bed arrangement")
    def beds_summary_display(self, obj):
        text = obj.get_beds_display()
        if not text:
            return format_html('<span style="color:#888;">No beds configured</span>')
        return text

    @admin.display(description="Bathrooms")
    def bathrooms_summary(self, obj):
        text = obj.get_bathrooms_display()
        return text if text else "—"

    @admin.display(description="Bathroom arrangement")
    def bathrooms_summary_display(self, obj):
        text = obj.get_bathrooms_display()
        if not text:
            return format_html('<span style="color:#888;">No bathrooms configured</span>')
        return text


@admin.register(UnitBathroom)
class UnitBathroomAdmin(TenantScopedAdminMixin, admin.ModelAdmin):
    list_display = (
        "unit",
        "unit_property",
        "is_private",
        "is_inside_room",
        "sort_order",
        "tenant",
    )
    list_filter = ("tenant", "is_private", "is_inside_room", "unit__property")
    search_fields = ("unit__code", "unit__name", "unit__property__name")
    raw_id_fields = ("tenant", "unit")
    ordering = ("unit__code", "sort_order", "id")
    readonly_fields = ("created_at", "updated_at")

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("unit", "unit__property", "tenant")

    @admin.display(description="Property")
    def unit_property(self, obj):
        return obj.unit.property.name


@admin.register(UnitBed)
class UnitBedAdmin(TenantScopedAdminMixin, admin.ModelAdmin):
    list_display = (
        "unit",
        "unit_property",
        "bed_type_label",
        "count",
        "sort_order",
        "tenant",
    )
    list_filter = ("tenant", "bed_type", "unit__property")
    search_fields = ("unit__code", "unit__name", "unit__property__name")
    raw_id_fields = ("tenant", "unit")
    ordering = ("unit__code", "sort_order", "id")
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "tenant",
                    "unit",
                    "bed_type",
                    "count",
                    "sort_order",
                ),
            },
        ),
        (
            "Timestamps",
            {
                "fields": ("created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )
    readonly_fields = ("created_at", "updated_at")

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("unit", "unit__property", "tenant")

    @admin.display(description="Property")
    def unit_property(self, obj):
        return obj.unit.property.name

    @admin.display(description="Bed type")
    def bed_type_label(self, obj):
        return obj.get_bed_type_display()


@admin.register(UnitPhoto)
class UnitPhotoAdmin(TenantScopedAdminMixin, admin.ModelAdmin):
    list_display = (
        "id",
        "unit",
        "status",
        "is_primary",
        "sort_order",
        "original_filename",
        "content_checksum",
        "tenant",
    )
    list_filter = ("tenant", "status", "is_primary")
    search_fields = ("unit__code", "original_filename", "content_checksum", "storage_ref")
    raw_id_fields = ("tenant", "unit")
    readonly_fields = (
        "storage_ref",
        "content_checksum",
        "created_at",
        "updated_at",
        "deleted_at",
    )


@admin.register(PhotoOutbox)
class PhotoOutboxAdmin(TenantScopedAdminMixin, admin.ModelAdmin):
    list_display = ("id", "kind", "status", "unit_photo", "tenant", "created_at", "sent_at")
    list_filter = ("tenant", "kind", "status")
    raw_id_fields = ("tenant", "unit_photo")
    readonly_fields = ("created_at", "updated_at", "sent_at")


@admin.register(UnitPhotoLink)
class UnitPhotoLinkAdmin(TenantScopedAdminMixin, admin.ModelAdmin):
    list_display = (
        "id",
        "provider",
        "external_id",
        "unit_photo",
        "content_checksum_pushed",
        "last_sync_at",
        "tenant",
    )
    list_filter = ("tenant", "provider")
    raw_id_fields = ("tenant", "unit_photo")
    readonly_fields = ("created_at", "updated_at")
