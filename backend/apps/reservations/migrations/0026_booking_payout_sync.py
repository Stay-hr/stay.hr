import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("reservations", "0025_booking_payout_import"),
        # Tables come from 0028 on fresh DBs (0025_booking_payout_import is a no-op leaf).
        ("reservations", "0028_booking_payout_import"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="bookingpayoutimport",
            name="revision",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AlterField(
            model_name="bookingpayoutimport",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("parsed", "Parsed"),
                    ("partially_synced", "Partially synced"),
                    ("applied", "Applied"),
                    ("failed", "Failed"),
                ],
                default="pending",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="bookingpayoutline",
            name="last_sync_result",
            field=models.CharField(
                blank=True,
                choices=[
                    ("success", "Success"),
                    ("no_changes", "No changes"),
                    ("failed", "Failed"),
                ],
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="bookingpayoutline",
            name="reservation_after_sync",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="bookingpayoutline",
            name="reservation_before_sync",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="bookingpayoutline",
            name="reservation_sync_reason",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="bookingpayoutline",
            name="reservation_synced_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="bookingpayoutline",
            name="reservation_synced_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="booking_payout_line_syncs",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="reservation",
            name="financial_source",
            field=models.CharField(
                blank=True,
                choices=[
                    ("booking_pdf", "Booking PDF"),
                    ("booking_payout", "Booking payout CSV"),
                    ("channel_manager", "Channel manager"),
                    ("manual", "Manual"),
                ],
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="reservation",
            name="financial_synced_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="reservation",
            name="financial_synced_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="reservation_financial_syncs",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterModelOptions(
            name="bookingpayoutline",
            options={
                "ordering": ["line_number"],
                "permissions": [
                    (
                        "apply_booking_payout_line",
                        "Can sync booking payout to reservation",
                    ),
                ],
            },
        ),
    ]
