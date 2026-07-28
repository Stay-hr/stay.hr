from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.billing.services.eporezna.errors import EporeznaError
from apps.billing.services.eporezna.import_service import import_foreign_service_invoice
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Import an EU reverse-charge service invoice PDF into ForeignServiceInvoice."

    def add_arguments(self, parser):
        parser.add_argument("--tenant-slug", required=True)
        parser.add_argument("--file", required=True, help="Path to invoice PDF")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Parse and validate without saving",
        )

    def handle(self, *args, **options):
        slug = options["tenant_slug"]
        path = Path(options["file"])
        dry_run = options["dry_run"]

        try:
            tenant = Tenant.objects.get(slug=slug)
        except Tenant.DoesNotExist as exc:
            raise CommandError(f"Unknown tenant slug: {slug}") from exc

        if not path.is_file():
            raise CommandError(f"File not found: {path}")

        raw = path.read_bytes()
        try:
            result = import_foreign_service_invoice(
                tenant=tenant,
                raw=raw,
                filename=path.name,
                dry_run=dry_run,
            )
        except EporeznaError as exc:
            raise CommandError(str(exc)) from exc

        dto = result.dto
        self.stdout.write(f"provider:     {dto.provider}")
        self.stdout.write(f"invoice:      {dto.invoice_number}")
        self.stdout.write(f"period:       {dto.tax_period}")
        self.stdout.write(f"amount:       {dto.taxable_amount} {dto.currency}")
        self.stdout.write(f"VAT:          {dto.supplier_country}{dto.supplier_vat_id}")
        self.stdout.write(f"sha256:       {result.document_sha256[:16]}…")
        if result.already_imported:
            self.stdout.write(self.style.WARNING("already imported (idempotent)"))
        if dry_run:
            self.stdout.write(self.style.SUCCESS("OK (dry-run, not saved)"))
        else:
            assert result.invoice is not None
            self.stdout.write(self.style.SUCCESS(f"OK id={result.invoice.pk}"))
