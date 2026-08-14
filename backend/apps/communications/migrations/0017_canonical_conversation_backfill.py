import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("communications", "0016_canonical_conversation_store"),
        ("tenants", "0017_tenantreceptionsettings_messaging_schedules"),
    ]

    operations = [
        migrations.CreateModel(
            name="CanonicalConversationBackfill",
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
                ("cutoff_at", models.DateTimeField(blank=True, null=True)),
                ("cutoff_channex_id", models.PositiveIntegerField(blank=True, null=True)),
                ("cutoff_whatsapp_id", models.PositiveIntegerField(blank=True, null=True)),
                ("cutoff_inbound_id", models.PositiveIntegerField(blank=True, null=True)),
                ("cutoff_outbound_id", models.PositiveIntegerField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("snapshot", models.JSONField(blank=True, default=dict)),
                ("completed_by", models.CharField(blank=True, default="", max_length=128)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "tenant",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="canonical_backfill",
                        to="tenants.tenant",
                    ),
                ),
            ],
            options={
                "verbose_name": "Canonical conversation backfill",
                "verbose_name_plural": "Canonical conversation backfills",
            },
        ),
    ]
