# Generated manually for ADR 0010 Phase 5 messaging schedules.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0016_tenant_is_system"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenantreceptionsettings",
            name="pre_arrival_days_before",
            field=models.PositiveSmallIntegerField(
                blank=True,
                help_text="Tenant override: pre-arrival days_before. Null = platform default.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="tenantreceptionsettings",
            name="pre_arrival_schedule_strategy",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Tenant override: FIXED_TIME | FIRST_AFTER | IMMEDIATE. Blank = platform.",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="tenantreceptionsettings",
            name="pre_arrival_send_time",
            field=models.TimeField(
                blank=True,
                help_text="Tenant override: pre-arrival send_time. Null = platform default.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="tenantreceptionsettings",
            name="welcome_days_before",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="tenantreceptionsettings",
            name="welcome_schedule_strategy",
            field=models.CharField(blank=True, default="", max_length=16),
        ),
        migrations.AddField(
            model_name="tenantreceptionsettings",
            name="welcome_send_time",
            field=models.TimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="tenantreceptionsettings",
            name="whatsapp_welcome_days_before",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="tenantreceptionsettings",
            name="whatsapp_welcome_schedule_strategy",
            field=models.CharField(blank=True, default="", max_length=16),
        ),
        migrations.AddField(
            model_name="tenantreceptionsettings",
            name="whatsapp_welcome_send_time",
            field=models.TimeField(blank=True, null=True),
        ),
    ]
