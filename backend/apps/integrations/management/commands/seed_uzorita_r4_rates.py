from datetime import date

from django.core.management.base import BaseCommand

from apps.integrations.models import SalesChannel
from apps.integrations.pricing.r4_derived import (
    R4_RATE_PLAN_CODE,
    R4_SALES_CHANNELS,
    R4_SOURCE_UNIT_CODES,
    R4_TARGET_UNIT_CODE,
    sync_r4_rates_from_king_band,
)


class Command(BaseCommand):
    help = (
        "Derive Uzorita R4 RatePlanDay rows as 90% of R1/R2 same-day base "
        "(prefer R1, fall back to R2). OBP stays the same as R1 (primary occ=2, Δ€5)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--tenant-slug", default="uzorita")
        parser.add_argument("--property-slug", default="uzorita")
        parser.add_argument("--rate-plan-code", default=R4_RATE_PLAN_CODE)
        parser.add_argument(
            "--sales-channel",
            action="append",
            dest="sales_channels",
            choices=[choice.value for choice in SalesChannel],
            help=(
                "Sales channel to sync (repeatable). "
                f"Default: {' '.join(R4_SALES_CHANNELS)}."
            ),
        )
        parser.add_argument("--date-from", default="", help="Inclusive YYYY-MM-DD")
        parser.add_argument("--date-to", default="", help="Inclusive YYYY-MM-DD")
        parser.add_argument(
            "--no-push",
            action="store_true",
            help="Write RatePlanDay only; do not enqueue Channex outbox.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report created/updated counts without writing.",
        )

    def handle(self, *args, **options):
        date_from_raw = (options.get("date_from") or "").strip()
        date_to_raw = (options.get("date_to") or "").strip()
        date_from = date.fromisoformat(date_from_raw) if date_from_raw else None
        date_to = date.fromisoformat(date_to_raw) if date_to_raw else None
        sales_channels = tuple(options["sales_channels"] or R4_SALES_CHANNELS)

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"R4 derived rates ({R4_TARGET_UNIT_CODE} = 90% of "
                f"{'/'.join(R4_SOURCE_UNIT_CODES)}) "
                f"tenant={options['tenant_slug']} property={options['property_slug']}"
            )
        )

        try:
            results = sync_r4_rates_from_king_band(
                tenant_slug=options["tenant_slug"],
                property_slug=options["property_slug"],
                rate_plan_code=options["rate_plan_code"],
                sales_channels=sales_channels,
                date_from=date_from,
                date_to=date_to,
                queue_push=not options["no_push"],
                dry_run=options["dry_run"],
            )
        except ValueError as exc:
            self.stderr.write(self.style.ERROR(str(exc)))
            return

        for row in results:
            mode = "dry-run" if row.dry_run else "written"
            self.stdout.write(
                f"  {row.sales_channel}: source_days={row.source_days} "
                f"created={row.created} updated={row.updated} "
                f"unchanged={row.unchanged} {mode}={row.written}"
            )

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Done."))
