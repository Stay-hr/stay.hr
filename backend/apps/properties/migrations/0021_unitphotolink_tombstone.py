# Generated manually for ADR 0015 Phase B — UnitPhotoLink tombstone fields.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("properties", "0020_unit_photos_adr0015"),
    ]

    operations = [
        migrations.AddField(
            model_name="unitphotolink",
            name="deleted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="unitphotolink",
            name="deleted_checksum",
            field=models.CharField(blank=True, max_length=64),
        ),
    ]
