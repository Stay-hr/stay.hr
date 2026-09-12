import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0008_billing_recipient"),
    ]

    operations = [
        migrations.AlterField(
            model_name="invoice",
            name="reservation",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="invoices",
                to="reservations.reservation",
            ),
        ),
    ]
