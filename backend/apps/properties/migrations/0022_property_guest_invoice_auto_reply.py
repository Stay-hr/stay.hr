from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("properties", "0021_unitphotolink_tombstone"),
    ]

    operations = [
        migrations.AddField(
            model_name="property",
            name="guest_invoice_auto_reply_enabled",
            field=models.BooleanField(
                default=True,
                help_text=(
                    "Auto-reply when guest asks for an invoice and capture a usable email "
                    "(WhatsApp, email, Channex). Checkout still issues/sends the invoice."
                ),
            ),
        ),
    ]
