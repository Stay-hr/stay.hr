# Generated manually for ADR 0010 Phase 5 messaging schedules.

from datetime import time

from django.db import migrations, models


def copy_autocheckin_time_to_whatsapp_welcome(apps, schema_editor):
    Property = apps.get_model("properties", "Property")
    for prop in Property.objects.all().iterator():
        # Freeze current autocheck-in clock onto the orchestration field.
        prop.whatsapp_welcome_send_time = prop.whatsapp_autocheckin_time or time(8, 0)
        prop.save(update_fields=["whatsapp_welcome_send_time"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("properties", "0018_property_settings_version"),
    ]

    operations = [
        migrations.AddField(
            model_name="property",
            name="pre_arrival_days_before",
            field=models.PositiveSmallIntegerField(
                blank=True,
                help_text="Override: days before check-in for CHECKIN_INFO/LINK. Null = inherit.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="property",
            name="pre_arrival_schedule_strategy",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Override: FIXED_TIME | FIRST_AFTER | IMMEDIATE. Blank = inherit.",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="property",
            name="pre_arrival_send_time",
            field=models.TimeField(
                blank=True,
                help_text="Override: local send clock for pre-arrival. Null = inherit.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="property",
            name="welcome_days_before",
            field=models.PositiveSmallIntegerField(
                blank=True,
                help_text="Override: days before check-in for generic WELCOME schedule. Null = inherit.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="property",
            name="welcome_schedule_strategy",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Override: FIXED_TIME | FIRST_AFTER | IMMEDIATE. Blank = inherit.",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="property",
            name="welcome_send_time",
            field=models.TimeField(
                blank=True,
                help_text="Override: local send clock for welcome_*. Null = inherit.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="property",
            name="whatsapp_welcome_days_before",
            field=models.PositiveSmallIntegerField(
                blank=True,
                help_text="Override: days before check-in for WhatsApp WELCOME. Null = inherit.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="property",
            name="whatsapp_welcome_schedule_strategy",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Override: FIXED_TIME | FIRST_AFTER | IMMEDIATE. Blank = inherit.",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="property",
            name="whatsapp_welcome_send_time",
            field=models.TimeField(
                blank=True,
                help_text=(
                    "Override: local send clock for WhatsApp welcome. Null falls back to "
                    "whatsapp_autocheckin_time then Tenant → Platform."
                ),
                null=True,
            ),
        ),
        migrations.RunPython(
            copy_autocheckin_time_to_whatsapp_welcome,
            noop_reverse,
        ),
    ]
