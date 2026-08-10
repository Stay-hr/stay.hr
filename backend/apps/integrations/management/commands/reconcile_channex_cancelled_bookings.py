from django.core.management.base import BaseCommand, CommandError

from apps.integrations.channex.cancel_reconcile import (
    DEFAULT_BATCH_LIMIT,
    reconcile_channex_cancelled_bookings,
)
from apps.integrations.models import IntegrationConfig


class Command(BaseCommand):
    help = (
        "Cancel-only safety net: compare local open Channex reservations to "
        "GET /bookings/:id and heal missed cancels."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant-slug",
            default="uzorita",
            help="Tenant slug (default: uzorita).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Call get_booking only; do not heal or write.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=DEFAULT_BATCH_LIMIT,
            help=f"Max candidates to check (default: {DEFAULT_BATCH_LIMIT}).",
        )

    def handle(self, *args, **options):
        tenant_slug = options["tenant_slug"]
        dry_run = bool(options["dry_run"])
        limit = int(options["limit"])

        rows = list(
            IntegrationConfig.objects.filter(
                tenant__slug=tenant_slug,
                provider=IntegrationConfig.Provider.CHANNEX,
                is_active=True,
            ).select_related("tenant")
        )
        if not rows:
            raise CommandError(f"No active Channex IntegrationConfig for tenant {tenant_slug}")
        if len(rows) > 1:
            raise CommandError(
                f"Ambiguous Channex IntegrationConfig for tenant {tenant_slug}: "
                f"{len(rows)} active rows"
            )

        stats = reconcile_channex_cancelled_bookings(
            rows[0],
            dry_run=dry_run,
            limit=limit,
        )

        self.stdout.write(
            f"candidates={stats['candidates']} api_checked={stats['api_checked']} "
            f"healed={stats['healed']} would_heal={stats['would_heal']} "
            f"noop_active={stats['noop_active']} remote_not_found={stats['remote_not_found']} "
            f"invalid_remote_payload={stats['invalid_remote_payload']} "
            f"skipped_unparseable_id={stats['skipped_unparseable_id']} "
            f"errors={stats['errors']}"
        )
        healed_ids = stats.get("healed_ids") or []
        if healed_ids:
            self.stdout.write(self.style.SUCCESS(f"healed_ids={healed_ids}"))
        elif dry_run and stats.get("would_heal"):
            self.stdout.write(
                self.style.WARNING(f"would_heal={stats['would_heal']} (dry-run, no writes)")
            )
        else:
            self.stdout.write(self.style.WARNING("No cancels healed."))
