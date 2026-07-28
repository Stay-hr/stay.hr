# Generated manually for MessageDispatchEventType.DEFERRED

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("communications", "0013_message_orchestration_outbox"),
    ]

    operations = [
        migrations.AlterField(
            model_name="messagedispatchevent",
            name="event_type",
            field=models.CharField(
                choices=[
                    ("DISPATCH_CREATED", "Dispatch created"),
                    ("RENDERED", "Rendered"),
                    ("CHANNEL_SELECTED", "Channel selected"),
                    ("FALLBACK", "Fallback"),
                    ("DEFERRED", "Deferred"),
                    ("DELIVERED", "Delivered"),
                    ("FAILED", "Failed"),
                    ("CANCELLED", "Cancelled"),
                    ("SKIPPED", "Skipped"),
                    ("REPLAYED", "Replayed"),
                ],
                max_length=32,
            ),
        ),
    ]
