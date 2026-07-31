"""ChannexPhotoProvider — project UnitPhoto outbox to Channex Photos API (ADR 0015 Phase B).

LIST is a read capability for smoke / drift / admin — never used on the upload success path.
"""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.db.models import QuerySet
from django.utils import timezone

from apps.integrations.channex.client import ChannexClient
from apps.integrations.channex.config import ChannexRuntimeConfig
from apps.integrations.channex.exceptions import (
    ChannexApiError,
    PhotoSyncPermanentError,
    PhotoSyncRetryableError,
)
from apps.integrations.channex import photo_metrics
from apps.properties.unit_photos.models import PhotoOutbox, UnitPhoto, UnitPhotoLink
from apps.properties.unit_photos.providers import PhotoCapability
from apps.properties.unit_photos.storage import MediaStorage, default_media_storage

logger = logging.getLogger(__name__)

_PERMANENT_HTTP = frozenset({400, 401, 403, 404, 422})
_RETRYABLE_HTTP = frozenset({408, 425, 429})


def classify_photo_sync_error(exc: BaseException) -> type[Exception]:
    """Return PhotoSyncPermanentError or PhotoSyncRetryableError class for ``exc``."""
    if isinstance(exc, PhotoSyncPermanentError):
        return PhotoSyncPermanentError
    if isinstance(exc, PhotoSyncRetryableError):
        return PhotoSyncRetryableError
    if isinstance(exc, ChannexApiError):
        code = exc.status_code
        if code is None:
            return PhotoSyncRetryableError
        if code in _PERMANENT_HTTP:
            return PhotoSyncPermanentError
        if code in _RETRYABLE_HTTP or code >= 500:
            return PhotoSyncRetryableError
        if 400 <= code < 500:
            return PhotoSyncPermanentError
        return PhotoSyncRetryableError
    return PhotoSyncRetryableError


def raise_classified(exc: BaseException) -> None:
    """Re-raise as permanent/retryable photo sync error."""
    if isinstance(exc, (PhotoSyncPermanentError, PhotoSyncRetryableError)):
        raise exc
    cls = classify_photo_sync_error(exc)
    raise cls(str(exc)) from exc


def active_link_qs(*, unit_photo: UnitPhoto, provider: str) -> QuerySet[UnitPhotoLink]:
    return UnitPhotoLink.objects.filter(
        unit_photo=unit_photo,
        provider=provider,
        deleted_at__isnull=True,
    )


def channex_position_for_photo(photo: UnitPhoto) -> int:
    """Primary → 0; other active photos ordered by sort_order then id."""
    if photo.is_primary:
        return 0
    siblings = list(
        UnitPhoto.objects.filter(unit_id=photo.unit_id)
        .exclude(status=UnitPhoto.Status.DELETED)
        .exclude(status=UnitPhoto.Status.DELETE_PENDING)
        .order_by("-is_primary", "sort_order", "id")
        .values_list("id", "is_primary")
    )
    # positions: primary first at 0, then remaining in order
    ordered_ids = [pid for pid, _ in siblings]
    try:
        idx = ordered_ids.index(photo.pk)
    except ValueError:
        return max(int(photo.sort_order), 1)
    return idx


def positions_for_unit(unit_id: int) -> dict[int, int]:
    """Map unit_photo_id → Channex position for all non-deleted photos on the unit."""
    photos = list(
        UnitPhoto.objects.filter(unit_id=unit_id)
        .exclude(status=UnitPhoto.Status.DELETED)
        .exclude(status=UnitPhoto.Status.DELETE_PENDING)
        .order_by("-is_primary", "sort_order", "id")
    )
    return {p.pk: i for i, p in enumerate(photos)}


