from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.billing.services.eporezna.errors import EporeznaError
from apps.billing.services.eporezna.pdvs.validate import validate_pdvs_xml


class Command(BaseCommand):
    help = "Validate an Obrazac PDV-S XML file against the vendored XSD."

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True)

    def handle(self, *args, **options):
        path = Path(options["file"])
        if not path.is_file():
            raise CommandError(f"File not found: {path}")
        try:
            validate_pdvs_xml(path.read_bytes())
        except EporeznaError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS("OK"))
