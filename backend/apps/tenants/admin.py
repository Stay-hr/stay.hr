from django import forms
from django.contrib import admin, messages
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from apps.core.admin import SuperuserOnlyAdminMixin, TenantScopedAdminMixin
from apps.tenants.cloudflare.client import CloudflareAPIError
from apps.tenants.cloudflare.dns import provision_tenant_domain_dns
from apps.tenants.models import (
    VALID_SCOPES,
    ApiApplication,
    StaffLoginEvent,
    StaffProfile,
    Tenant,
    TenantDomain,
    TenantMembership,
    TenantReceptionSettings,
)

User = get_user_model()


class TenantDomainInline(admin.TabularInline):
    model = TenantDomain
    extra = 0
    raw_id_fields = ("property",)


class TenantReceptionSettingsInlineForm(forms.ModelForm):
    guest_smtp_password = forms.CharField(
        label="Guest SMTP password",
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="SMTP password for guest_contact_email. Leave blank to keep the current password.",
    )

    class Meta:
        model = TenantReceptionSettings
        fields = (
            "channel_manager",
            "auto_checkout_enabled",
            "guest_contact_email",
            "guest_contact_name",
            "guest_smtp_password",
            "whatsapp_operator_phones",
            "pre_arrival_days_before",
            "pre_arrival_send_time",
            "pre_arrival_schedule_strategy",
            "whatsapp_welcome_days_before",
            "whatsapp_welcome_send_time",
            "whatsapp_welcome_schedule_strategy",
        )

    def save(self, commit=True):
        instance = super().save(commit=False)
        password = self.cleaned_data.get("guest_smtp_password")
        if password:
            instance.set_guest_smtp_password(password)
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class TenantReceptionSettingsInline(admin.StackedInline):
    model = TenantReceptionSettings
    form = TenantReceptionSettingsInlineForm
    extra = 0
    readonly_fields = ("updated_at", "has_guest_smtp_password_display")
    fields = (
        "channel_manager",
        "auto_checkout_enabled",
        "guest_contact_email",
        "guest_contact_name",
        "guest_smtp_password",
        "whatsapp_operator_phones",
        "pre_arrival_days_before",
        "pre_arrival_send_time",
        "pre_arrival_schedule_strategy",
        "whatsapp_welcome_days_before",
        "whatsapp_welcome_send_time",
        "whatsapp_welcome_schedule_strategy",
        "has_guest_smtp_password_display",
        "updated_at",
    )

    @admin.display(description="SMTP password set", boolean=True)
    def has_guest_smtp_password_display(self, obj: TenantReceptionSettings | None) -> bool:
        if obj is None or not obj.pk:
            return False
        return obj.has_guest_smtp_password


class TenantMembershipInline(admin.TabularInline):
    model = TenantMembership
    extra = 1
    raw_id_fields = ("tenant",)
    verbose_name = "Tenant access"
    verbose_name_plural = "Tenant access"


class StaffProfileInline(admin.StackedInline):
    model = StaffProfile
    extra = 0
    max_num = 1
    can_delete = False
    fields = ("preferred_language", "updated_at")
    readonly_fields = ("updated_at",)


class StaffLoginEventInline(admin.TabularInline):
    model = StaffLoginEvent
    fk_name = "user"
    extra = 0
    max_num = 0
    can_delete = False
    fields = ("created_at", "channel", "tenant", "ip_address", "user_agent")
    readonly_fields = fields
    ordering = ("-created_at",)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("tenant").order_by("-created_at")[:10]


@admin.register(StaffLoginEvent)
class StaffLoginEventAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    list_display = (
        "created_at",
        "username",
        "user",
        "tenant",
        "channel",
        "ip_address",
    )
    list_filter = ("channel", "tenant", "created_at")
    search_fields = ("username", "user__username", "ip_address")
    readonly_fields = (
        "user",
        "username",
        "tenant",
        "channel",
        "ip_address",
        "user_agent",
        "created_at",
    )
    raw_id_fields = ("user", "tenant")
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Tenant)
class TenantAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    list_display = (
        "name",
        "slug",
        "status",
        "is_system",
        "timezone",
        "default_language",
        "updated_at",
    )
    list_filter = ("status", "is_system")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    inlines = [TenantDomainInline, TenantReceptionSettingsInline]

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.GET.get("show_system") == "1":
            return qs
        return qs.filter(is_system=False)

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.is_system:
            return False
        return super().has_delete_permission(request, obj)


@admin.action(description="Provision DNS in Cloudflare")
def provision_tenant_domain_dns_action(modeladmin, request, queryset):
    success = 0
    for tenant_domain in queryset:
        try:
            message = provision_tenant_domain_dns(tenant_domain)
        except CloudflareAPIError as exc:
            modeladmin.message_user(
                request,
                f"{tenant_domain.domain}: {exc}",
                level=messages.ERROR,
            )
            continue
        modeladmin.message_user(
            request,
            f"{tenant_domain.domain}: {message}",
            level=messages.SUCCESS,
        )
        success += 1

    if success == 0 and queryset.exists():
        modeladmin.message_user(
            request,
            "No DNS records were provisioned.",
            level=messages.WARNING,
        )


