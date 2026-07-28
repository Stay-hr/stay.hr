from __future__ import annotations

import json

from django import forms
from django.contrib import admin, messages
from django.utils.html import format_html

from apps.billing.models import (
    FiscalizationAttempt,
    FiscalPreparer,
    ForeignServiceInvoice,
    Invoice,
    InvoiceLine,
    TaxOffice,
    TenantFiscalSettings,
)
from apps.billing.services.issue import (
    get_fiscal_settings_for_reservation,
    refresh_invoice_buyer_from_reservation,
)
from apps.billing.services.pdf import render_invoice_pdf
from apps.billing.tasks import fiscalize_invoice
from apps.core.admin import SuperuserOnlyAdminMixin
from apps.tenants.models import Tenant


class TenantFiscalSettingsInlineForm(forms.ModelForm):
    certificate_password = forms.CharField(
        label="Certificate password",
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Password for the .p12 certificate. Leave blank to keep the current password.",
    )

    class Meta:
        model = TenantFiscalSettings
        fields = (
            "is_vat_registered",
            "issuer_oib",
            "issuer_first_name",
            "issuer_last_name",
            "issuer_name",
            "issuer_place",
            "issuer_street",
            "issuer_street_number",
            "issuer_address",
            "issuer_iban",
            "tax_office_code",
            "default_preparer",
            "business_premise_code",
            "payment_device_code",
            "operator_code",
            "accommodation_vat_rate",
            "certificate_file",
            "certificate_password",
            "certificate_expires_at",
            "use_test_endpoint",
        )
        widgets = {
            "tax_office_code": forms.Select(
                choices=[("", "---------")]
                + [(c.value, f"{c.value} — {c.label}") for c in TaxOffice]
            ),
        }
        help_texts = {
            "default_preparer": "Must belong to the same tenant.",
            "tax_office_code": "Porezna ispostava (Ispostava) for ePorezna forms.",
        }

    def clean_default_preparer(self):
        preparer = self.cleaned_data.get("default_preparer")
        if preparer is None:
            return preparer
        tenant_id = getattr(self.instance, "tenant_id", None)
        if tenant_id is None:
            return preparer
        if preparer.tenant_id != tenant_id:
            raise forms.ValidationError(
                "Default preparer must belong to the same tenant."
            )
        return preparer

    def save(self, commit=True):
        instance = super().save(commit=False)
        password = self.cleaned_data.get("certificate_password")
        if password:
            instance.set_certificate_password(password)
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class TenantFiscalSettingsInline(admin.StackedInline):
    model = TenantFiscalSettings
    form = TenantFiscalSettingsInlineForm
    extra = 0
    max_num = 1
    can_delete = False
    verbose_name = "Fiscal settings"
    verbose_name_plural = "Fiscal settings (PDV-S / guest invoices)"
    readonly_fields = (
        "invoice_sequence",
        "has_certificate_display",
        "has_certificate_password_display",
        "updated_at",
    )
    fieldsets = (
        (
            "Fiscal identity",
            {
                "fields": (
                    "issuer_oib",
                    "issuer_first_name",
                    "issuer_last_name",
                    "issuer_name",
                ),
            },
        ),
        (
            "Address",
            {
                "fields": (
                    "issuer_place",
                    "issuer_street",
                    "issuer_street_number",
                    "issuer_address",
                    "issuer_iban",
                ),
            },
        ),
        (
            "Tax (PDV-S)",
            {
                "fields": (
                    "tax_office_code",
                    "default_preparer",
                ),
                "description": (
                    "PDV-S Zaglavlje. Default preparer must belong to the same tenant."
                ),
            },
        ),
        (
            "Guest invoice / CIS",
            {
                "classes": ("collapse",),
                "fields": (
                    "is_vat_registered",
                    "business_premise_code",
                    "payment_device_code",
                    "operator_code",
                    "accommodation_vat_rate",
                    "invoice_sequence",
                ),
            },
        ),
        (
            "Certificate",
            {
                "classes": ("collapse",),
                "fields": (
                    "certificate_file",
                    "certificate_password",
                    "has_certificate_display",
                    "has_certificate_password_display",
                    "certificate_expires_at",
                    "use_test_endpoint",
                    "updated_at",
                ),
            },
        ),
    )

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "default_preparer":
            parent_id = getattr(self, "_parent_tenant_id", None)
            if parent_id is None:
                object_id = request.resolver_match.kwargs.get("object_id") if request.resolver_match else None
                parent_id = object_id
            qs = FiscalPreparer.objects.all().order_by("last_name", "first_name")
            if parent_id:
                qs = qs.filter(tenant_id=parent_id)
            else:
                qs = qs.none()
            kwargs["queryset"] = qs
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def get_formset(self, request, obj=None, **kwargs):
        self._parent_tenant_id = obj.pk if obj is not None else None
        return super().get_formset(request, obj, **kwargs)

    @admin.display(description="Certificate uploaded", boolean=True)
    def has_certificate_display(self, obj: TenantFiscalSettings | None) -> bool:
        if obj is None or not obj.pk:
            return False
        return obj.has_certificate

    @admin.display(description="Certificate password set", boolean=True)
    def has_certificate_password_display(self, obj: TenantFiscalSettings | None) -> bool:
        if obj is None or not obj.pk:
            return False
        return obj.has_certificate_password


