import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("billing", "0009_invoice_reservation_fk"),
        ("reservations", "0039_guest_payment_access"),
        ("tenants", "0017_tenantreceptionsettings_messaging_schedules"),
    ]

    operations = [
        migrations.CreateModel(
            name="InvoiceReplacement",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("open", "Open"),
                            ("completed", "Completed"),
                            ("cancelled", "Cancelled"),
                        ],
                        db_index=True,
                        default="open",
                        max_length=16,
                    ),
                ),
                ("reason", models.TextField()),
                ("opened_at", models.DateTimeField()),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("cancelled_at", models.DateTimeField(blank=True, null=True)),
                ("cancel_reason", models.TextField(blank=True, default="")),
                ("original_issuer_oib", models.CharField(blank=True, default="", max_length=11)),
                ("original_issuer_oib_source", models.TextField(blank=True, default="")),
                ("original_issuer_oib_recorded_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "cancelled_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="invoice_replacements_cancelled",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "completed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="invoice_replacements_completed",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "opened_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="invoice_replacements_opened",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "original_invoice",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="replacements_as_original",
                        to="billing.invoice",
                    ),
                ),
                (
                    "original_issuer_oib_recorded_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="invoice_replacements_issuer_oib_recorded",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "replacement_invoice",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="replacements_as_replacement",
                        to="billing.invoice",
                    ),
                ),
                (
                    "reservation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="invoice_replacements",
                        to="reservations.reservation",
                    ),
                ),
                (
                    "storno_invoice",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="replacements_as_storno",
                        to="billing.invoice",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="tenants.tenant",
                    ),
                ),
            ],
            options={
                "ordering": ["-opened_at", "-id"],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("status", "open")),
                        fields=("reservation",),
                        name="billing_ir_one_open_per_reservation",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("status", "open")),
                        fields=("original_invoice",),
                        name="billing_ir_one_open_per_original",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("status", "completed")),
                        fields=("original_invoice",),
                        name="billing_ir_one_completed_per_original",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("storno_invoice__isnull", False)),
                        fields=("storno_invoice",),
                        name="billing_ir_unique_storno_invoice",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("replacement_invoice__isnull", False)),
                        fields=("replacement_invoice",),
                        name="billing_ir_unique_replacement_invoice",
                    ),
                    models.CheckConstraint(
                        check=(
                            models.Q(
                                ("status", "open"),
                                ("replacement_invoice__isnull", True),
                                ("completed_by__isnull", True),
                                ("completed_at__isnull", True),
                                ("cancelled_by__isnull", True),
                                ("cancelled_at__isnull", True),
                                ("cancel_reason", ""),
                            )
                            | models.Q(
                                ("status", "completed"),
                                ("storno_invoice__isnull", False),
                                ("replacement_invoice__isnull", False),
                                ("completed_by__isnull", False),
                                ("completed_at__isnull", False),
                                ("cancelled_by__isnull", True),
                                ("cancelled_at__isnull", True),
                            )
                            | (
                                models.Q(
                                    ("status", "cancelled"),
                                    ("storno_invoice__isnull", True),
                                    ("replacement_invoice__isnull", True),
                                    ("cancelled_by__isnull", False),
                                    ("cancelled_at__isnull", False),
                                    ("completed_by__isnull", True),
                                    ("completed_at__isnull", True),
                                )
                                & ~models.Q(("cancel_reason", ""))
                            )
                        ),
                        name="billing_ir_status_links_and_audit",
                    ),
                    models.CheckConstraint(
                        check=(
                            models.Q(("storno_invoice__isnull", True))
                            | ~models.Q(("storno_invoice", models.F("original_invoice")))
                        ),
                        name="billing_ir_storno_ne_original",
                    ),
                    models.CheckConstraint(
                        check=(
                            models.Q(("replacement_invoice__isnull", True))
                            | ~models.Q(("replacement_invoice", models.F("original_invoice")))
                        ),
                        name="billing_ir_replacement_ne_original",
                    ),
                    models.CheckConstraint(
                        check=(
                            models.Q(("storno_invoice__isnull", True))
                            | models.Q(("replacement_invoice__isnull", True))
                            | ~models.Q(("storno_invoice", models.F("replacement_invoice")))
                        ),
                        name="billing_ir_storno_ne_replacement",
                    ),
                    models.CheckConstraint(
                        check=(
                            models.Q(
                                ("original_issuer_oib_recorded_by__isnull", True),
                                ("original_issuer_oib_recorded_at__isnull", True),
                            )
                            | models.Q(
                                ("original_issuer_oib_recorded_by__isnull", False),
                                ("original_issuer_oib_recorded_at__isnull", False),
                            )
                        ),
                        name="billing_ir_issuer_oib_audit_pair",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="InvoiceReplacementRecipient",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "identity_confidence",
                    models.CharField(
                        choices=[("unverified", "Unverified"), ("verified", "Verified")],
                        default="unverified",
                        max_length=16,
                    ),
                ),
                ("company_name", models.CharField(blank=True, default="", max_length=255)),
                ("tax_id", models.CharField(blank=True, default="", max_length=64)),
                ("tax_id_country", models.CharField(blank=True, default="", max_length=2)),
                ("country", models.CharField(blank=True, default="", max_length=2)),
                ("address", models.TextField(blank=True, default="")),
                ("postal_code", models.CharField(blank=True, default="", max_length=16)),
                ("city", models.CharField(blank=True, default="", max_length=128)),
                ("email", models.EmailField(blank=True, default="", max_length=254)),
                ("phone", models.CharField(blank=True, default="", max_length=64)),
                (
                    "source",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("booking_message", "Booking message"),
                            ("whatsapp", "WhatsApp"),
                            ("guest_form", "Guest form"),
                            ("staff", "Staff"),
                        ],
                        default="",
                        max_length=32,
                    ),
                ),
                ("source_ref", models.CharField(blank=True, default="", max_length=64)),
                ("source_excerpt", models.TextField(blank=True, default="")),
                ("verified_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "case",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="recipient",
                        to="billing.invoicereplacement",
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
                    "verified_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="invoice_replacement_recipients_verified",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        check=(
                            models.Q(
                                ("identity_confidence", "unverified"),
                                ("verified_by__isnull", True),
                                ("verified_at__isnull", True),
                            )
                            | models.Q(
                                ("identity_confidence", "verified"),
                                ("verified_by__isnull", False),
                                ("verified_at__isnull", False),
                            )
                        ),
                        name="billing_irr_verified_iff_audit",
                    ),
                ],
            },
        ),
    ]
