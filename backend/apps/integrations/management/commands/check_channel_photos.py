"""Ops smoke: stay.hr ↔ Channex photo sync (ADR 0015 ChannelSyncObserver)."""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from apps.integrations.channex.observers.photos import ChannelSyncObserver


class Command(BaseCommand):
    help = (
        "Read-only ChannelSyncObserver compare (ADR 0015 Layer 2). "
        "Verifies stay.hr ↔ Channex; never writes. Booking.com galleries are "
        "OTA-managed (outside this command)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--tenant-slug", default="uzorita")
        parser.add_argument(
            "--unit-code",
            action="append",
            dest="unit_codes",
            default=None,
            help="Limit to unit code(s); repeatable (e.g. --unit-code R4).",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Print full JSON contract to stdout.",
        )

    def handle(self, *args, **options):
        tenant_slug = options["tenant_slug"]
        unit_codes = options.get("unit_codes") or None
        try:
            result = ChannelSyncObserver().compare(
                tenant_slug=tenant_slug,
                unit_codes=unit_codes,
            )
        except Exception as exc:  # noqa: BLE001
            raise CommandError(f"check_channel_photos failed: {exc}") from exc

        if options["json"]:
            self.stdout.write(json.dumps(result, indent=2, sort_keys=True))
        else:
            status = result.get("status")
            prop = result.get("property") or {}
            self.stdout.write(
                f"status={status} stay={prop.get('stay_hr')} "
                f"channex={prop.get('channex')} "
                f"outbox_pending={prop.get('outbox_pending')} "
                f"outbox_failed={prop.get('outbox_failed')} "
                f"last_sync={prop.get('last_successful_sync_at')}"
            )
            if result.get("error"):
                self.stdout.write(self.style.WARNING(f"error={result['error']}"))
            for room in result.get("rooms") or []:
                self.stdout.write(
                    f"  {room.get('code')}: stay={room.get('stay_hr')} "
                    f"channex={room.get('channex')} cover={room.get('cover')} "
                    f"positions_ok={room.get('positions_ok')} "
                    f"mapped={room.get('room_mapped')}"
                )

        status = result.get("status")
        if status == "PHOTO_SYNC_OK":
            self.stdout.write(self.style.SUCCESS("PHOTO_SYNC_OK"))
            return
        if status == "CHANNEX_UNAVAILABLE":
            self.stdout.write(self.style.WARNING(status))
            return
        raise CommandError(status)
