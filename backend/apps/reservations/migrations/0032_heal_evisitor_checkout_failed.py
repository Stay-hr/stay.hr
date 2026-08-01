"""Heal guests mislabeled as check-in failed after a successful CheckIn + failed CheckOut."""

from django.db import migrations


def heal_checkout_failed(apps, schema_editor):
    Guest = apps.get_model("reservations", "Guest")
    EvisitorSubmission = apps.get_model("reservations", "EvisitorSubmission")

    candidates = Guest.objects.filter(
        evisitor_status="failed",
        evisitor_registration_id__isnull=False,
    ).only("id", "evisitor_status")

    for guest in candidates.iterator():
        submissions = list(
            EvisitorSubmission.objects.filter(guest_id=guest.pk)
            .order_by("-created_at", "-id")
            .only("id", "status", "request_payload", "created_at")
        )
        if not submissions:
            continue

        has_successful_checkin = False
        for sub in submissions:
            payload = sub.request_payload or {}
            if not isinstance(payload, dict):
                continue
            if sub.status == "sent" and "CheckOutDate" not in payload:
                has_successful_checkin = True
                break

        if not has_successful_checkin:
            continue

        latest_failed = next((s for s in submissions if s.status == "failed"), None)
        if latest_failed is None:
            continue
        failed_payload = latest_failed.request_payload or {}
        if not isinstance(failed_payload, dict):
            continue
        if "CheckOutDate" not in failed_payload:
            continue

        Guest.objects.filter(pk=guest.pk, evisitor_status="failed").update(
            evisitor_status="checkout_failed"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("reservations", "0031_guestportalaccess"),
    ]

    operations = [
        migrations.RunPython(heal_checkout_failed, migrations.RunPython.noop),
    ]
