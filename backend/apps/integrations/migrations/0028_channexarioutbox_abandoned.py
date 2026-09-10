from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("integrations", "0027_whatsappmessage_source_received_at"),
    ]

    operations = [
        migrations.AlterField(
            model_name="channexarioutbox",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("sent", "Sent"),
                    ("failed", "Failed"),
                    ("abandoned", "Abandoned"),
                ],
                default="pending",
                max_length=16,
            ),
        ),
    ]