@admin.register(TenantDomain)
class TenantDomainAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    list_display = (
        "domain",
        "tenant",
        "property",
        "domain_type",
        "is_primary",
        "is_verified",
    )
    list_filter = ("domain_type", "is_primary", "is_verified")
    search_fields = ("domain", "tenant__name", "tenant__slug", "property__name")
    raw_id_fields = ("tenant", "property")
    actions = [provision_tenant_domain_dns_action]


class ApiApplicationAdminForm(forms.ModelForm):
    scopes = forms.MultipleChoiceField(
        choices=[(s, s) for s in sorted(VALID_SCOPES)],
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text=(
            "Hospira/recepcija: reception:read, reception:write, public:read. "
            "Public booking: public:read, reservations:create. No admin scopes on tablets."
        ),
    )

    class Meta:
        model = ApiApplication
        fields = ("tenant", "name", "key_prefix", "scopes", "is_active")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and isinstance(self.instance.scopes, list):
            self.initial["scopes"] = self.instance.scopes

    def clean_scopes(self):
        return list(self.cleaned_data.get("scopes") or [])


@admin.action(description="Deactivate selected API applications")
def deactivate_api_applications(modeladmin, request, queryset):
    updated = queryset.update(is_active=False)
    modeladmin.message_user(
        request,
        f"Deactivated {updated} API application(s).",
        level=messages.SUCCESS,
    )


@admin.action(description="Regenerate API token (invalidates previous bearer tokens)")
def regenerate_api_tokens(modeladmin, request, queryset):
    lines: list[str] = []
    for app in queryset:
        raw = app.regenerate_token()
        lines.append(f"{app.name} ({app.tenant.slug}):\n{raw}")

    if not lines:
        modeladmin.message_user(request, "No applications selected.", level=messages.WARNING)
        return

    body = "\n\n".join(lines[:5])
    if len(lines) > 5:
        body += f"\n\n… and {len(lines) - 5} more (open each record for token_display)."

    modeladmin.message_user(
        request,
        "New token(s) generated. Update Hospira / clients — old bearer tokens no longer work.\n\n"
        + body,
        level=messages.WARNING,
    )


@admin.register(ApiApplication)
class ApiApplicationAdmin(TenantScopedAdminMixin, admin.ModelAdmin):
    form = ApiApplicationAdminForm
    list_display = ("name", "tenant", "key_prefix", "is_active", "has_fcm_token", "last_used_at", "created_at")
    list_filter = ("is_active", "tenant")
    search_fields = ("name", "tenant__name", "tenant__slug")
    raw_id_fields = ("tenant",)
    readonly_fields = (
        "token_display",
        "fcm_token_display",
        "fcm_token_updated_at",
        "last_used_at",
        "created_at",
        "updated_at",
    )
    actions = [regenerate_api_tokens, deactivate_api_applications]
    fieldsets = (
        (
            None,
            {
                "fields": ("tenant", "name", "scopes", "is_active"),
            },
        ),
        (
            "Credentials",
            {
                "fields": ("token_display", "public_key_hash", "key_prefix"),
                "description": (
                    "Device token for Hospira (Bearer). Regenerate invalidates the previous token. "
                    "Stored encrypted in the database."
                ),
            },
        ),
        (
            "Push (FCM)",
            {
                "fields": ("fcm_token_display", "fcm_token_updated_at"),
                "description": "Registered by Hospira via PUT /api/v1/app/fcm-token.",
            },
        ),
        (
            "Timestamps",
            {
                "fields": ("last_used_at", "created_at", "updated_at"),
            },
        ),
    )

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        readonly.append("public_key_hash")
        return readonly

    @admin.display(description="Device token")
    def token_display(self, obj: ApiApplication | None) -> str:
        if obj is None or not obj.pk:
            return "—"
        try:
            raw = obj.get_stored_token()
        except Exception as exc:
            return format_html(
                '<span style="color:#b00020">{}</span>',
                str(exc),
            )
        if not raw:
            return mark_safe(
                "<em>Token nije pohranjen — koristi admin akciju "
                "„Regenerate API token”.</em>"
            )
        return format_html(
            '<code style="user-select:all;word-break:break-all">{}</code>',
            raw,
        )

    @admin.display(description="FCM", boolean=True)
    def has_fcm_token(self, obj: ApiApplication) -> bool:
        return bool(obj.fcm_token)

    @admin.display(description="FCM token")
    def fcm_token_display(self, obj: ApiApplication | None) -> str:
        if obj is None or not obj.pk or not obj.fcm_token:
            return "—"
        token = obj.fcm_token
        if len(token) <= 24:
            masked = token
        else:
            masked = f"{token[:12]}…{token[-8:]}"
        return format_html("<code>{}</code>", masked)

    def save_model(self, request, obj, form, change):
        self._enforce_tenant_on_save(request, obj)
        if change:
            obj.full_clean()
            obj.save()
            return

        raw_token = obj.set_token()
        obj.full_clean()
        obj.save()
        self.message_user(
            request,
            f"API application created. Copy this token now — it is also stored encrypted "
            f"and visible on this page after save:\n\n{raw_token}",
            level=messages.WARNING,
        )


# Replace default User admin (platform superusers manage staff + tenant access).
admin.site.unregister(User)


@admin.register(User)
class StayUserAdmin(SuperuserOnlyAdminMixin, DjangoUserAdmin):
    list_display = DjangoUserAdmin.list_display + ("last_login",)
    inlines = list(DjangoUserAdmin.inlines) + [
        StaffProfileInline,
        TenantMembershipInline,
        StaffLoginEventInline,
    ]
