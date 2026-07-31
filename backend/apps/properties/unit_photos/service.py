"""Domain service for UnitPhoto lifecycle (ADR 0015 Phase A)."""

from __future__ import annotations

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.properties.models import Unit
from apps.properties.unit_photos.audit import audit_unit_photo
from apps.properties.unit_photos.exceptions import UnitPhotoStateError
from apps.properties.unit_photos.models import PhotoOutbox, UnitPhoto
from apps.properties.unit_photos.storage import (
    MediaStorage,
    default_media_storage,
    storage_key_for_checksum,
)
from apps.properties.unit_photos.validation import validate_image_bytes

_ACTIVE_STATUSES = frozenset(
    {
        UnitPhoto.Status.UPLOAD_PENDING,
        UnitPhoto.Status.SYNCING,
        UnitPhoto.Status.ACTIVE,
        UnitPhoto.Status.FAILED,
        UnitPhoto.Status.OUT_OF_SYNC,
        UnitPhoto.Status.DRAFT,
    }
)


class UnitPhotoService:
    def __init__(self, storage: MediaStorage | None = None) -> None:
        self.storage = storage or default_media_storage()

    def _enqueue(
        self,
        photo: UnitPhoto,
        kind: str,
        *,
        payload: dict | None = None,
    ) -> PhotoOutbox:
        return PhotoOutbox.objects.create(
            tenant=photo.tenant,
            unit_photo=photo,
            kind=kind,
            status=PhotoOutbox.Status.PENDING,
            payload=payload or {},
        )

    def _next_sort_order(self, unit: Unit) -> int:
        agg = (
            UnitPhoto.objects.filter(unit=unit)
            .exclude(status=UnitPhoto.Status.DELETED)
            .aggregate(m=Max("sort_order"))
        )
        current = agg["m"]
        return 0 if current is None else int(current) + 1

    def _clear_primary(self, unit: Unit, *, except_id: int | None = None) -> None:
        qs = UnitPhoto.objects.filter(unit=unit, is_primary=True).exclude(
            status=UnitPhoto.Status.DELETED
        )
        if except_id is not None:
            qs = qs.exclude(pk=except_id)
        qs.update(is_primary=False, updated_at=timezone.now())

    @transaction.atomic
    def add_photo(
        self,
        unit: Unit,
        data: bytes,
        *,
        original_filename: str = "",
        make_primary: bool = False,
        actor: str | None = None,
    ) -> UnitPhoto:
        validated = validate_image_bytes(data)

        existing = (
            UnitPhoto.objects.filter(
                unit=unit,
                content_checksum=validated.checksum,
            )
            .exclude(status=UnitPhoto.Status.DELETED)
            .exclude(status=UnitPhoto.Status.DELETE_PENDING)
            .first()
        )
        if existing is not None:
            # Same bytes already canonical — no duplicate push intent
            if make_primary and not existing.is_primary:
                return self.set_primary(existing, actor=actor)
            audit_unit_photo(
                "add_skipped_duplicate_checksum",
                unit_id=unit.pk,
                photo_id=existing.pk,
                checksum=validated.checksum,
                actor=actor,
            )
            return existing

        ref = storage_key_for_checksum(validated.checksum, validated.suffix)
        self.storage.put(ref, validated.data)

        living = (
            UnitPhoto.objects.filter(unit=unit)
            .exclude(status=UnitPhoto.Status.DELETED)
            .exclude(status=UnitPhoto.Status.DELETE_PENDING)
        )
        is_first = not living.exists()
        primary = make_primary or is_first
        if primary:
            self._clear_primary(unit)

        photo = UnitPhoto.objects.create(
            tenant=unit.tenant,
            unit=unit,
            storage_ref=ref,
            content_checksum=validated.checksum,
            original_filename=original_filename[:255],
            is_primary=primary,
            sort_order=self._next_sort_order(unit),
            status=UnitPhoto.Status.UPLOAD_PENDING,
        )
        self._enqueue(photo, PhotoOutbox.Kind.UPLOAD)
        if primary:
            self._enqueue(photo, PhotoOutbox.Kind.SET_PRIMARY)
        audit_unit_photo(
            "upload",
            unit_id=unit.pk,
            photo_id=photo.pk,
            checksum=validated.checksum,
            actor=actor,
            extra={"original_filename": original_filename, "is_primary": primary},
        )
        return photo

    @transaction.atomic
    def set_primary(
        self, photo: UnitPhoto, *, actor: str | None = None
    ) -> UnitPhoto:
        if photo.status == UnitPhoto.Status.DELETED:
            raise UnitPhotoStateError("Cannot set primary on a deleted photo.")
        if photo.status == UnitPhoto.Status.DELETE_PENDING:
            raise UnitPhotoStateError("Cannot set primary on a photo pending delete.")
        self._clear_primary(photo.unit, except_id=photo.pk)
        if not photo.is_primary:
            photo.is_primary = True
            photo.save(update_fields=["is_primary", "updated_at"])
        self._enqueue(photo, PhotoOutbox.Kind.SET_PRIMARY)
        audit_unit_photo(
            "primary_changed",
            unit_id=photo.unit_id,
            photo_id=photo.pk,
            checksum=photo.content_checksum,
            actor=actor,
        )
        return photo

    @transaction.atomic
    def reorder(
        self,
        unit: Unit,
        photo_ids_in_order: list[int],
        *,
        actor: str | None = None,
    ) -> list[UnitPhoto]:
        photos = {
            p.pk: p
            for p in UnitPhoto.objects.filter(unit=unit, pk__in=photo_ids_in_order)
            .exclude(status=UnitPhoto.Status.DELETED)
        }
        if len(photos) != len(photo_ids_in_order):
            raise UnitPhotoStateError("Reorder list must include only live photos of this unit.")
        if set(photos.keys()) != set(photo_ids_in_order):
            raise UnitPhotoStateError("Reorder list has duplicate or unknown ids.")

        updated: list[UnitPhoto] = []
        for index, pk in enumerate(photo_ids_in_order):
            photo = photos[pk]
            if photo.sort_order != index:
                photo.sort_order = index
                photo.save(update_fields=["sort_order", "updated_at"])
            self._enqueue(
                photo,
                PhotoOutbox.Kind.REORDER,
                payload={"sort_order": index},
            )
            updated.append(photo)
        audit_unit_photo(
            "reorder",
            unit_id=unit.pk,
            actor=actor,
            extra={"photo_ids": photo_ids_in_order},
        )
        return updated

    @transaction.atomic
    def soft_delete(
        self, photo: UnitPhoto, *, actor: str | None = None
    ) -> UnitPhoto:
        if photo.status in (
            UnitPhoto.Status.DELETED,
            UnitPhoto.Status.DELETE_PENDING,
        ):
            return photo
        was_primary = photo.is_primary
        photo.status = UnitPhoto.Status.DELETE_PENDING
        photo.is_primary = False
        photo.save(update_fields=["status", "is_primary", "updated_at"])
        self._enqueue(photo, PhotoOutbox.Kind.DELETE)
        audit_unit_photo(
            "delete",
            unit_id=photo.unit_id,
            photo_id=photo.pk,
            checksum=photo.content_checksum,
            actor=actor,
        )
        if was_primary:
            successor = (
                UnitPhoto.objects.filter(unit=photo.unit)
                .exclude(pk=photo.pk)
                .exclude(status=UnitPhoto.Status.DELETED)
                .exclude(status=UnitPhoto.Status.DELETE_PENDING)
                .order_by("sort_order", "id")
                .first()
            )
            if successor is not None:
                self.set_primary(successor, actor=actor)
        return photo

    @transaction.atomic
    def replace_photo(
        self,
        photo: UnitPhoto,
        data: bytes,
        *,
        original_filename: str = "",
        actor: str | None = None,
    ) -> UnitPhoto:
        """Business replace = soft_delete old + add_photo new (ADR 0015)."""
        unit = photo.unit
        make_primary = photo.is_primary
        self.soft_delete(photo, actor=actor)
        new_photo = self.add_photo(
            unit,
            data,
            original_filename=original_filename,
            make_primary=make_primary,
            actor=actor,
        )
        audit_unit_photo(
            "replace",
            unit_id=unit.pk,
            photo_id=new_photo.pk,
            checksum=new_photo.content_checksum,
            actor=actor,
            extra={"replaced_photo_id": photo.pk},
        )
        return new_photo