class InvoiceLineInline(admin.TabularInline):
    model = InvoiceLine
    extra = 0
    readonly_fields = (
        "sort_order",
        "line_kind",
        "description",
        "quantity",
        "unit_price",
        "vat_rate",
        "vat_amount",
        "line_total",
    )
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.action(description="Retry fiscalization")
def retry_fiscalization(modeladmin, request, queryset):
    count = 0
    for invoice in queryset:
        fiscalize_invoice.delay(invoice.pk)
        count += 1
    modeladmin.message_user(
        request,
        f"Queued fiscalization for {count} invoice(s).",
        level=messages.SUCCESS,
    )


@admin.action(description="Regeneriraj PDF")
def regenerate_invoice_pdf(modeladmin, request, queryset):
    count = 0
    for invoice in queryset.select_related("tenant", "reservation"):
        refresh_invoice_buyer_from_reservation(invoice)
        settings = get_fiscal_settings_for_reservation(invoice.reservation)
        render_invoice_pdf(invoice, settings)
        count += 1
    modeladmin.message_user(
        request,
        f"Regenerated PDF for {count} invoice(s).",
        level=messages.SUCCESS,
    )


@admin.register(Invoice)
class InvoiceAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    list_display = (
        "invoice_number",
        "tenant",
        "buyer_name",
        "total",
        "currency",
        "fiscal_status",
        "issued_at",
    )
    list_filter = ("fiscal_status", "tenant")
    search_fields = ("invoice_number", "buyer_name", "jir", "zki", "reservation__booking_code")
    readonly_fields = (
        "tenant",
        "reservation",
        "invoice_number",
        "sequence_number",
        "issued_at",
        "buyer_name",
        "buyer_document_number",
        "buyer_address",
        "payment_method",
        "payment_note",
        "subtotal",
        "vat_amount",
        "total",
        "currency",
        "zki",
        "jir",
        "fiscal_status",
        "fiscal_error",
        "fiscalized_at",
        "pdf_link",
        "public_access_token",
        "email_sent_at",
        "email_recipient",
        "created_at",
        "updated_at",
    )
    inlines = [InvoiceLineInline]
    actions = [retry_fiscalization, regenerate_invoice_pdf]

    @admin.display(description="PDF")
    def pdf_link(self, obj: Invoice | None) -> str:
        if obj is None or not obj.pk or not obj.pdf_file:
            return "—"
        return format_html('<a href="{}" target="_blank">Download PDF</a>', obj.pdf_file.url)

    def has_add_permission(self, request):
        return False


