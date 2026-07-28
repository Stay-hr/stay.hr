from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.billing.services.eporezna.errors import EporeznaError
from apps.billing.services.eporezna.pdvs.builder import PDVSBuilder
from apps.billing.services.eporezna.pdvs.validate import validate_pdvs_xml
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Export Obrazac PDV-S XML for a tenant tax period (YYYY-MM)."

    def add_arguments(self, parser):
        parser.add_argument("--tenant-slug", required=True)
        parser.add_argument("--period", required=True, help="Tax period YYYY-MM")
        parser.add_argument(
            "--out",
            default="",
            help="Output path (default: ./PDV-S_<oib>_<from>-<to>.xml)",
        )
        parser.add_argument(
            "--skip-validate",
            action="store_true",
            help="Skip XSD validation before writing",
        )

    def handle(self, *args, **options):
        slug = options["tenant_slug"]
        period = options["period"]
        try:
            tenant = Tenant.objects.get(slug=slug)
        except Tenant.DoesNotExist as exc:
            raise CommandError(f"Unknown tenant slug: {slug}") from exc

        try:
            export = PDVSBuilder().build(tenant=tenant, period=period)
            if not options["skip_validate"]:
                validate_pdvs_xml(export.xml_bytes)
        except EporeznaError as exc:
            raise CommandError(str(exc)) from exc

        out = Path(options["out"] or export.filename)
        out.write_bytes(export.xml_bytes)
        self.stdout.write(
            self.style.SUCCESS(
                f"Wrote {out} (invoices in period: {export.invoice_count})"
            )
        )
