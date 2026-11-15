"""Replay historical guest emails from IMAP without any live automation.

Dry-run is the default. See apps/communications/guest_email_replay.py for the guarantees.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.communications.guest_email_replay import (
    MAX_REPLAY_RANGE,
    ReplayError,
    format_report,
    replay_tenant_guest_inbox,
)
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = (
        "Record historical guest emails (UID range) WITHOUT LLM, auto-replies, push, language or "
        "reservation changes. Dry-run by default; --apply needs --expect-cursor."
    )

    def add_arguments(self, parser):
        parser.add_argument("--tenant", default="uzorita", help="Tenant slug (default: uzorita).")
        parser.add_argument("--from-uid", type=int, required=True)
        parser.add_argument("--to-uid", type=int, required=True)
        parser.add_argument("--apply", action="store_true", help="Write messages and move the cursor (default: dry-run).")
        parser.add_argument("--expect-cursor", type=int, default=None, help="Required with --apply: current guest_imap_last_uid.")
        parser.add_argument("--expect-uidvalidity", type=int, default=None, help="Refuse if the server UIDVALIDITY differs.")
        parser.add_argument("--accept-unmatched", action="store_true", help="Allow moving the cursor past emails whose booking has no reservation.")
        parser.add_argument("--max-range", type=int, default=MAX_REPLAY_RANGE)
        parser.add_argument("--allow-large-range", action="store_true")

    def handle(self, *args, **options):
        slug = (options["tenant"] or "").strip()
        tenant = Tenant.objects.filter(slug=slug).select_related("reception_settings").first()
        if tenant is None:
            raise CommandError(f"Tenant not found: {slug}")
        try:
            report = replay_tenant_guest_inbox(
                tenant,
                from_uid=options["from_uid"],
                to_uid=options["to_uid"],
                apply=options["apply"],
                expect_cursor=options["expect_cursor"],
                expect_uidvalidity=options["expect_uidvalidity"],
                accept_unmatched=options["accept_unmatched"],
                max_range=options["max_range"],
                allow_large_range=options["allow_large_range"],
            )
        except ReplayError as exc:
            raise CommandError(str(exc)) from exc
        for line in format_report(report):
            self.stdout.write(line)
