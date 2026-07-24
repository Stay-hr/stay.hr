from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("properties", "0017_property_self_service_mode"),
    ]

    operations = [
        migrations.AddField(
            model_name="property",
            name="settings_version",
            field=models.PositiveIntegerField(
                default=1,
                help_text="Optimistic-concurrency token for Property Settings PATCH (ADR 0008).",
            ),
        ),
    ]
