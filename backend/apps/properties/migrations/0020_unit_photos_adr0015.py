# Generated manually for ADR 0015 Phase A — UnitPhoto / PhotoOutbox / UnitPhotoLink.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("properties", "0019_property_messaging_schedules"),
    ]

    operations = [
        migrations.CreateModel(
            name="UnitPhoto",
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
                ("storage_ref", models.CharField(max_length=512)),
                ("content_checksum", models.CharField(db_index=True, max_length=64)),
                ("original_filename", models.CharField(blank=True, max_length=255)),
                ("is_primary", models.BooleanField(default=False)),
                ("sort_order", models.PositiveIntegerField(default=0)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("draft", "Draft"),
                            ("upload_pending", "Upload pending"),
                            ("syncing", "Syncing"),
                            ("active", "Active"),
                            ("delete_pending", "Delete pending"),
                            ("deleted", "Deleted"),
                            ("failed", "Failed"),
                            ("out_of_sync", "Out of sync"),
                        ],
                        db_index=True,
                        default="draft",
                        max_length=32,
                    ),
                ),
                ("deleted_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="tenants.tenant",
                    ),
                ),
                (
                    "unit",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="photos",
                        to="properties.unit",
                    ),
                ),
            ],
            options={
                "ordering": ["sort_order", "id"],
            },
        ),
        migrations.CreateModel(
            name="PhotoOutbox",
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
                    "kind",
                    models.CharField(
                        choices=[
                            ("upload", "Upload"),
                            ("delete", "Delete"),
                            ("reorder", "Reorder"),
                            ("set_primary", "Set primary"),
                        ],
                        max_length=16,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("sent", "Sent"),
                            ("failed", "Failed"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("error_message", models.TextField(blank=True)),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="tenants.tenant",
                    ),
                ),
                (
                    "unit_photo",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="outbox_entries",
                        to="properties.unitphoto",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="UnitPhotoLink",
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
                ("provider", models.CharField(db_index=True, default="channex", max_length=32)),
                ("external_id", models.CharField(blank=True, max_length=64)),
                ("content_checksum_pushed", models.CharField(blank=True, max_length=64)),
                ("last_sync_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="tenants.tenant",
                    ),
                ),
                (
                    "unit_photo",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="links",
                        to="properties.unitphoto",
                    ),
                ),
            ],
            options={
                "ordering": ["-updated_at"],
            },
        ),
        migrations.AddIndex(
            model_name="unitphoto",
            index=models.Index(
                fields=["tenant", "unit", "status"],
                name="properties__tenant__6ddd9c_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="unitphoto",
            index=models.Index(
                fields=["unit", "sort_order"],
                name="properties__unit_id_584836_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="unitphoto",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_primary", True), models.Q(("status", "deleted"), _negated=True)),
                fields=("unit",),
                name="properties_unitphoto_one_primary_per_unit",
            ),
        ),
        migrations.AddIndex(
            model_name="photooutbox",
            index=models.Index(
                fields=["tenant", "status", "kind"],
                name="properties__tenant__7f82c9_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="photooutbox",
            index=models.Index(
                fields=["unit_photo", "status"],
                name="properties__unit_ph_6f65eb_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="unitphotolink",
            constraint=models.UniqueConstraint(
                fields=("unit_photo", "provider"),
                name="properties_unitphotolink_unique_photo_provider",
            ),
        ),
    ]
