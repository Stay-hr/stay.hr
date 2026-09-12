import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0007_booking_offer"),
        ("reservations", "0039_guest_payment_access"),
        ("tenants", "0017_tenantreceptionsettings_messaging_schedules"),
    ]

    operations = [
        migrations.CreateModel(
            name="BillingRecipient",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("requested", "Requested"),
                            ("ready", "Ready"),
                            ("applied", "Applied"),
                        ],
                        db_index=True,
                        default="requested",
                        max_length=16,
                    ),
                ),
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
                ("requested_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("ready_at", models.DateTimeField(blank=True, null=True)),
                ("applied_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "applied_invoice",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="applied_billing_recipient",
                        to="billing.invoice",
                    ),
                ),
                (
                    "reservation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="billing_recipients",
                        to="reservations.reservation",
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
                "ordering": ["-requested_at", "-id"],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("status__in", ["requested", "ready"])),
                        fields=("reservation",),
                        name="billing_recipient_one_open_per_reservation",
                    ),
                    models.CheckConstraint(
                        check=(
                            models.Q(("applied_invoice_id__isnull", False), ("status", "applied"))
                            | models.Q(
                                ("applied_invoice_id__isnull", True),
                                ("status__in", ["requested", "ready"]),
                            )
                        ),
                        name="billing_recipient_applied_iff_invoice",
                    ),
                ],
            },
        ),
    ]
