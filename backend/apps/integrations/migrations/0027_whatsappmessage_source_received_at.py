from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("integrations", "0026_whatsapp_platform_routing"),
    ]

    operations = [
        migrations.AddField(
            model_name="whatsappmessage",
            name="source",
            field=models.CharField(
                choices=[
                    ("cloud_api", "Cloud API"),
                    ("business_app", "Business App"),
                ],
                default="cloud_api",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="whatsappmessage",
            name="received_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
