# Generated manually for PR2 BookingIntakeDraft

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("reservations", "0037_reservation_b2b_billing_snapshot"),
    ]

    operations = [
        migrations.CreateModel(
            name="BookingIntakeDraft",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("draft", "Draft"),
                            ("confirming", "Confirming"),
                            ("confirmed", "Confirmed"),
                            ("discarded", "Discarded"),
                        ],
                        db_index=True,
                        default="draft",
                        max_length=16,
                    ),
                ),
                ("raw_text", models.TextField()),
                ("parsed_json", models.JSONField(blank=True, default=dict)),
                ("missing_fields", models.JSONField(blank=True, default=list)),
                ("property_slug", models.SlugField(blank=True, default="", max_length=64)),
                ("unit_id", models.PositiveIntegerField(blank=True, null=True)),
                ("unit_code", models.CharField(blank=True, default="", max_length=64)),
                ("check_in", models.DateField(blank=True, null=True)),
                ("check_out", models.DateField(blank=True, null=True)),
                ("amount", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ("currency", models.CharField(default="EUR", max_length=3)),
                ("booker_name", models.CharField(blank=True, default="", max_length=255)),
                ("booker_phone", models.CharField(blank=True, default="", max_length=64)),
                ("booker_email", models.EmailField(blank=True, default="", max_length=254)),
                ("booker_address", models.TextField(blank=True, default="")),
                ("buyer_company_name", models.CharField(blank=True, default="", max_length=255)),
                ("buyer_oib", models.CharField(blank=True, default="", max_length=11)),
                ("buyer_address", models.TextField(blank=True, default="")),
                ("invoice_email", models.EmailField(blank=True, default="", max_length=254)),
                ("guest_first_name", models.CharField(blank=True, default="", max_length=100)),
                ("guest_last_name", models.CharField(blank=True, default="", max_length=100)),
                ("llm_model", models.CharField(blank=True, default="", max_length=64)),
                ("prompt_version", models.CharField(blank=True, default="", max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "confirmed_reservation",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="booking_intake_drafts",
                        to="reservations.reservation",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="booking_intake_drafts",
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
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="bookingintakedraft",
            index=models.Index(
                fields=["tenant", "status", "-created_at"],
                name="reservation_tenant__b2f0d1_idx",
            ),
        ),
    ]
