"""Import unit listing photos from a directory (ADR 0015 Phase A — no Channex flush)."""

from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.properties.models import Unit
from apps.properties.unit_photos.exceptions import UnitPhotoError
from apps.properties.unit_photos.service import UnitPhotoService
from apps.tenants.models import Tenant

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


class Command(BaseCommand):
    help = (
        "Import listing photos for a unit from a directory into UnitPhoto + PhotoOutbox. "
        "Does not upload to Channex (Phase B)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--tenant-slug", required=True)
        parser.add_argument("--unit-code", required=True)
        parser.add_argument(
            "--dir",
            required=True,
            help="Directory of jpeg/png/webp files (e.g. .wp_photos/R4)",
        )
        parser.add_argument(
            "--primary",
            default="",
            help="Basename of the file that should be primary (e.g. r4-19.jpeg).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List files only; do not write.",
        )

    def handle(self, *args, **options):
        tenant = Tenant.objects.filter(slug=options["tenant_slug"]).first()
        if tenant is None:
            raise CommandError(f"Tenant not found: {options['tenant_slug']}")
        unit = (
            Unit.objects.filter(tenant=tenant, code=options["unit_code"], is_active=True)
            .order_by("id")
            .first()
        )
        if unit is None:
            raise CommandError(
                f"Active unit {options['unit_code']!r} not found for tenant {tenant.slug}"
            )

        root = Path(options["dir"]).expanduser()
        if not root.is_dir():
            raise CommandError(f"Not a directory: {root}")

        files = sorted(
            (
                p
                for p in root.iterdir()
                if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
            ),
            key=lambda p: p.name.lower(),
        )
        if not files:
            raise CommandError(f"No image files in {root}")

        primary_name = (options["primary"] or "").strip()
        if primary_name and not any(p.name == primary_name for p in files):
            raise CommandError(f"--primary file not found in dir: {primary_name}")

        self.stdout.write(
            f"Importing {len(files)} file(s) → tenant={tenant.slug} unit={unit.code}"
        )
        if options["dry_run"]:
            for p in files:
                mark = " (primary)" if p.name == primary_name else ""
                self.stdout.write(f"  {p.name}{mark}")
            self.stdout.write(self.style.WARNING("Dry run — no writes."))
            return

        service = UnitPhotoService()
        created = 0
        skipped = 0
        for path in files:
            data = path.read_bytes()
            make_primary = bool(primary_name) and path.name == primary_name
            try:
                before_ids = set(
                    unit.photos.exclude(status="deleted").values_list("id", flat=True)
                )
                photo = service.add_photo(
                    unit,
                    data,
                    original_filename=path.name,
                    make_primary=make_primary,
                    actor="import_unit_photos",
                )
                if photo.pk in before_ids and not make_primary:
                    skipped += 1
                    self.stdout.write(
                        f"  skip duplicate checksum {path.name} → #{photo.pk}"
                    )
                else:
                    created += 1
                    primary = " primary" if photo.is_primary else ""
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"  {path.name} → photo#{photo.pk}{primary} [{photo.status}]"
                        )
                    )
            except UnitPhotoError as exc:
                raise CommandError(f"{path.name}: {exc}") from exc

        pending = (
            unit.photos.filter(outbox_entries__status="pending").distinct().count()
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Done: created/updated={created} skipped_dup={skipped}; "
                f"photos with pending outbox≈{pending} (no Channex flush in Phase A)"
            )
        )
