from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0010_invoice_replacement"),
    ]

    operations = [
        migrations.AddField(
            model_name="invoice",
            name="issuer_name",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Frozen issuer name at issue time. Empty on legacy invoices.",
                max_length=255,
            ),
        ),
        migrations.AddField(
            model_name="invoice",
            name="issuer_address",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Frozen issuer address at issue time. Empty on legacy invoices.",
            ),
        ),
        migrations.AddField(
            model_name="invoice",
            name="issuer_oib",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Frozen issuer OIB at issue time. Empty on legacy invoices.",
                max_length=11,
            ),
        ),
        migrations.AddField(
            model_name="invoice",
            name="issuer_iban",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Frozen issuer IBAN at issue time. Empty on legacy invoices.",
                max_length=34,
            ),
        ),
        migrations.AddField(
            model_name="invoice",
            name="operator_code",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Frozen operator mark at issue time. Empty on legacy invoices.",
                max_length=64,
            ),
        ),
        migrations.AddField(
            model_name="invoice",
            name="business_premise_code",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Frozen business premise code at issue time. Empty on legacy invoices.",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="invoice",
            name="payment_device_code",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Frozen payment device code at issue time. Empty on legacy invoices.",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="invoice",
            name="reservation_reference",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Frozen reservation reference at issue time. Empty on legacy invoices.",
                max_length=255,
            ),
        ),
    ]
