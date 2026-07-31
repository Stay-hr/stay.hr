"""ADR 0015 Phase B — ChannexPhotoProvider + photo outbox flush."""

from __future__ import annotations

import io
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from PIL import Image

from apps.integrations.channex.config import ChannexRuntimeConfig
from apps.integrations.channex.exceptions import (
    ChannexApiError,
    PhotoSyncPermanentError,
    PhotoSyncRetryableError,
)
from apps.integrations.channex.photo_outbox import flush_photo_outbox
from apps.integrations.channex.photo_provider import (
    ChannexPhotoProvider,
    classify_photo_sync_error,
)
from apps.integrations.channex.tasks import flush_photo_outbox_task
from apps.integrations.models import IntegrationConfig
from apps.properties.models import PhotoOutbox, Property, Unit, UnitPhoto, UnitPhotoLink
from apps.properties.unit_photos.service import UnitPhotoService
from apps.properties.unit_photos.storage import LocalStorage
from apps.tenants.models import Tenant


def _jpeg_bytes(width: int = 200, height: int = 150, color=(40, 80, 120)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="JPEG", quality=85)
    return buf.getvalue()


class ClassifyErrorTests(TestCase):
    def test_429_retryable(self):
        self.assertIs(
            classify_photo_sync_error(ChannexApiError("x", status_code=429)),
            PhotoSyncRetryableError,
        )

    def test_500_retryable(self):
        self.assertIs(
            classify_photo_sync_error(ChannexApiError("x", status_code=503)),
            PhotoSyncRetryableError,
        )

    def test_400_permanent(self):
        self.assertIs(
            classify_photo_sync_error(ChannexApiError("x", status_code=400)),
            PhotoSyncPermanentError,
        )

    def test_401_permanent(self):
        self.assertIs(
            classify_photo_sync_error(ChannexApiError("x", status_code=401)),
            PhotoSyncPermanentError,
        )

    def test_404_permanent(self):
        self.assertIs(
            classify_photo_sync_error(ChannexApiError("x", status_code=404)),
            PhotoSyncPermanentError,
        )

    def test_network_retryable(self):
        self.assertIs(
            classify_photo_sync_error(ChannexApiError("timeout")),
            PhotoSyncRetryableError,
        )


