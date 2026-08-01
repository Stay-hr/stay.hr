"""ChannelSyncObserver — stay.hr ↔ Channex photo verify (ADR 0015 Layer 2).

Read-only: local outbox/links + Channex LIST. Never writes.
Booking.com galleries are OTA-managed and out of scope (Channex support 2026-07-31).
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from django.db.models import Max

from apps.integrations.channex.photo_provider import positions_for_unit
from apps.properties.models import Unit, UnitPhoto, UnitPhotoLink
from apps.properties.unit_photos.models import PhotoOutbox
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

CompareStatus = Literal[
    "PHOTO_SYNC_OK",
    "OUTBOX_PENDING",
    "OUTBOX_FAILED",
    "COUNT_MISMATCH",
    "COVER_MISMATCH",
    "POSITION_MISMATCH",
    "ROOM_UNMAPPED",
    "CHANNEX_UNAVAILABLE",
]


def _live_photos(*, unit: Unit):
    return UnitPhoto.objects.filter(unit=unit).exclude(
        status__in=[UnitPhoto.Status.DELETED, UnitPhoto.Status.DELETE_PENDING]
    )


class ChannelSyncObserver:
    """Verify SoT projection to Channex (counts, cover, positions, outbox)."""

    def __init__(
        self,
        *,
        channex_client=None,
        channex_config=None,
        remote_by_unit: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self._client = channex_client
        self._config = channex_config
        self._remote_by_unit = remote_by_unit

    def compare(
        self,
        *,
        tenant_slug: str,
        unit_codes: list[str] | None = None,
    ) -> dict[str, Any]:
        tenant = Tenant.objects.filter(slug=tenant_slug).first()
        if tenant is None:
            return self._unavailable(f"tenant not found: {tenant_slug}")

        units_qs = Unit.objects.filter(tenant=tenant, is_active=True).order_by("code")
        if unit_codes:
            units_qs = units_qs.filter(code__in=unit_codes)
        units = list(units_qs)

        pending = PhotoOutbox.objects.filter(
            tenant=tenant,
            status=PhotoOutbox.Status.PENDING,
        )
        failed = PhotoOutbox.objects.filter(
            tenant=tenant,
            status=PhotoOutbox.Status.FAILED,
        )
        if unit_codes:
            pending = pending.filter(unit_photo__unit__code__in=unit_codes)
            failed = failed.filter(unit_photo__unit__code__in=unit_codes)
        outbox_pending = pending.count()
        outbox_failed = failed.count()

        last_sync = None
        if units:
            last_sync = (
                UnitPhotoLink.objects.filter(
                    tenant=tenant,
                    provider="channex",
                    deleted_at__isnull=True,
                    last_sync_at__isnull=False,
                    unit_photo__unit__in=units,
                )
                .aggregate(m=Max("last_sync_at"))
                .get("m")
            )

        cfg, client, owns_client, channex_error = self._resolve_client(tenant_slug)
        rooms: list[dict[str, Any]] = []
        stay_total = 0
        channex_total = 0

        try:
            for unit in units:
                room = self._compare_unit(
                    unit=unit,
                    cfg=cfg,
                    client=client,
                )
                rooms.append(room)
                stay_total += int(room.get("stay_hr") or 0)
                if isinstance(room.get("channex"), int):
                    channex_total += room["channex"]
        finally:
            if owns_client and client is not None and hasattr(client, "close"):
                client.close()

        result: dict[str, Any] = {
            "status": "PHOTO_SYNC_OK",
            "property": {
                "stay_hr": stay_total,
                "channex": channex_total if not channex_error else None,
                "outbox_pending": outbox_pending,
                "outbox_failed": outbox_failed,
                "last_successful_sync_at": (
                    last_sync.isoformat() if last_sync is not None else None
                ),
            },
            "rooms": rooms,
        }
        if channex_error:
            result["status"] = "CHANNEX_UNAVAILABLE"
            result["error"] = channex_error
            return result

        result["status"] = self._derive_status(
            outbox_pending=outbox_pending,
            outbox_failed=outbox_failed,
            rooms=rooms,
        )
        return result

    def _resolve_client(self, tenant_slug: str):
        if self._remote_by_unit is not None:
            return self._config, self._client, False, None
        if self._client is not None and self._config is not None:
            return self._config, self._client, False, None
        try:
            from apps.integrations.channex.ari_service import (
                get_active_channex_integration,
            )
            from apps.integrations.channex.client import ChannexClient
            from apps.integrations.channex.config import ChannexRuntimeConfig

            row = get_active_channex_integration(tenant_slug)
            cfg = ChannexRuntimeConfig.from_integration_dict(row.get_config_dict())
            if self._client is not None:
                return cfg, self._client, False, None
            return cfg, ChannexClient(cfg), True, None
        except Exception as exc:  # noqa: BLE001
            logger.warning("channel sync observer channex resolve failed: %s", exc)
            return None, None, False, str(exc)[:200]

    def _compare_unit(
        self,
        *,
        unit: Unit,
        cfg,
        client,
    ) -> dict[str, Any]:
        stay_qs = _live_photos(unit=unit)
        stay_count = stay_qs.count()
        primary = stay_qs.filter(is_primary=True).first()

        room_mapped = True
        room_type_id = None
        if cfg is not None:
            room_type_id = cfg.room_type_id_for_unit_code(unit.code)
            room_mapped = bool(room_type_id)

        remote: list[dict[str, Any]] = []
        if self._remote_by_unit is not None:
            remote = list(self._remote_by_unit.get(unit.code) or [])
        elif client is not None and cfg is not None and room_type_id:
            from apps.integrations.channex.client import ChannexClient

            remote = client.list_photos(
                property_id=cfg.property_id,
                room_type_id=room_type_id,
                limit=100,
            )
        elif not room_mapped:
            remote = []

        from apps.integrations.channex.client import ChannexClient

        remote_by_id: dict[str, dict[str, Any]] = {}
        cover_remote_id: str | None = None
        for item in remote:
            attrs = ChannexClient.photo_attributes(item)
            rid = str(item.get("id") or attrs.get("id") or "")
            if not rid:
                continue
            remote_by_id[rid] = attrs
            pos = attrs.get("position")
            if pos is not None and int(pos) == 0:
                cover_remote_id = rid

        expected_positions = positions_for_unit(unit.pk)
        links = {
            link.unit_photo_id: link
            for link in UnitPhotoLink.objects.filter(
                unit_photo__unit=unit,
                provider="channex",
                deleted_at__isnull=True,
            ).exclude(external_id="")
        }

        cover_ok = True
        if stay_count > 0:
            if primary is None:
                cover_ok = False
            else:
                link = links.get(primary.pk)
                if link is None or not link.external_id:
                    cover_ok = False
                elif cover_remote_id is None:
                    cover_ok = False
                else:
                    cover_ok = str(link.external_id) == str(cover_remote_id)

        positions_ok = True
        if room_mapped and stay_count > 0:
            for photo in stay_qs:
                link = links.get(photo.pk)
                if link is None or not link.external_id:
                    positions_ok = False
                    break
                attrs = remote_by_id.get(str(link.external_id))
                if attrs is None:
                    positions_ok = False
                    break
                expected = expected_positions.get(photo.pk)
                remote_pos = attrs.get("position")
                if expected is None or remote_pos is None:
                    positions_ok = False
                    break
                if int(remote_pos) != int(expected):
                    positions_ok = False
                    break

        return {
            "code": unit.code,
            "stay_hr": stay_count,
            "channex": len(remote) if room_mapped else None,
            "cover": cover_ok,
            "positions_ok": positions_ok if room_mapped else False,
            "room_mapped": room_mapped,
        }

    @staticmethod
    def _derive_status(
        *,
        outbox_pending: int,
        outbox_failed: int,
        rooms: list[dict[str, Any]],
    ) -> CompareStatus:
        if any(not r.get("room_mapped") for r in rooms):
            return "ROOM_UNMAPPED"
        if outbox_failed > 0:
            return "OUTBOX_FAILED"
        if outbox_pending > 0:
            return "OUTBOX_PENDING"
        for room in rooms:
            s = int(room.get("stay_hr") or 0)
            c = room.get("channex")
            if isinstance(c, int) and c != s:
                return "COUNT_MISMATCH"
        for room in rooms:
            if int(room.get("stay_hr") or 0) > 0 and room.get("cover") is False:
                return "COVER_MISMATCH"
        for room in rooms:
            if int(room.get("stay_hr") or 0) > 0 and room.get("positions_ok") is False:
                return "POSITION_MISMATCH"
        return "PHOTO_SYNC_OK"

    @staticmethod
    def _unavailable(error: str) -> dict[str, Any]:
        return {
            "status": "CHANNEX_UNAVAILABLE",
            "property": {
                "stay_hr": 0,
                "channex": None,
                "outbox_pending": 0,
                "outbox_failed": 0,
                "last_successful_sync_at": None,
            },
            "rooms": [],
            "error": error,
        }
