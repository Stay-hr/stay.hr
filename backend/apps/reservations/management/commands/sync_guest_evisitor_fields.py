"""Backfill guest identity fields needed for eVisitor from MRZ and address rules."""

from __future__ import annotations

import re

from django.core.management.base import BaseCommand

from apps.integrations.evisitor.residence_address import validate_evisitor_residence_address
from apps.reservations.models import Guest
from apps.reservations.mrz_parse import parse_sex_from_mrz

# Geschlecht / Sex on German ID front (OCR fragments).
_SEX_PATTERNS = (
    re.compile(r"\b(?:GESCHLECHT|SEX|SPOL)\b[\s:./-]*([MFmf])\b", re.IGNORECASE),
    re.compile(r"\b([MFmf])\s*/\s*(?:MÄNNLICH|WEIBLICH|M|F)\b", re.IGNORECASE),
)


def _sex_from_ocr_text(text: str) -> str:
    for pattern in _SEX_PATTERNS:
        match = pattern.search(text or "")
        if match:
            return match.group(1).upper()
    return ""


class Command(BaseCommand):
    help = "Fill sex from MRZ/OCR text and normalize address for eVisitor."

    def add_arguments(self, parser):
        parser.add_argument("--guest-id", type=int, action="append", dest="guest_ids")
        parser.add_argument("--reservation-id", type=int)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        guest_ids = list(options.get("guest_ids") or [])
        if options.get("reservation_id"):
            guest_ids.extend(
                Guest.objects.filter(reservation_id=options["reservation_id"]).values_list(
                    "pk", flat=True
                )
            )
        if not guest_ids:
            self.stderr.write("Provide --guest-id or --reservation-id.")
            return

        updated = 0
        for guest_id in sorted(set(guest_ids)):
            guest = Guest.objects.get(pk=guest_id)
            fields: list[str] = []

            if guest.mrz_raw_text and not (guest.sex or "").strip():
                sex = parse_sex_from_mrz(guest.mrz_raw_text)
                if sex:
                    guest.sex = sex
                    fields.append("sex")

            if not (guest.sex or "").strip():
                ocr_text = self._ocr_text_for_guest(guest)
                sex = _sex_from_ocr_text(ocr_text)
                if sex:
                    guest.sex = sex
                    fields.append("sex")

            if guest.address:
                result = validate_evisitor_residence_address(guest.address)
                if result.valid and result.normalized_address != guest.address:
                    guest.address = result.normalized_address
                    fields.append("address")
                elif not result.valid:
                    self.stdout.write(
                        f"guest {guest_id}: address invalid — "
                        f"{result.errors[0] if result.errors else 'cannot determine city'}"
                    )

            if not fields:
                self.stdout.write(f"guest {guest_id}: nothing to update")
                continue

            if options.get("dry_run"):
                self.stdout.write(f"guest {guest_id}: would update {fields}")
            else:
                guest.save(update_fields=fields + ["updated_at"])
                self.stdout.write(f"guest {guest_id}: updated {fields}")
            updated += 1

        self.stdout.write(self.style.SUCCESS(f"Done: {updated} guests processed"))

    def _ocr_text_for_guest(self, guest: Guest) -> str:
        doc = guest.id_documents.order_by("-id").first()
        if doc is None:
            return ""
        person = (doc.extracted_payload or {}).get("person") or {}
        chunks = [str(person.get("ocr_text") or "")]
        for line in person.get("mrz_lines") or []:
            chunks.append(str(line))
        return "\n".join(chunks)
