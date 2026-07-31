"""Phase A tests for unit listing photos (ADR 0015)."""

from __future__ import annotations

import io
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings
from PIL import Image

from apps.properties.models import PhotoOutbox, Property, Unit, UnitPhoto, UnitPhotoLink
from apps.properties.unit_photos.exceptions import (
    UnitPhotoStateError,
    UnitPhotoValidationError,
)
from apps.properties.unit_photos.providers import MockPhotoProvider, process_pending_outbox
from apps.properties.unit_photos.service import UnitPhotoService
from apps.properties.unit_photos.storage import LocalStorage, sha256_hex
from apps.properties.unit_photos.validation import validate_image_bytes
from apps.tenants.models import Tenant


def _jpeg_bytes(width: int = 200, height: int = 150, color=(40, 80, 120)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="JPEG", quality=85)
    return buf.getvalue()


class UnitPhotoValidationTests(TestCase):
    def test_accept_jpeg(self):
        data = _jpeg_bytes()
        validated = validate_image_bytes(data)
        self.assertEqual(validated.format, "JPEG")
        self.assertEqual(validated.checksum, sha256_hex(validated.data))

    def test_reject_empty(self):
        with self.assertRaises(UnitPhotoValidationError):
            validate_image_bytes(b"")

    def test_reject_non_image(self):
        with self.assertRaises(UnitPhotoValidationError):
            validate_image_bytes(b"not-an-image")

    @override_settings(UNIT_PHOTO_MIN_EDGE=500)
    def test_reject_too_small(self):
        with self.assertRaises(UnitPhotoValidationError):
            validate_image_bytes(_jpeg_bytes(100, 100))


class UnitPhotoServiceTests(TestCase):
    def setUp(self):
        self._media = tempfile.TemporaryDirectory()
        self.addCleanup(self._media.cleanup)
        self.storage = LocalStorage(root=Path(self._media.name))
        self.service = UnitPhotoService(storage=self.storage)
        self.tenant = Tenant.objects.create(slug="photo-t", name="Photo T")
        self.property = Property.objects.create(
            tenant=self.tenant, slug="photo-p", name="Photo P"
        )
        self.unit_a = Unit.objects.create(
            tenant=self.tenant, property=self.property, code="R4", name="R4"
        )
        self.unit_b = Unit.objects.create(
            tenant=self.tenant, property=self.property, code="R3", name="R3"
        )

    def test_add_photo_enqueues_upload(self):
        photo = self.service.add_photo(self.unit_a, _jpeg_bytes(), original_filename="a.jpg")
        self.assertEqual(photo.status, UnitPhoto.Status.UPLOAD_PENDING)
        self.assertTrue(photo.is_primary)
        self.assertTrue(
            PhotoOutbox.objects.filter(
                unit_photo=photo, kind=PhotoOutbox.Kind.UPLOAD, status="pending"
            ).exists()
        )
        self.assertTrue(self.storage.exists(photo.storage_ref))

    def test_ownership_same_bytes_two_units(self):
        data = _jpeg_bytes(color=(1, 2, 3))
        p1 = self.service.add_photo(self.unit_a, data, original_filename="x.jpg")
        p2 = self.service.add_photo(self.unit_b, data, original_filename="x.jpg")
        self.assertNotEqual(p1.pk, p2.pk)
        self.assertEqual(p1.content_checksum, p2.content_checksum)
        self.assertEqual(p1.storage_ref, p2.storage_ref)
        self.service.soft_delete(p1)
        p2.refresh_from_db()
        self.assertNotEqual(p2.status, UnitPhoto.Status.DELETED)
        self.assertEqual(p2.status, UnitPhoto.Status.UPLOAD_PENDING)

    def test_duplicate_checksum_same_unit_skips(self):
        data = _jpeg_bytes()
        first = self.service.add_photo(self.unit_a, data, original_filename="1.jpg")
        second = self.service.add_photo(self.unit_a, data, original_filename="2.jpg")
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(
            UnitPhoto.objects.filter(unit=self.unit_a)
            .exclude(status=UnitPhoto.Status.DELETED)
            .count(),
            1,
        )

    def test_primary_invariant(self):
        p1 = self.service.add_photo(self.unit_a, _jpeg_bytes(color=(10, 0, 0)))
        p2 = self.service.add_photo(
            self.unit_a, _jpeg_bytes(color=(0, 10, 0)), make_primary=True
        )
        p1.refresh_from_db()
        self.assertFalse(p1.is_primary)
        self.assertTrue(p2.is_primary)

    def test_replace_is_delete_plus_upload(self):
        old = self.service.add_photo(self.unit_a, _jpeg_bytes(color=(9, 9, 9)))
        new = self.service.replace_photo(
            old, _jpeg_bytes(color=(8, 8, 8)), original_filename="new.jpg"
        )
        old.refresh_from_db()
        self.assertEqual(old.status, UnitPhoto.Status.DELETE_PENDING)
        self.assertFalse(old.is_primary)
        self.assertEqual(new.status, UnitPhoto.Status.UPLOAD_PENDING)
        self.assertTrue(new.is_primary)
        self.assertNotEqual(old.pk, new.pk)

    def test_reorder_enqueues(self):
        p1 = self.service.add_photo(self.unit_a, _jpeg_bytes(color=(1, 0, 0)))
        p2 = self.service.add_photo(self.unit_a, _jpeg_bytes(color=(0, 1, 0)))
        self.service.reorder(self.unit_a, [p2.pk, p1.pk])
        p1.refresh_from_db()
        p2.refresh_from_db()
        self.assertEqual(p2.sort_order, 0)
        self.assertEqual(p1.sort_order, 1)
        self.assertTrue(
            PhotoOutbox.objects.filter(
                unit_photo=p2, kind=PhotoOutbox.Kind.REORDER
            ).exists()
        )

    def test_set_primary_on_deleted_raises(self):
        photo = self.service.add_photo(self.unit_a, _jpeg_bytes())
        photo.status = UnitPhoto.Status.DELETED
        photo.is_primary = False
        photo.save(update_fields=["status", "is_primary"])
        with self.assertRaises(UnitPhotoStateError):
            self.service.set_primary(photo)

    def test_mock_provider_drains_outbox(self):
        photo = self.service.add_photo(self.unit_a, _jpeg_bytes(color=(3, 3, 3)))
        n = process_pending_outbox(provider=MockPhotoProvider())
        self.assertGreaterEqual(n, 1)
        photo.refresh_from_db()
        self.assertEqual(photo.status, UnitPhoto.Status.ACTIVE)
        link = UnitPhotoLink.objects.get(unit_photo=photo, provider="mock")
        self.assertTrue(link.external_id.startswith("mock-"))
        self.assertFalse(
            PhotoOutbox.objects.filter(
                unit_photo=photo, status=PhotoOutbox.Status.PENDING
            ).exists()
        )


