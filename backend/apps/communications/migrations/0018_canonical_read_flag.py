from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("communications", "0017_canonical_conversation_backfill"),
    ]

    operations = [
        migrations.AddField(
            model_name="canonicalconversationbackfill",
            name="read_canonical_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="canonicalconversationbackfill",
            name="read_canonical_by",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="canonicalconversationbackfill",
            name="read_snapshot",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
