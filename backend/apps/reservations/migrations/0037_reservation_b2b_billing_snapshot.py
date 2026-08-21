# Generated manually for PR1 B2B billing snapshot fields

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("reservations", "0036_guestcheckinsession_last_distributed_from"),
    ]

    operations = [
        migrations.AddField(
            model_name="reservation",
            name="buyer_address",
            field=models.TextField(
                blank=True,
                default="",
                help_text="B2B billing snapshot: company address.",
            ),
        ),
        migrations.AddField(
            model_name="reservation",
            name="buyer_company_name",
            field=models.CharField(
                blank=True,
                default="",
                help_text="B2B billing snapshot: company legal name for invoices/offers.",
                max_length=255,
            ),
        ),
        migrations.AddField(
            model_name="reservation",
            name="buyer_oib",
            field=models.CharField(
                blank=True,
                default="",
                help_text="B2B billing snapshot: company OIB (11 digits).",
                max_length=11,
            ),
        ),
        migrations.AddField(
            model_name="reservation",
            name="invoice_email",
            field=models.EmailField(
                blank=True,
                default="",
                help_text="B2B billing snapshot: preferred email for invoice/offer delivery.",
                max_length=254,
            ),
        ),
        migrations.AlterField(
            model_name="reservation",
            name="amount",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text=(
                    "Guest-facing gross/all-in stay total. Tourist tax is split out of this "
                    "amount on invoice build; never added on top."
                ),
                max_digits=12,
                null=True,
            ),
        ),
    ]
