import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import apps.billing.models


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("billing", "0005_fiscalizationattempt_fiskal_request_id"),
        ("tenants", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="FiscalPreparer",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("first_name", models.CharField(max_length=128)),
                ("last_name", models.CharField(max_length=128)),
                ("email", models.EmailField(max_length=254)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="tenants.tenant",
                    ),
                ),
            ],
            options={
                "verbose_name": "Fiscal preparer",
                "verbose_name_plural": "Fiscal preparers",
                "ordering": ["last_name", "first_name", "id"],
            },
        ),
        migrations.CreateModel(
            name="ForeignServiceInvoice",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "provider",
                    models.CharField(
                        choices=[
                            ("booking", "Booking.com"),
                            ("airbnb", "Airbnb"),
                            ("expedia", "Expedia"),
                            ("other", "Other"),
                        ],
                        max_length=32,
                    ),
                ),
                ("supplier_name", models.CharField(max_length=255)),
                (
                    "supplier_country",
                    models.CharField(
                        help_text="ISO 3166-1 alpha-2",
                        max_length=2,
                    ),
                ),
                (
                    "supplier_vat_id",
                    models.CharField(
                        help_text="VAT ID without country prefix (e.g. 805734958B01).",
                        max_length=32,
                    ),
                ),
                ("invoice_number", models.CharField(max_length=64)),
                ("invoice_date", models.DateField()),
                (
                    "tax_period",
                    models.CharField(help_text="YYYY-MM", max_length=7),
                ),
                ("period_from", models.DateField()),
                ("period_to", models.DateField()),
                (
                    "taxable_amount",
                    models.DecimalField(decimal_places=2, max_digits=12),
                ),
                ("currency", models.CharField(default="EUR", max_length=3)),
                (
                    "source_document",
                    models.FileField(
                        blank=True,
                        upload_to=apps.billing.models.foreign_service_invoice_upload_to,
                    ),
                ),
                (
                    "document_sha256",
                    models.CharField(
                        help_text="SHA-256 of original PDF bytes (not extracted text).",
                        max_length=64,
                    ),
                ),
                (
                    "parsed_payload",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="Immutable parser snapshot; do not mutate after import.",
                    ),
                ),
                ("imported_at", models.DateTimeField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="foreign_service_invoices_created",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="tenants.tenant",
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="foreign_service_invoices_updated",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-tax_period", "-invoice_date", "-id"],
            },
        ),
        migrations.AddField(
            model_name="tenantfiscalsettings",
            name="default_preparer",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="default_for_fiscal_settings",
                to="billing.fiscalpreparer",
            ),
        ),
        migrations.AddField(
            model_name="tenantfiscalsettings",
            name="issuer_first_name",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="tenantfiscalsettings",
            name="issuer_last_name",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="tenantfiscalsettings",
            name="issuer_place",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="tenantfiscalsettings",
            name="issuer_street",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="tenantfiscalsettings",
            name="issuer_street_number",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="tenantfiscalsettings",
            name="tax_office_code",
            field=models.CharField(
                blank=True,
                choices=[
                    ("3566", "Šibenik"),
                    ("3500", "Zadar"),
                    ("3400", "Split"),
                    ("3600", "Dubrovnik"),
                    ("1000", "Zagreb"),
                ],
                default="",
                help_text="Porezna ispostava code for ePorezna forms (Ispostava).",
                max_length=8,
            ),
        ),
        migrations.AddConstraint(
            model_name="foreignserviceinvoice",
            constraint=models.UniqueConstraint(
                fields=("tenant", "document_sha256"),
                name="billing_fsi_unique_tenant_sha256",
            ),
        ),
        migrations.AddConstraint(
            model_name="foreignserviceinvoice",
            constraint=models.UniqueConstraint(
                fields=("tenant", "provider", "invoice_number"),
                name="billing_fsi_unique_tenant_provider_number",
            ),
        ),
        migrations.AddIndex(
            model_name="foreignserviceinvoice",
            index=models.Index(
                fields=["tenant", "tax_period"],
                name="billing_fsi_tenant_period",
            ),
        ),
    ]
