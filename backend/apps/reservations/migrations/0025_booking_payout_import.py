from django.conf import settings
from django.db import migrations


class Migration(migrations.Migration):
    """No-op: tables are created by 0028_booking_payout_import (canonical leaf).

    Both leaves exist so historical production graphs merge cleanly via 0030.
    Fresh databases must not CreateModel twice.
    """

    dependencies = [
        ("properties", "0012_property_guest_parking_auto_reply"),
        ("reservations", "0024_reservationversion"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = []
