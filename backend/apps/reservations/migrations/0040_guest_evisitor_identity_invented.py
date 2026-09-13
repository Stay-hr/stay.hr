import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("reservations", "0039_guest_payment_access"),
    ]

    operations = [
        migrations.AddField(
            model_name="guest",
            name="evisitor_identity_invented_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="guest",
            name="evisitor_identity_invented_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="evisitor_identity_invented_guests",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
