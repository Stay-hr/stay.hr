from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from apps.communications.canonical_backfill import run_canonical_backfill
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = (
        "Backfill Conversation / GuestMessage / GuestMessageSource from today's "
        "timeline merge groups (ADR 0019 D3). GET is unchanged."
    )

    def add_arguments(self, parser):
        parser.add_argument("--tenant-slug", required=True, help="Tenant slug (required).")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Simulate apply and print would_create_*; no writes.",
        )
        parser.add_argument(
            "--validate-only",
            action="store_true",
            help="Check current DB state; no apply simulation and no writes.",
        )
        parser.add_argument(
            "--mark-complete",
            action="store_true",
            help="Full-tenant validate against stored cutoff, then set completed_at.",
        )
        parser.add_argument("--reservation-id", type=int, default=None)
        parser.add_argument("--resume-after-reservation-id", type=int, default=None)

    def handle(self, *args, **options):
        dry_run = bool(options["dry_run"])
        validate_only = bool(options["validate_only"])
        mark_complete = bool(options["mark_complete"])
        reservation_id = options["reservation_id"]
        resume_after = options["resume_after_reservation_id"]

        if mark_complete and dry_run:
            raise CommandError("--mark-complete cannot be combined with --dry-run")
        if mark_complete and reservation_id is not None:
            raise CommandError("--mark-complete cannot be combined with --reservation-id")
        if mark_complete and resume_after is not None:
            raise CommandError(
                "--mark-complete cannot be combined with --resume-after-reservation-id"
            )
        if dry_run and validate_only:
            raise CommandError("--dry-run cannot be combined with --validate-only")
        if mark_complete and validate_only:
            raise CommandError("--mark-complete already runs a full-tenant validate")

        slug = (options["tenant_slug"] or "").strip()
        tenant = Tenant.objects.filter(slug=slug).first()
        if tenant is None:
            raise CommandError(f"Tenant not found: {slug}")

        try:
            report = run_canonical_backfill(
                tenant,
                dry_run=dry_run,
                validate_only=validate_only,
                mark_complete=mark_complete,
                reservation_id=reservation_id,
                resume_after_reservation_id=resume_after,
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        payload = report.as_dict()
        mode = "apply"
        if dry_run:
            mode = "dry-run"
        elif validate_only:
            mode = "validate-only"
        elif mark_complete:
            mode = "mark-complete"
        self.stdout.write(f"mode={mode} tenant={slug}")
        self.stdout.write(json.dumps(payload, indent=2, default=str))
        if report.blocking_count:
            self.stdout.write(self.style.WARNING(f"blocking={report.blocking_count}"))
        elif mark_complete:
            self.stdout.write(self.style.SUCCESS("backfill-complete"))
