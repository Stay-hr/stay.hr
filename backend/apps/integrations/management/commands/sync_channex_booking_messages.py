from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.communications.conversation_ingest_status import mark_conversation_ingest
from apps.integrations.channex.ari_service import get_active_channex_integration
from apps.integrations.channex.exceptions import ChannexApiError, ChannexBookingIngestError
from apps.integrations.channex.message_service import sync_booking_messages_from_channex
from apps.reservations.models import Reservation


class Command(BaseCommand):
    help = "Pull guest messages from Channex for one or all Channex-linked reservations."

    def add_arguments(self, parser):
        parser.add_argument("--tenant-slug", type=str, default="uzorita")
        parser.add_argument(
            "--reservation-id",
            type=int,
            default=None,
            help="Sync a single reservation; omit to sync all channex reservations.",
        )

    def handle(self, *args, **options):
        tenant_slug = options["tenant_slug"]
        reservation_id = options["reservation_id"]

        try:
            integration = get_active_channex_integration(tenant_slug)
        except ChannexBookingIngestError as exc:
            raise CommandError(str(exc)) from exc

        reservations = Reservation.objects.filter(
            tenant__slug=tenant_slug,
            import_source="channex",
        ).order_by("pk")
        if reservation_id is not None:
            reservations = reservations.filter(pk=reservation_id)

        if not reservations.exists():
            raise CommandError("No matching Channex reservations found.")

        total_rows = 0
        for reservation in reservations:
            try:
                rows = sync_booking_messages_from_channex(integration, reservation)
            except (ChannexBookingIngestError, ChannexApiError) as exc:
                self.stderr.write(
                    self.style.WARNING(
                        f"Skipped reservation #{reservation.pk} "
                        f"({reservation.booking_code}): {exc}"
                    )
                )
                continue
            total_rows += len(rows)
            self.stdout.write(
                f"#{reservation.pk} {reservation.booking_code or reservation.external_id}: "
                f"{len(rows)} message(s)"
            )

        mark_conversation_ingest("channex", "poll")
        self.stdout.write(self.style.SUCCESS(f"Synced {total_rows} message row(s)."))