class ImportUnitPhotosCommandTests(TestCase):
    def setUp(self):
        self._media = tempfile.TemporaryDirectory()
        self.addCleanup(self._media.cleanup)
        self.tenant = Tenant.objects.create(slug="imp-t", name="Imp")
        self.property = Property.objects.create(
            tenant=self.tenant, slug="imp-p", name="Imp P"
        )
        self.unit = Unit.objects.create(
            tenant=self.tenant, property=self.property, code="R4", name="R4"
        )
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        (self.dir / "r4-01.jpeg").write_bytes(_jpeg_bytes(color=(11, 0, 0)))
        (self.dir / "r4-02.jpeg").write_bytes(_jpeg_bytes(color=(0, 11, 0)))
        (self.dir / "r4-19.jpeg").write_bytes(_jpeg_bytes(color=(0, 0, 11)))

    @patch("apps.properties.unit_photos.service.default_media_storage")
    def test_importer_smoke(self, mock_storage_factory):
        mock_storage_factory.return_value = LocalStorage(root=Path(self._media.name))
        call_command(
            "import_unit_photos",
            "--tenant-slug",
            "imp-t",
            "--unit-code",
            "R4",
            "--dir",
            str(self.dir),
            "--primary",
            "r4-19.jpeg",
        )
        photos = list(
            UnitPhoto.objects.filter(unit=self.unit).exclude(status="deleted").order_by("id")
        )
        self.assertEqual(len(photos), 3)
        primary = [p for p in photos if p.is_primary]
        self.assertEqual(len(primary), 1)
        self.assertEqual(primary[0].original_filename, "r4-19.jpeg")
        self.assertTrue(
            PhotoOutbox.objects.filter(
                unit_photo__unit=self.unit, status=PhotoOutbox.Status.PENDING
            ).exists()
        )
