from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("reservations", "0035_expected_checkin_adults_ops_version"),
    ]

    operations = [
        migrations.AddField(
            model_name="guestcheckinsession",
            name="last_distributed_from",
            field=models.CharField(
                blank=True,
                choices=[
                    ("email", "Email"),
                    ("whatsapp_autocheckin", "WhatsApp autocheck-in"),
                    ("channex", "Channex"),
                    ("reception_manual", "Reception manual"),
                ],
                help_text=(
                    "Last channel that successfully delivered the check-in link. "
                    "Null until first successful send; never set on ensure/reuse alone."
                ),
                max_length=32,
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="guestcheckinsession",
            name="created_from",
            field=models.CharField(
                choices=[
                    ("email", "Email"),
                    ("whatsapp_autocheckin", "WhatsApp autocheck-in"),
                    ("channex", "Channex"),
                    ("reception_manual", "Reception manual"),
                ],
                help_text="Immutable first origin of this session (analytics).",
                max_length=32,
            ),
        ),
    ]
