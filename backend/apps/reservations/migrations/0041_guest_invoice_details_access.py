# Generated manually for GuestInvoiceDetailsAccess

import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("reservations", "0040_guest_evisitor_identity_invented"),
    ]

    operations = [
        migrations.CreateModel(
            name="GuestInvoiceDetailsAccess",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "status",
                    models.CharField(
                        choices=[("active", "Active"), ("revoked", "Revoked")],
                        default="active",
                        max_length=16,
                    ),
                ),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                (
                    "created_from",
                    models.CharField(
                        choices=[
                            ("whatsapp", "WhatsApp"),
                            ("email", "Email"),
                            ("booking", "Booking"),
                            ("reception_manual", "Reception manual"),
                            ("system", "System"),
                        ],
                        max_length=32,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("token", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                (
                    "reservation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="guest_invoice_details_accesses",
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
                "ordering": ["-created_at", "id"],
            },
        ),
        migrations.AddIndex(
            model_name="guestinvoicedetailsaccess",
            index=models.Index(fields=["reservation", "status"], name="reservation_invd_res_stat_idx"),
        ),
        migrations.AddIndex(
            model_name="guestinvoicedetailsaccess",
            index=models.Index(fields=["token"], name="reservation_invd_token_idx"),
        ),
        migrations.AddConstraint(
            model_name="guestinvoicedetailsaccess",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "active")),
                fields=("reservation",),
                name="reservations_guest_invd_one_active_per_reservation",
            ),
        ),
    ]
