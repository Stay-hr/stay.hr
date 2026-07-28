"""Merge registry welcome template langs into live WhatsApp IntegrationConfigs."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.integrations.whatsapp.welcome_template_config import merge_welcome_templates_all


class Command(BaseCommand):
    help = (
        "Merge-only: fill missing whatsapp_templates.welcome languages from the "
        "registry on all active WhatsApp IntegrationConfig rows. Never overwrites "
        "non-empty custom values."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show keys that would be added without saving.",
        )

    def handle(self, *args, **options):
        dry_run = bool(options["dry_run"])
        results = merge_welcome_templates_all(dry_run=dry_run)
        if not results:
            self.stdout.write("No active WhatsApp IntegrationConfig rows.")
            return

        changed = 0
        skipped = 0
        for entry in results:
            added = entry["added"]
            label = (
                f"id={entry['id']} tenant={entry['tenant']}"
                + (f" property_id={entry['property_id']}" if entry["property_id"] else "")
            )
            if entry.get("skipped"):
                skipped += 1
                self.stdout.write(
                    self.style.WARNING(f"SKIP {label}: {entry['skipped']}")
                )
                continue
            if added:
                changed += 1
                verb = "Would add" if dry_run else "Added"
                self.stdout.write(
                    self.style.SUCCESS(
                        f"{verb} {label}: {','.join(added)} "
                        f"(now {len(entry['welcome_keys'])} langs)"
                    )
                )
            else:
                self.stdout.write(
                    f"OK {label}: already complete ({len(entry['welcome_keys'])} langs)"
                )

        suffix = " (dry-run)" if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"Done{suffix}: {changed}/{len(results)} configs needed merge"
                + (f", {skipped} skipped" if skipped else "")
                + "."
            )
        )