class ChannexPhotoProviderTests(TestCase):
    def setUp(self):
        self._media = tempfile.TemporaryDirectory()
        self.addCleanup(self._media.cleanup)
        self.storage = LocalStorage(root=Path(self._media.name))
        self.service = UnitPhotoService(storage=self.storage)
        self.tenant = Tenant.objects.create(slug="photo-b", name="Photo B")
        self.property = Property.objects.create(
            tenant=self.tenant, slug="photo-b-p", name="Photo B P"
        )
        self.unit = Unit.objects.create(
            tenant=self.tenant, property=self.property, code="R4", name="R4"
        )
        from apps.integrations.channex.config import ChannexRoomTypeLink

        self.config = ChannexRuntimeConfig(
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
        self.client = MagicMock()
        self.provider = ChannexPhotoProvider(
            client=self.client,
            config=self.config,
            storage=self.storage,
            verify_after_upload=True,
        )

    def test_upload_idempotent_skip_when_checksum_matches(self):
        photo = self.service.add_photo(
            self.unit, _jpeg_bytes(), original_filename="a.jpg"
        )
        UnitPhotoLink.objects.create(
            tenant=self.tenant,
            unit_photo=photo,
            provider="channex",
            external_id="ext-1",
            content_checksum_pushed=photo.content_checksum,
        )
        entry = PhotoOutbox.objects.get(unit_photo=photo, kind=PhotoOutbox.Kind.UPLOAD)
        self.provider.apply(entry)
        self.client.upload_photo_file.assert_not_called()
        self.client.create_photo.assert_not_called()
        self.client.list_photos.assert_not_called()
        entry.refresh_from_db()
        photo.refresh_from_db()
        self.assertEqual(entry.status, PhotoOutbox.Status.SENT)
        self.assertEqual(photo.status, UnitPhoto.Status.ACTIVE)

    def test_upload_happy_path_no_list(self):
        photo = self.service.add_photo(
            self.unit, _jpeg_bytes(), original_filename="a.jpg"
        )
        entry = PhotoOutbox.objects.get(unit_photo=photo, kind=PhotoOutbox.Kind.UPLOAD)
        self.client.upload_photo_file.return_value = "https://tmp/photo.jpg"
        self.client.create_photo.return_value = {
            "data": {"id": "new-ext", "type": "photo"}
        }
        self.client.extract_photo_id.side_effect = (
            lambda r: ChannexPhotoProviderTests._extract(r)
        )
        # Use real extract_photo_id
        from apps.integrations.channex.client import ChannexClient

        self.client.extract_photo_id.side_effect = ChannexClient.extract_photo_id
        self.client.photo_attributes.side_effect = ChannexClient.photo_attributes
        self.client.get_photo.return_value = {
            "id": "new-ext",
            "attributes": {
                "id": "new-ext",
                "position": 0,
                "room_type_id": "rt-r4",
                "property_id": "prop-1",
            },
        }

        self.provider.apply(entry)

        self.client.upload_photo_file.assert_called_once()
        self.client.create_photo.assert_called_once()
        self.client.get_photo.assert_called_once_with("new-ext")
        self.client.list_photos.assert_not_called()
        link = UnitPhotoLink.objects.get(unit_photo=photo, provider="channex")
        self.assertEqual(link.external_id, "new-ext")
        self.assertIsNone(link.deleted_at)
        photo.refresh_from_db()
        self.assertEqual(photo.status, UnitPhoto.Status.ACTIVE)

    @staticmethod
    def _extract(response):
        from apps.integrations.channex.client import ChannexClient

        return ChannexClient.extract_photo_id(response)

    def test_upload_replace_when_checksum_differs(self):
        photo = self.service.add_photo(
            self.unit, _jpeg_bytes(color=(1, 1, 1)), original_filename="a.jpg"
        )
        UnitPhotoLink.objects.create(
            tenant=self.tenant,
            unit_photo=photo,
            provider="channex",
            external_id="old-ext",
            content_checksum_pushed="deadbeef" * 8,
        )
        # Force new outbox as if replace left pending upload
        entry = PhotoOutbox.objects.get(unit_photo=photo, kind=PhotoOutbox.Kind.UPLOAD)
        from apps.integrations.channex.client import ChannexClient

        self.client.upload_photo_file.return_value = "https://tmp/photo.jpg"
        self.client.create_photo.return_value = {
            "data": {"id": "new-ext", "type": "photo"}
        }
        self.client.extract_photo_id.side_effect = ChannexClient.extract_photo_id
        self.client.photo_attributes.side_effect = ChannexClient.photo_attributes
        self.client.get_photo.return_value = {
            "id": "new-ext",
            "attributes": {
                "id": "new-ext",
                "position": 0,
                "room_type_id": "rt-r4",
            },
        }
        self.provider.apply(entry)
        self.client.delete_photo.assert_called_once_with("old-ext")
        link = UnitPhotoLink.objects.get(unit_photo=photo)
        self.assertEqual(link.external_id, "new-ext")
        self.assertEqual(link.content_checksum_pushed, photo.content_checksum)

    def test_delete_tombstones_link(self):
        photo = self.service.add_photo(
            self.unit, _jpeg_bytes(), original_filename="a.jpg"
        )
        PhotoOutbox.objects.filter(unit_photo=photo).update(status=PhotoOutbox.Status.SENT)
        link = UnitPhotoLink.objects.create(
            tenant=self.tenant,
            unit_photo=photo,
            provider="channex",
            external_id="ext-del",
            content_checksum_pushed=photo.content_checksum,
        )
        photo.status = UnitPhoto.Status.DELETE_PENDING
        photo.save(update_fields=["status"])
        entry = PhotoOutbox.objects.create(
            tenant=self.tenant,
            unit_photo=photo,
            kind=PhotoOutbox.Kind.DELETE,
            status=PhotoOutbox.Status.PENDING,
        )
        self.provider.apply(entry)
        self.client.delete_photo.assert_called_once_with("ext-del")
        link.refresh_from_db()
        self.assertIsNotNone(link.deleted_at)
        self.assertEqual(link.external_id, "ext-del")
        self.assertEqual(link.deleted_checksum, photo.content_checksum)
        photo.refresh_from_db()
        self.assertEqual(photo.status, UnitPhoto.Status.DELETED)

    def test_missing_room_mapping_permanent(self):
        from apps.integrations.channex.config import ChannexRoomTypeLink

        self.provider.config = ChannexRuntimeConfig(
            environment="staging",
            base_url="https://staging.channex.io/api/v1",
            property_id="prop-1",
            api_key="key",
            room_types=(
                ChannexRoomTypeLink(
                    unit_code="R1",
                    channex_room_type_id="rt-r1",
                    channex_title="R1",
                ),
            ),
        )
        photo = self.service.add_photo(
            self.unit, _jpeg_bytes(), original_filename="a.jpg"
        )
        entry = PhotoOutbox.objects.get(unit_photo=photo, kind=PhotoOutbox.Kind.UPLOAD)
        with self.assertRaises(PhotoSyncPermanentError):
            self.provider.apply(entry)

    def test_verify_failure_retryable(self):
        photo = self.service.add_photo(
            self.unit, _jpeg_bytes(), original_filename="a.jpg"
        )
        entry = PhotoOutbox.objects.get(unit_photo=photo, kind=PhotoOutbox.Kind.UPLOAD)
        from apps.integrations.channex.client import ChannexClient

        self.client.upload_photo_file.return_value = "https://tmp/photo.jpg"
        self.client.create_photo.return_value = {
            "data": {"id": "new-ext", "type": "photo"}
        }
        self.client.extract_photo_id.side_effect = ChannexClient.extract_photo_id
        self.client.photo_attributes.side_effect = ChannexClient.photo_attributes
        self.client.get_photo.return_value = {
            "id": "new-ext",
            "attributes": {
                "id": "new-ext",
                "position": 99,
                "room_type_id": "rt-r4",
            },
        }
        with self.assertRaises(PhotoSyncRetryableError):
            self.provider.apply(entry)


class FlushPhotoOutboxTests(TestCase):
    def setUp(self):
        self._media = tempfile.TemporaryDirectory()
        self.addCleanup(self._media.cleanup)
        self.storage = LocalStorage(root=Path(self._media.name))
        self.service = UnitPhotoService(storage=self.storage)
        self.tenant = Tenant.objects.create(slug="uzorita-photo", name="Uzorita Photo")
        self.property = Property.objects.create(
            tenant=self.tenant, slug="uz-p", name="Uzorita P"
        )
        self.unit = Unit.objects.create(
            tenant=self.tenant, property=self.property, code="R4", name="R4"
        )
        self.integration = IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.CHANNEX,
            is_active=True,
            config={
                "environment": "staging",
                "base_url": "https://staging.channex.io/api/v1",
                "property_id": "prop-1",
                "api_key": "test-key",
                "room_types": [
                    {
                        "unit_code": "R4",
                        "channex_room_type_id": "rt-r4",
                        "channex_title": "R4",
                    }
                ],
            },
        )

    @override_settings(CHANNEX_OUTBOUND_ENABLED=False)
    def test_flush_skips_when_write_disabled(self):
        self.service.add_photo(self.unit, _jpeg_bytes(), original_filename="a.jpg")
        result = flush_photo_outbox(tenant_slug="uzorita-photo", storage=self.storage)
        self.assertTrue(result.get("skipped"))
        self.assertEqual(
            PhotoOutbox.objects.filter(status=PhotoOutbox.Status.PENDING).count(),
            2,  # UPLOAD + SET_PRIMARY for first photo
        )

    @override_settings(CHANNEX_OUTBOUND_ENABLED=False)
    def test_celery_task_skips_when_write_disabled(self):
        result = flush_photo_outbox_task(tenant_slug="uzorita-photo")
        self.assertTrue(result.get("skipped"))

    @override_settings(CHANNEX_OUTBOUND_ENABLED=True, UNIT_PHOTO_VERIFY_AFTER_UPLOAD=True)
    def test_flush_success_with_stub_client(self):
        photo = self.service.add_photo(
            self.unit, _jpeg_bytes(), original_filename="a.jpg"
        )
        client = MagicMock()
        from apps.integrations.channex.client import ChannexClient

        client.upload_photo_file.return_value = "https://tmp/x.jpg"
        client.create_photo.return_value = {"data": {"id": "ext-ok", "type": "photo"}}
        client.extract_photo_id.side_effect = ChannexClient.extract_photo_id
        client.photo_attributes.side_effect = ChannexClient.photo_attributes
        client.get_photo.return_value = {
            "id": "ext-ok",
            "attributes": {
                "id": "ext-ok",
                "position": 0,
                "room_type_id": "rt-r4",
            },
        }
        summary = flush_photo_outbox(
            tenant_slug="uzorita-photo",
            client=client,
            storage=self.storage,
        )
        self.assertEqual(summary["sent"], 2)  # UPLOAD + SET_PRIMARY
        photo.refresh_from_db()
        self.assertEqual(photo.status, UnitPhoto.Status.ACTIVE)
        self.assertTrue(
            UnitPhotoLink.objects.filter(
                unit_photo=photo, external_id="ext-ok", deleted_at__isnull=True
            ).exists()
        )
        client.list_photos.assert_not_called()

    @override_settings(CHANNEX_OUTBOUND_ENABLED=True)
    def test_permanent_fail_marks_failed_no_raise(self):
        photo = self.service.add_photo(
            self.unit, _jpeg_bytes(), original_filename="a.jpg"
        )
        client = MagicMock()
        client.upload_photo_file.side_effect = ChannexApiError(
            "bad", status_code=400
        )
        summary = flush_photo_outbox(
            tenant_slug="uzorita-photo",
            client=client,
            storage=self.storage,
        )
        self.assertEqual(summary["failed"], 1)
        entry = PhotoOutbox.objects.get(
            unit_photo=photo, kind=PhotoOutbox.Kind.UPLOAD
        )
        self.assertEqual(entry.status, PhotoOutbox.Status.FAILED)
        photo.refresh_from_db()
        self.assertEqual(photo.status, UnitPhoto.Status.FAILED)

    @override_settings(CHANNEX_OUTBOUND_ENABLED=True)
    def test_retryable_fail_raises(self):
        self.service.add_photo(self.unit, _jpeg_bytes(), original_filename="a.jpg")
        client = MagicMock()
        client.upload_photo_file.side_effect = ChannexApiError(
            "busy", status_code=429
        )
        with self.assertRaises(PhotoSyncRetryableError):
            flush_photo_outbox(
                tenant_slug="uzorita-photo",
                client=client,
                storage=self.storage,
            )

    @override_settings(CHANNEX_OUTBOUND_ENABLED=True, UNIT_PHOTO_VERIFY_AFTER_UPLOAD=False)
    def test_reorder_coalesced_under_unit_lock(self):
        p1 = self.service.add_photo(
            self.unit, _jpeg_bytes(color=(1, 0, 0)), original_filename="1.jpg"
        )
        p2 = self.service.add_photo(
            self.unit, _jpeg_bytes(color=(0, 1, 0)), original_filename="2.jpg"
        )
        # Mark uploads sent + links so only reorder is pending
        PhotoOutbox.objects.filter(kind=PhotoOutbox.Kind.UPLOAD).update(
            status=PhotoOutbox.Status.SENT
        )
        for photo, ext in ((p1, "e1"), (p2, "e2")):
            UnitPhotoLink.objects.create(
                tenant=self.tenant,
                unit_photo=photo,
                provider="channex",
                external_id=ext,
                content_checksum_pushed=photo.content_checksum,
            )
            photo.status = UnitPhoto.Status.ACTIVE
            photo.save(update_fields=["status"])
        self.service.reorder(self.unit, [p2.pk, p1.pk])
        client = MagicMock()
        summary = flush_photo_outbox(
            tenant_slug="uzorita-photo",
            client=client,
            storage=self.storage,
        )
        self.assertGreaterEqual(summary["sent"], 1)
        self.assertTrue(client.update_photo.called)
        # Both photos should get position updates in one batch
        updated_ids = {c.args[0] for c in client.update_photo.call_args_list}
        self.assertEqual(updated_ids, {"e1", "e2"})


class ChannexClientPhotoMethodsTests(TestCase):
    @patch("apps.integrations.channex.client.httpx.Client")
    def test_upload_uses_multipart(self, client_cls):
        from apps.integrations.channex.client import ChannexClient
        from apps.integrations.channex.config import ChannexRuntimeConfig

        session = MagicMock()
        client_cls.return_value = session
        response = MagicMock()
        response.status_code = 200
        response.content = b'{"url":"https://tmp/a.jpg"}'
        response.json.return_value = {"url": "https://tmp/a.jpg"}
        session.request.return_value = response

        cfg = ChannexRuntimeConfig(
            environment="staging",
            base_url="https://staging.channex.io/api/v1",
            property_id="p",
            api_key="k",
        )
        with override_settings(CHANNEX_OUTBOUND_ENABLED=True):
            c = ChannexClient(cfg)
            url = c.upload_photo_file(b"jpeg-bytes", "x.jpg")
        self.assertEqual(url, "https://tmp/a.jpg")
        kwargs = session.request.call_args.kwargs
        self.assertIn("files", kwargs)
        self.assertEqual(session.request.call_args.args[0], "POST")
