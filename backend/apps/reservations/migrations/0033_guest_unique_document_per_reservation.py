# Generated manually for identity consistency (ADR 0017)

from django.db import migrations, models


def _normalize_document_number(value: str) -> str:
    """Keep in sync with apps.reservations.document_intake_ocr_fixup.normalize_document_number.

    Inline copy so historical migrations do not import app code that may change.
    """
    import re

    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def cleanup_guest_document_numbers(apps, schema_editor):
    Guest = apps.get_model("reservations", "Guest")
    # Canonicalize non-empty document numbers.
    for guest in Guest.objects.exclude(document_number="").iterator():
        normalized = _normalize_document_number(guest.document_number)
        if normalized != guest.document_number:
            guest.document_number = normalized
            guest.save(update_fields=["document_number"])

    # Resolve duplicates per reservation: keep primary, else lowest pk.
    from collections import defaultdict

    by_key: dict[tuple[int, str], list] = defaultdict(list)
    for guest in Guest.objects.exclude(document_number="").order_by(
        "-is_primary", "pk"
    ):
        by_key[(guest.reservation_id, guest.document_number)].append(guest)

    for _key, guests in by_key.items():
        if len(guests) < 2:
            continue
        # First in order (-is_primary, pk) wins; blank the rest.
        for guest in guests[1:]:
            guest.document_number = ""
            guest.save(update_fields=["document_number"])


class Migration(migrations.Migration):
    dependencies = [
        ("reservations", "0032_heal_evisitor_checkout_failed"),
    ]

    operations = [
        migrations.RunPython(cleanup_guest_document_numbers, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="guest",
            constraint=models.UniqueConstraint(
                condition=~models.Q(document_number=""),
                fields=("reservation", "document_number"),
                name="reservations_guest_unique_doc_per_reservation",
            ),
        ),
    ]
