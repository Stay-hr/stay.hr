# Generated manually for PR4 BookingOffer

import uuid

import django.db.models.deletion
from django.db import migrations, models

import apps.billing.models


class Migration(migrations.Migration):

    dependencies = [
        ("reservations", "0039_guest_payment_access"),
        ("tenants", "0017_tenantreceptionsettings_messaging_schedules"),
        ("billing", "0006_foreign_service_invoice_pdvs"),
    ]

    operations = [
        migrations.CreateModel(
            name="BookingOffer",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("offer_number", models.CharField(max_length=64)),
                ("issued_at", models.DateTimeField()),
                ("valid_until", models.DateField(blank=True, null=True)),
                ("snapshot", models.JSONField(help_text="Frozen seller/buyer/lines/totals at generation time.")),
                (
                    "pdf_file",
                    models.FileField(
                        blank=True,
                        null=True,
                        upload_to=apps.billing.models.booking_offer_pdf_upload_to,
                    ),
                ),
                ("public_access_token", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("email_sent_at", models.DateTimeField(blank=True, null=True)),
                ("email_recipient", models.EmailField(blank=True, default="", max_length=254)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "reservation",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="booking_offer",
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
                "ordering": ["-issued_at", "-id"],
            },
        ),
    ]
