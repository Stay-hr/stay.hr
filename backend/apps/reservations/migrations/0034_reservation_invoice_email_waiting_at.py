from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("reservations", "0033_guest_unique_document_per_reservation"),
    ]

    operations = [
        migrations.AddField(
            model_name="reservation",
            name="invoice_email_waiting_at",
            field=models.DateTimeField(
                blank=True,
                help_text="When set, inbound capture waits for a single usable invoice email.",
                null=True,
            ),
        ),
    ]
