"""Store guest_imap_uidvalidity for a tenant that already has an IMAP cursor (verified, explicit)."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.communications.guest_imap_init import InitError, format_init_report, initialise_uidvalidity
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = (
        "Initialise guest_imap_uidvalidity. Dry-run by default; the operator must assert the "
        "UIDVALIDITY and the cursor, and the server must confirm the UIDVALIDITY."
    )

    def add_arguments(self, parser):
        parser.add_argument("--tenant", default="uzorita")
        parser.add_argument("--expect-uidvalidity", type=int, required=True)
        parser.add_argument("--expect-cursor", type=int, required=True)
        parser.add_argument("--replace-from", type=int, default=None,
                            help="Deliberately replace an already stored value (must equal it).")
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        tenant = Tenant.objects.filter(slug=(options["tenant"] or "").strip()).select_related("reception_settings").first()
        if tenant is None:
            raise CommandError(f"Tenant not found: {options['tenant']}")
        try:
            report = initialise_uidvalidity(
                tenant,
                expect_uidvalidity=options["expect_uidvalidity"],
                expect_cursor=options["expect_cursor"],
                apply=options["apply"],
                replace_from=options["replace_from"],
            )
        except InitError as exc:
            raise CommandError(str(exc)) from exc
        for line in format_init_report(report):
            self.stdout.write(line)
