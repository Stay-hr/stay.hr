from __future__ import annotations

from datetime import date

from django.core.management.base import BaseCommand

from apps.integrations.channex.availability_verify_service import (
    DEFAULT_VERIFY_DAYS,
    verify_and_repair_availability,
)
from apps.integrations.channex.management_mixins import ChannexWriteCommandMixin

# Ops convenience default only — service requires an explicit tenant_slug.
OPS_DEFAULT_TENANT_SLUG = "uzorita"


class Command(ChannexWriteCommandMixin, BaseCommand):
    help = (
        "Verify stay.hr occupancy vs live Channex GET /availability "
        "(any Channex tenant; default slug: uzorita). "
        "Default is verify-only (Breaking operational change 2026-08: bare "
        "command no longer repairs). Pass --repair on the writer host to re-push."
    )

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            "--tenant-slug",
            default=OPS_DEFAULT_TENANT_SLUG,
            help=f"Tenant slug (default: {OPS_DEFAULT_TENANT_SLUG}).",
        )
        parser.add_argument(
            "--days",
            type=int,
            default=DEFAULT_VERIFY_DAYS,
            help=f"Number of nights to verify from --from-date (default: {DEFAULT_VERIFY_DAYS}).",
        )
        parser.add_argument(
            "--from-date",
            type=str,
            default="",
            help="Start date YYYY-MM-DD (default: today).",
        )
        parser.add_argument(
            "--repair",
            action="store_true",
            help=(
                "Re-push ARI for mismatches on an authorized writer host. "
                "Subject to OutboundGuard + blast-radius threshold."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Alias of default behaviour (verify-only; no repair).",
        )
        parser.add_argument(
            "--no-notify",
            action="store_true",
            help="Do not send reception push on mismatches.",
        )

    def handle(self, *args, **options):
        from_date = self._parse_from_date(options["from_date"])
        do_repair = bool(options["repair"]) and not bool(options["dry_run"])
        notify = not bool(options["no_notify"])

        if do_repair:
            with self.channex_write_context(options):
                result = verify_and_repair_availability(
                    tenant_slug=options["tenant_slug"],
                    days=options["days"],
                    from_date=from_date,
                    repair=True,
                    notify=notify,
                    caller="cli",
                )
        else:
            result = verify_and_repair_availability(
                tenant_slug=options["tenant_slug"],
                days=options["days"],
                from_date=from_date,
                repair=False,
                notify=notify,
                caller="cli",
            )

        if result.get("skipped"):
            self.stderr.write(
                self.style.ERROR(
                    f"Skipped: {result.get('reason')} (tenant={options['tenant_slug']})"
                )
            )
            raise SystemExit(2)

        mismatch_count = int(result.get("mismatch_count") or 0)
        repaired = int(result.get("repaired") or 0)
        self.stdout.write(
            f"Tenant {result.get('tenant_slug')} "
            f"{result.get('from_date')}..{result.get('to_date')}: "
            f"units={result.get('units_checked')} "
            f"mismatches={mismatch_count} repaired={repaired}"
        )
        for row in result.get("mismatches") or []:
            self.stdout.write(
                f"  {row['unit_code']} {row['day']}: "
                f"expected={row['expected']} channex={row['actual']}"
            )

        if result.get("repair_skipped"):
            blast = result.get("blast_radius") or {}
            self.stderr.write(
                self.style.WARNING(
                    f"Repair skipped ({result.get('repair_skip_reason')}): "
                    f"units={blast.get('units')} "
                    f"affected_percent={blast.get('affected_percent')} "
                    f"max_days={blast.get('max_days')} "
                    f"reasons={blast.get('reasons')}"
                )
            )

        if mismatch_count:
            if do_repair and repaired:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Found {mismatch_count} mismatch(es); repaired={repaired}."
                    )
                )
                return
            self.stdout.write(
                self.style.WARNING(
                    f"Found {mismatch_count} mismatch(es) (verify-only, not repaired)."
                )
            )
            self.stderr.write(
                "Hint: mismatches not repaired; pass --repair on the writer host "
                "(CHANNEX_OUTBOUND_ENABLED=true). "
                "Breaking change 2026-08: bare command is verify-only."
            )
            raise SystemExit(1)

        self.stdout.write(self.style.SUCCESS("No availability mismatches."))

    @staticmethod
    def _parse_from_date(raw: str) -> date | None:
        if not raw:
            return None
        try:
            return date.fromisoformat(raw.strip())
        except ValueError as exc:
            raise ValueError(f"Invalid --from-date {raw!r}; use YYYY-MM-DD.") from exc
