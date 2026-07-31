from django.core.management.base import BaseCommand, CommandError

from apps.integrations.channex.exceptions import PhotoSyncRetryableError
from apps.integrations.channex.management_mixins import ChannexWriteCommandMixin
from apps.integrations.channex.photo_outbox import flush_photo_outbox


class Command(ChannexWriteCommandMixin, BaseCommand):
    help = "Flush pending UnitPhoto PhotoOutbox entries to Channex (ADR 0015 Phase B)."

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument("--tenant-slug", default="uzorita")
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        with self.channex_write_context(options):
            try:
                summary = flush_photo_outbox(
                    tenant_slug=options["tenant_slug"],
                    limit=options["limit"],
                )
            except PhotoSyncRetryableError as exc:
                raise CommandError(f"Retryable photo flush failures: {exc}") from exc

        if summary.get("skipped"):
            self.stdout.write(self.style.WARNING(f"Skipped: {summary}"))
            return
        self.stdout.write(
            self.style.SUCCESS(
                f"photo outbox tenant={summary.get('tenant')} "
                f"sent={summary.get('sent')} failed={summary.get('failed')} "
                f"retry={summary.get('retry')} processed={summary.get('processed')}"
            )
        )
        for row in summary.get("results") or []:
            self.stdout.write(f"  {row}")
