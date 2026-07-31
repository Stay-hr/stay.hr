from django.core.management.base import BaseCommand
from django.db import transaction

from apps.properties.beds import UZORITA_BED_SEED_UNIT_CODES, beds_for_unit
from apps.properties.models import Property, Unit, UnitBed
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = (
        "Seed Booking.com standard bed arrangement for Uzorita units "
        "(R1/R2/R3/R6: Queen x1 + Sofa bed x1; R4: King x1)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--tenant-slug", default="uzorita")
        parser.add_argument("--property-slug", default="uzorita")
        parser.add_argument(
            "--unit-codes",
            nargs="+",
            default=list(UZORITA_BED_SEED_UNIT_CODES),
            help="Unit codes to seed (default: R1 R2 R3 R4 R6).",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        tenant = Tenant.objects.filter(slug=options["tenant_slug"]).first()
        if tenant is None:
            self.stderr.write(self.style.ERROR(f"Tenant not found: {options['tenant_slug']}"))
            return

        prop = Property.objects.filter(tenant=tenant, slug=options["property_slug"]).first()
        if prop is None:
            self.stderr.write(
                self.style.ERROR(
                    f"Property not found: {options['property_slug']} "
                    f"(tenant {options['tenant_slug']})"
                )
            )
            return

        unit_codes = [code.strip().upper() for code in options["unit_codes"] if code.strip()]
        total_beds = 0

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"Seeding beds for tenant={tenant.slug} property={prop.slug}"
            )
        )

        for code in unit_codes:
            unit = Unit.objects.filter(tenant=tenant, property=prop, code=code).first()
            if unit is None:
                self.stdout.write(self.style.WARNING(f"  SKIP {code} — unit not found"))
                continue

            deleted, _ = UnitBed.objects.filter(tenant=tenant, unit=unit).delete()
            created = 0
            for spec in beds_for_unit(code):
                UnitBed.objects.create(
                    tenant=tenant,
                    unit=unit,
                    bed_type=spec["bed_type"],
                    count=spec["count"],
                    sort_order=spec["sort_order"],
                )
                created += 1

            total_beds += created
            self.stdout.write(
                f"  {unit.code}: replaced {deleted} bed row(s), created {created} "
                f"({unit.get_beds_display()})"
            )

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"Done — {total_beds} bed row(s) across {len(unit_codes)} unit code(s)."
            )
        )
        self.stdout.write(
            "Note: R6 = Booking listing R6 (formerly D1). Override with --unit-codes if needed."
        )
