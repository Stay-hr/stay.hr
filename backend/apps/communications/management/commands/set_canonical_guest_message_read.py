from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from apps.communications.canonical_read import (
    compare_timeline_parity,
    disable_canonical_read,
    enable_canonical_read,
    status_payload,
    validate_canonical_read,
)
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = (
        "Enable or disable canonical GuestMessage GET/inbox for one tenant "
        "(ADR 0019 D4). Default GET stays raw until --enable."
    )

    def add_arguments(self, parser):
        parser.add_argument("--tenant-slug", required=True)
        parser.add_argument("--enable", action="store_true")
        parser.add_argument("--disable", action="store_true")
        parser.add_argument("--parity", action="store_true")
        parser.add_argument("--validate", action="store_true")
        parser.add_argument("--status", action="store_true")

    def handle(self, *args, **options):
        modes = [
            name
            for name in ("enable", "disable", "parity", "validate", "status")
            if options[name]
        ]
        if len(modes) != 1:
            raise CommandError("Choose exactly one of --enable/--disable/--parity/--validate/--status")

        slug = (options["tenant_slug"] or "").strip()
        tenant = Tenant.objects.filter(slug=slug).first()
        if tenant is None:
            raise CommandError(f"Tenant not found: {slug}")

        mode = modes[0]
        try:
            if mode == "status":
                payload = status_payload(tenant)
            elif mode == "parity":
                payload = compare_timeline_parity(tenant)
            elif mode == "validate":
                payload = validate_canonical_read(tenant)
            elif mode == "enable":
                payload = enable_canonical_read(tenant)
            else:
                payload = disable_canonical_read(tenant)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(f"mode={mode} tenant={slug}")
        self.stdout.write(json.dumps(payload, indent=2, default=str))
        if mode in {"parity", "validate"} and payload.get("blocking_count"):
            self.stdout.write(self.style.WARNING(f"blocking={payload['blocking_count']}"))
        elif mode == "enable":
            self.stdout.write(self.style.SUCCESS("canonical-read-enabled"))
        elif mode == "disable":
            self.stdout.write(self.style.SUCCESS("canonical-read-disabled"))
