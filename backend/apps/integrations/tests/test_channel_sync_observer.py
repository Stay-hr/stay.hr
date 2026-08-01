"""ADR 0015 Layer 2 — ChannelSyncObserver (stay.hr ↔ Channex; mocked remote)."""

from __future__ import annotations

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.integrations.channex.config import ChannexRoomTypeLink, ChannexRuntimeConfig
from apps.integrations.channex.observers.photos import ChannelSyncObserver
from apps.properties.models import PhotoOutbox, Property, Unit, UnitPhoto, UnitPhotoLink
from apps.tenants.models import Tenant


def _remote(photo_id: str, position: int) -> dict:
    return {
        "id": photo_id,
        "type": "photo",
        "attributes": {"position": position, "id": photo_id},
    }


class ChannelSyncObserverTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="CS", slug="channel-sync")
        self.prop = Property.objects.create(
            tenant=self.tenant, name="CS", slug="channel-sync-p"
        )
        self.unit = Unit.objects.create(
            tenant=self.tenant, property=self.prop, code="R4", name="R4"
        )
        self.cfg = ChannexRuntimeConfig(
            environment="staging",
            base_url="https://staging.channex.io/api/v1",
            property_id="prop-1",
            api_key="key",
            room_types=(
                ChannexRoomTypeLink(
                    unit_code="R4",
                    channex_room_type_id="rt-r4",
                    channex_title="R4",
                ),
            ),
        )
        now = timezone.now()
        self.photos = []
        for i in range(3):
            photo = UnitPhoto.objects.create(
                tenant=self.tenant,
                unit=self.unit,
                storage_ref=f"r4/{i}.jpg",
                content_checksum=f"hash{i}",
                original_filename=f"r4-{i:02d}.jpeg",
                is_primary=(i == 0),
                sort_order=i,
                status=UnitPhoto.Status.ACTIVE,
            )
            UnitPhotoLink.objects.create(
                tenant=self.tenant,
                unit_photo=photo,
                provider="channex",
                external_id=f"ext-{i}",
                content_checksum_pushed=photo.content_checksum,
                last_sync_at=now,
            )
            self.photos.append(photo)

    def _observer(self, remote: list[dict] | None = None, **kwargs):
        if remote is None:
            remote = [_remote(f"ext-{i}", i) for i in range(3)]
        return ChannelSyncObserver(
            channex_config=self.cfg,
            remote_by_unit={"R4": remote},
            **kwargs,
        )

    def test_photo_sync_ok(self):
        result = self._observer().compare(
            tenant_slug=self.tenant.slug, unit_codes=["R4"]
        )
        self.assertEqual(result["status"], "PHOTO_SYNC_OK")
        self.assertEqual(result["property"]["stay_hr"], 3)
        self.assertEqual(result["property"]["channex"], 3)
        self.assertEqual(result["property"]["outbox_pending"], 0)
        self.assertTrue(result["rooms"][0]["cover"])
        self.assertTrue(result["rooms"][0]["positions_ok"])

    def test_count_mismatch(self):
        remote = [_remote("ext-0", 0), _remote("ext-1", 1)]
        result = self._observer(remote).compare(
            tenant_slug=self.tenant.slug, unit_codes=["R4"]
        )
        self.assertEqual(result["status"], "COUNT_MISMATCH")

    def test_cover_mismatch(self):
        # Remote cover is ext-1, stay primary is ext-0
        remote = [_remote("ext-1", 0), _remote("ext-0", 1), _remote("ext-2", 2)]
        result = self._observer(remote).compare(
            tenant_slug=self.tenant.slug, unit_codes=["R4"]
        )
        self.assertEqual(result["status"], "COVER_MISMATCH")
        self.assertFalse(result["rooms"][0]["cover"])

    def test_position_mismatch(self):
        remote = [_remote("ext-0", 0), _remote("ext-2", 1), _remote("ext-1", 2)]
        result = self._observer(remote).compare(
            tenant_slug=self.tenant.slug, unit_codes=["R4"]
        )
        self.assertEqual(result["status"], "POSITION_MISMATCH")

    def test_outbox_pending(self):
        PhotoOutbox.objects.create(
            tenant=self.tenant,
            unit_photo=self.photos[0],
            kind=PhotoOutbox.Kind.UPLOAD,
            status=PhotoOutbox.Status.PENDING,
        )
        result = self._observer().compare(
            tenant_slug=self.tenant.slug, unit_codes=["R4"]
        )
        self.assertEqual(result["status"], "OUTBOX_PENDING")
        self.assertEqual(result["property"]["outbox_pending"], 1)

    def test_outbox_failed(self):
        PhotoOutbox.objects.create(
            tenant=self.tenant,
            unit_photo=self.photos[0],
            kind=PhotoOutbox.Kind.UPLOAD,
            status=PhotoOutbox.Status.FAILED,
            error_message="boom",
        )
        result = self._observer().compare(
            tenant_slug=self.tenant.slug, unit_codes=["R4"]
        )
        self.assertEqual(result["status"], "OUTBOX_FAILED")

    def test_room_unmapped(self):
        cfg = ChannexRuntimeConfig(
            environment="staging",
            base_url="https://staging.channex.io/api/v1",
            property_id="prop-1",
            api_key="key",
            room_types=(),
        )
        observer = ChannelSyncObserver(
            channex_config=cfg,
            remote_by_unit={"R4": []},
        )
        result = observer.compare(tenant_slug=self.tenant.slug, unit_codes=["R4"])
        self.assertEqual(result["status"], "ROOM_UNMAPPED")
        self.assertFalse(result["rooms"][0]["room_mapped"])

    def test_unknown_tenant(self):
        result = self._observer().compare(tenant_slug="missing")
        self.assertEqual(result["status"], "CHANNEX_UNAVAILABLE")


class PhotosStatusBlockTests(TestCase):
    def test_photos_block_disabled_by_default(self):
        from apps.core.system_status import build_system_status_payload

        payload = build_system_status_payload(tenant_slug="uzorita")
        self.assertIn("photos", payload)
        self.assertEqual(payload["photos"]["status"], "CHANNEX_UNAVAILABLE")
        self.assertIn("disabled", payload["photos"].get("error", ""))

    @override_settings(CHANNEL_PHOTO_STATUS_ENABLED=True)
    def test_photos_block_live_when_enabled(self):
        from apps.core.system_status import build_system_status_payload

        payload = build_system_status_payload(tenant_slug="missing-tenant-xyz")
        self.assertEqual(payload["photos"]["status"], "CHANNEX_UNAVAILABLE")
        self.assertIn("tenant not found", payload["photos"].get("error", ""))
