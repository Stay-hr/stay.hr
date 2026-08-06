from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("reservations", "0034_reservation_invoice_email_waiting_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="reservation",
            name="expected_checkin_adults",
            field=models.PositiveSmallIntegerField(
                blank=True,
                help_text=(
                    "Check-in domain override: adults expected to submit identity "
                    "documents. Null means use OTA adults_count. OTA importers must "
                    "never write this field."
                ),
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="guestcheckinsession",
            name="ops_version",
            field=models.PositiveIntegerField(
                default=0,
                help_text=(
                    "Optimistic-lock revision for occupancy/commit/complete. "
                    "Not bumped on draft autosave PATCH."
                ),
            ),
        ),
    ]