@admin.register(FiscalizationAttempt)
class FiscalizationAttemptAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("invoice", "attempt_no", "success", "created_at")
    list_filter = ("success",)
    readonly_fields = (
        "invoice",
        "attempt_no",
        "success",
        "request_snapshot",
        "response_snapshot",
        "error_message",
        "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(FiscalPreparer)
class FiscalPreparerAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("last_name", "first_name", "email", "tenant", "created_at")
    list_filter = ("tenant",)
    search_fields = ("first_name", "last_name", "email", "tenant__name", "tenant__slug")
    ordering = ("last_name", "first_name")
    autocomplete_fields = ("tenant",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(ForeignServiceInvoice)
class ForeignServiceInvoiceAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    list_display = (
        "invoice_number",
        "provider_label",
        "supplier_name",
        "supplier_country",
        "tax_period",
        "amount_display",
        "document_link",
        "tenant",
        "imported_at",
    )
    list_display_links = ("invoice_number",)
    list_filter = ("provider", "supplier_country", "tax_period", "tenant")
    search_fields = (
        "invoice_number",
        "supplier_name",
        "supplier_vat_id",
        "document_sha256",
        "tenant__slug",
        "tenant__name",
    )
    ordering = ("-imported_at",)
    readonly_fields = (
        "tenant",
        "provider",
        "supplier_name",
        "supplier_country",
        "supplier_vat_id",
        "invoice_number",
        "invoice_date",
        "tax_period",
        "period_from",
        "period_to",
        "taxable_amount",
        "currency",
        "source_document",
        "document_sha256",
        "parsed_payload_pretty",
        "created_by",
        "updated_by",
        "imported_at",
        "created_at",
        "updated_at",
    )
    fieldsets = (
        (
            "General",
            {"fields": ("tenant", "source_document")},
        ),
        (
            "Provider",
            {"fields": ("provider",)},
        ),
        (
            "Invoice",
            {
                "fields": (
                    "invoice_number",
                    "invoice_date",
                    "taxable_amount",
                    "currency",
                ),
            },
        ),
        (
            "Supplier",
            {
                "fields": (
                    "supplier_name",
                    "supplier_country",
                    "supplier_vat_id",
                ),
            },
        ),
        (
            "Tax period",
            {
                "fields": (
                    "tax_period",
                    "period_from",
                    "period_to",
                ),
            },
        ),
        (
            "Import metadata",
            {
                "fields": (
                    "document_sha256",
                    "created_by",
                    "updated_by",
                    "imported_at",
                    "created_at",
                    "updated_at",
                ),
            },
        ),
        (
            "Raw payload",
            {
                "classes": ("collapse",),
                "fields": ("parsed_payload_pretty",),
            },
        ),
    )

    def has_add_permission(self, request):
        return False

    @admin.display(description="Provider", ordering="provider")
    def provider_label(self, obj: ForeignServiceInvoice) -> str:
        return obj.get_provider_display()

    @admin.display(description="Amount", ordering="taxable_amount")
    def amount_display(self, obj: ForeignServiceInvoice) -> str:
        return f"{obj.taxable_amount} {obj.currency}"

    @admin.display(description="Document")
    def document_link(self, obj: ForeignServiceInvoice) -> str:
        if not obj.source_document:
            return "—"
        return format_html(
            '<a href="{}" target="_blank" rel="noopener">PDF</a>',
            obj.source_document.url,
        )

    @admin.display(description="Parsed payload")
    def parsed_payload_pretty(self, obj: ForeignServiceInvoice | None) -> str:
        if obj is None or not obj.pk:
            return "—"
        text = json.dumps(obj.parsed_payload or {}, indent=2, ensure_ascii=False)
        return format_html(
            "<details><summary>Show JSON ({} bytes)</summary>"
            "<pre style=\"max-width:80ch;white-space:pre-wrap;word-break:break-word;"
            "background:#f6f8fa;padding:0.75rem;border-radius:4px;\">{}</pre>"
            "</details>",
            len(text.encode("utf-8")),
            text,
        )


def register_tenant_fiscal_inline():
    """Attach fiscal settings first on the Tenant change page (PDV-S config)."""
    try:
        tenant_admin = admin.site._registry[Tenant]
    except KeyError:
        return
    inlines = list(tenant_admin.inlines)
    if TenantFiscalSettingsInline in inlines:
        inlines.remove(TenantFiscalSettingsInline)
    tenant_admin.inlines = [TenantFiscalSettingsInline, *inlines]


register_tenant_fiscal_inline()