class ChannexPhotoProvider:
    """Production PhotoProvider for Channex Photos Collection."""

    PROVIDER = "channex"

    def __init__(
        self,
        *,
        client: ChannexClient,
        config: ChannexRuntimeConfig,
        storage: MediaStorage | None = None,
        verify_after_upload: bool | None = None,
    ) -> None:
        self.client = client
        self.config = config
        self.storage = storage or default_media_storage()
        if verify_after_upload is None:
            verify_after_upload = bool(
                getattr(settings, "UNIT_PHOTO_VERIFY_AFTER_UPLOAD", True)
            )
        self.verify_after_upload = verify_after_upload

    def capabilities(self) -> frozenset[PhotoCapability]:
        return frozenset(
            {
                PhotoCapability.UPLOAD,
                PhotoCapability.DELETE,
                PhotoCapability.REORDER,
                PhotoCapability.SET_PRIMARY,
                PhotoCapability.LIST,
            }
        )

    def apply(self, entry: PhotoOutbox) -> None:
        try:
            if entry.kind == PhotoOutbox.Kind.UPLOAD:
                self._apply_upload(entry)
            elif entry.kind == PhotoOutbox.Kind.DELETE:
                self._apply_delete(entry)
            elif entry.kind in (
                PhotoOutbox.Kind.REORDER,
                PhotoOutbox.Kind.SET_PRIMARY,
            ):
                self._apply_position(entry)
            else:
                raise PhotoSyncPermanentError(f"Unknown outbox kind: {entry.kind}")
        except (PhotoSyncPermanentError, PhotoSyncRetryableError):
            raise
        except Exception as exc:
            raise_classified(exc)

        now = timezone.now()
        entry.status = PhotoOutbox.Status.SENT
        entry.sent_at = now
        entry.error_message = ""
        entry.save(update_fields=["status", "sent_at", "error_message", "updated_at"])

    def apply_positions_batch(
        self,
        *,
        unit_id: int,
        entries: list[PhotoOutbox],
    ) -> None:
        """Apply coalesced REORDER/SET_PRIMARY for one unit (one logical transaction)."""
        if not entries:
            return
        position_map = positions_for_unit(unit_id)
        try:
            for photo_id, position in position_map.items():
                link = (
                    UnitPhotoLink.objects.filter(
                        unit_photo_id=photo_id,
                        provider=self.PROVIDER,
                        deleted_at__isnull=True,
                    )
                    .exclude(external_id="")
                    .select_related("unit_photo", "unit_photo__unit")
                    .first()
                )
                if link is None or not link.external_id:
                    continue
                self.client.update_photo(
                    link.external_id,
                    position=position,
                    property_id=self.config.property_id,
                    room_type_id=self._room_type_id_for_photo(link.unit_photo),
                )
                link.last_sync_at = timezone.now()
                link.save(update_fields=["last_sync_at", "updated_at"])
        except Exception as exc:
            raise_classified(exc)

        now = timezone.now()
        for entry in entries:
            entry.status = PhotoOutbox.Status.SENT
            entry.sent_at = now
            entry.error_message = ""
            entry.save(
                update_fields=["status", "sent_at", "error_message", "updated_at"]
            )
        photo_metrics.incr("photo_reorder_total", value=len(entries), unit_id=unit_id)

    def list_remote(
        self, *, room_type_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Read-only LIST — smoke / drift / admin. Not used by apply(UPLOAD)."""
        return self.client.list_photos(
            property_id=self.config.property_id,
            room_type_id=room_type_id,
        )

    def _room_type_id_for_photo(self, photo: UnitPhoto) -> str:
        unit = photo.unit
        room_type_id = self.config.room_type_id_for_unit_code(unit.code)
        if not room_type_id:
            raise PhotoSyncPermanentError(
                f"No Channex room_type mapping for unit code={unit.code!r}"
            )
        return room_type_id

    def _apply_upload(self, entry: PhotoOutbox) -> None:
        photo = entry.unit_photo
        if photo.status == UnitPhoto.Status.UPLOAD_PENDING:
            photo.status = UnitPhoto.Status.SYNCING
            photo.save(update_fields=["status", "updated_at"])

        link = (
            UnitPhotoLink.objects.filter(
                unit_photo=photo, provider=self.PROVIDER
            ).first()
        )
        if (
            link is not None
            and link.deleted_at is None
            and link.external_id
            and link.content_checksum_pushed == photo.content_checksum
        ):
            photo_metrics.incr("photo_upload_skipped_total", photo_id=photo.pk)
            if photo.status in (
                UnitPhoto.Status.UPLOAD_PENDING,
                UnitPhoto.Status.SYNCING,
            ):
                photo.status = UnitPhoto.Status.ACTIVE
                photo.save(update_fields=["status", "updated_at"])
            return

        room_type_id = self._room_type_id_for_photo(photo)
        property_id = self.config.property_id
        if not property_id:
            raise PhotoSyncPermanentError("Channex property_id missing from config")

        # Replace: remote delete prior active id when checksum differs
        if (
            link is not None
            and link.deleted_at is None
            and link.external_id
            and link.content_checksum_pushed
            and link.content_checksum_pushed != photo.content_checksum
        ):
            try:
                self.client.delete_photo(link.external_id)
            except ChannexApiError as exc:
                if exc.status_code != 404:
                    raise_classified(exc)

        position = channex_position_for_photo(photo)
        try:
            file_bytes = self.storage.open(photo.storage_ref)
            temp_url = self.client.upload_photo_file(
                file_bytes,
                photo.original_filename or "photo.jpg",
            )
            created = self.client.create_photo(
                property_id=property_id,
                url=temp_url,
                room_type_id=room_type_id,
                position=position,
            )
            external_id = self.client.extract_photo_id(created)
            if self.verify_after_upload:
                self._verify_remote(
                    external_id,
                    expected_position=position,
                    expected_room_type_id=room_type_id,
                )
        except Exception as exc:
            photo_metrics.incr("photo_upload_failed_total", photo_id=photo.pk)
            if photo.status != UnitPhoto.Status.DELETED:
                photo.status = UnitPhoto.Status.FAILED
                photo.save(update_fields=["status", "updated_at"])
            raise_classified(exc)

        now = timezone.now()
        if link is None:
            link = UnitPhotoLink(
                tenant=photo.tenant,
                unit_photo=photo,
                provider=self.PROVIDER,
            )
        link.external_id = external_id
        link.content_checksum_pushed = photo.content_checksum
        link.last_sync_at = now
        link.deleted_at = None
        link.deleted_checksum = ""
        link.save()
        photo.status = UnitPhoto.Status.ACTIVE
        photo.save(update_fields=["status", "updated_at"])
        photo_metrics.incr("photo_upload_success_total", photo_id=photo.pk)

    def _verify_remote(
        self,
        external_id: str,
        *,
        expected_position: int,
        expected_room_type_id: str,
    ) -> None:
        remote = self.client.get_photo(external_id)
        attrs = self.client.photo_attributes(remote)
        remote_id = str(attrs.get("id") or remote.get("id") or "")
        if remote_id and remote_id != external_id:
            raise PhotoSyncRetryableError(
                f"Post-upload verify id mismatch: got {remote_id} want {external_id}"
            )
        pos = attrs.get("position")
        if pos is not None and int(pos) != int(expected_position):
            raise PhotoSyncRetryableError(
                f"Post-upload verify position mismatch: got {pos} want {expected_position}"
            )
        rt = attrs.get("room_type_id")
        if rt is not None and str(rt) != str(expected_room_type_id):
            raise PhotoSyncRetryableError(
                f"Post-upload verify room_type mismatch: got {rt} want {expected_room_type_id}"
            )

    def _apply_delete(self, entry: PhotoOutbox) -> None:
        photo = entry.unit_photo
        link = UnitPhotoLink.objects.filter(
            unit_photo=photo, provider=self.PROVIDER
        ).first()
        now = timezone.now()
        if link is not None and link.deleted_at is None and link.external_id:
            try:
                self.client.delete_photo(link.external_id)
            except ChannexApiError as exc:
                if exc.status_code != 404:
                    raise_classified(exc)
            link.deleted_at = now
            link.deleted_checksum = (
                link.content_checksum_pushed or photo.content_checksum
            )
            link.last_sync_at = now
            link.save(
                update_fields=[
                    "deleted_at",
                    "deleted_checksum",
                    "last_sync_at",
                    "updated_at",
                ]
            )
        if photo.status == UnitPhoto.Status.DELETE_PENDING:
            photo.status = UnitPhoto.Status.DELETED
            photo.deleted_at = now
            photo.is_primary = False
            photo.save(
                update_fields=["status", "deleted_at", "is_primary", "updated_at"]
            )
        photo_metrics.incr("photo_delete_total", photo_id=photo.pk)

    def _apply_position(self, entry: PhotoOutbox) -> None:
        """Single-entry position update (prefer apply_positions_batch from worker)."""
        self.apply_positions_batch(
            unit_id=entry.unit_photo.unit_id,
            entries=[entry],
        )
