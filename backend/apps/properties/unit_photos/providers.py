"""PhotoProvider protocol + MockPhotoProvider (ADR 0015 Phase A)."""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Protocol

from django.utils import timezone

from apps.properties.unit_photos.models import PhotoOutbox, UnitPhoto, UnitPhotoLink


class PhotoCapability(str, Enum):
    UPLOAD = "upload"
    DELETE = "delete"
    REORDER = "reorder"
    SET_PRIMARY = "set_primary"
    LIST = "list"


class PhotoProvider(Protocol):
    def capabilities(self) -> frozenset[PhotoCapability]: ...

    def apply(self, entry: PhotoOutbox) -> None:
        """Process one outbox entry; update link / mark sent or raise."""


class MockPhotoProvider:
    """Test/dev provider — no Channex calls; invents external ids."""

    PROVIDER = "mock"

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
        photo = entry.unit_photo
        now = timezone.now()
        if entry.kind == PhotoOutbox.Kind.UPLOAD:
            link, _ = UnitPhotoLink.objects.get_or_create(
                tenant=photo.tenant,
                unit_photo=photo,
                provider=self.PROVIDER,
                defaults={"external_id": f"mock-{uuid.uuid4().hex[:12]}"},
            )
            if not link.external_id:
                link.external_id = f"mock-{uuid.uuid4().hex[:12]}"
            link.content_checksum_pushed = photo.content_checksum
            link.last_sync_at = now
            link.save(
                update_fields=[
                    "external_id",
                    "content_checksum_pushed",
                    "last_sync_at",
                    "updated_at",
                ]
            )
            if photo.status == UnitPhoto.Status.UPLOAD_PENDING:
                photo.status = UnitPhoto.Status.ACTIVE
                photo.save(update_fields=["status", "updated_at"])
        elif entry.kind == PhotoOutbox.Kind.DELETE:
            UnitPhotoLink.objects.filter(
                unit_photo=photo, provider=self.PROVIDER
            ).delete()
            if photo.status == UnitPhoto.Status.DELETE_PENDING:
                photo.status = UnitPhoto.Status.DELETED
                photo.deleted_at = now
                photo.is_primary = False
                photo.save(
                    update_fields=["status", "deleted_at", "is_primary", "updated_at"]
                )
        elif entry.kind in (
            PhotoOutbox.Kind.REORDER,
            PhotoOutbox.Kind.SET_PRIMARY,
        ):
            link = UnitPhotoLink.objects.filter(
                unit_photo=photo, provider=self.PROVIDER
            ).first()
            if link is not None:
                link.last_sync_at = now
                link.save(update_fields=["last_sync_at", "updated_at"])

        entry.status = PhotoOutbox.Status.SENT
        entry.sent_at = now
        entry.error_message = ""
        entry.save(update_fields=["status", "sent_at", "error_message", "updated_at"])


def process_pending_outbox(*, provider: PhotoProvider, limit: int = 100) -> int:
    """Drain pending outbox through provider (tests / Phase A helper)."""
    qs = (
        PhotoOutbox.objects.filter(status=PhotoOutbox.Status.PENDING)
        .select_related("unit_photo")
        .order_by("id")[:limit]
    )
    count = 0
    for entry in qs:
        provider.apply(entry)
        count += 1
    return count
