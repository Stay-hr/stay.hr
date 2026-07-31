"""Unit listing photos — Phase A models (ADR 0015)."""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from apps.core.models import TenantScopedModel


class UnitPhoto(TenantScopedModel):
    """Canonical listing photo owned by exactly one Unit (ADR 0015)."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        UPLOAD_PENDING = "upload_pending", "Upload pending"
        SYNCING = "syncing", "Syncing"
        ACTIVE = "active", "Active"
        DELETE_PENDING = "delete_pending", "Delete pending"
        DELETED = "deleted", "Deleted"
        FAILED = "failed", "Failed"
        OUT_OF_SYNC = "out_of_sync", "Out of sync"

    unit = models.ForeignKey(
        "properties.Unit",
        on_delete=models.CASCADE,
        related_name="photos",
    )
    storage_ref = models.CharField(max_length=512)
    content_checksum = models.CharField(max_length=64, db_index=True)
    original_filename = models.CharField(max_length=255, blank=True)
    is_primary = models.BooleanField(default=False)
    sort_order = models.PositiveIntegerField(default=0)
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "id"]
        indexes = [
            models.Index(fields=["tenant", "unit", "status"]),
            models.Index(fields=["unit", "sort_order"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["unit"],
                condition=Q(is_primary=True) & ~Q(status="deleted"),
                name="properties_unitphoto_one_primary_per_unit",
            ),
        ]

    def __str__(self) -> str:
        primary = " primary" if self.is_primary else ""
        return f"{self.unit.code} photo#{self.pk}{primary} [{self.status}]"

    @property
    def is_deleted(self) -> bool:
        return self.status == self.Status.DELETED


class PhotoOutbox(TenantScopedModel):
    """Async work queue for photo channel projection (ADR 0015)."""

    class Kind(models.TextChoices):
        UPLOAD = "upload", "Upload"
        DELETE = "delete", "Delete"
        REORDER = "reorder", "Reorder"
        SET_PRIMARY = "set_primary", "Set primary"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    unit_photo = models.ForeignKey(
        UnitPhoto,
        on_delete=models.CASCADE,
        related_name="outbox_entries",
    )
    kind = models.CharField(max_length=16, choices=Kind.choices)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    payload = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "status", "kind"]),
            models.Index(fields=["unit_photo", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.status} photo={self.unit_photo_id}"


class UnitPhotoLink(TenantScopedModel):
    """External provider id for a UnitPhoto (e.g. Channex photo UUID)."""

    unit_photo = models.ForeignKey(
        UnitPhoto,
        on_delete=models.CASCADE,
        related_name="links",
    )
    provider = models.CharField(max_length=32, default="channex", db_index=True)
    external_id = models.CharField(max_length=64, blank=True)
    content_checksum_pushed = models.CharField(max_length=64, blank=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    # Soft tombstone after remote DELETE (ADR 0015 Phase B) — keep for audit.
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_checksum = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["unit_photo", "provider"],
                name="properties_unitphotolink_unique_photo_provider",
            ),
        ]

    def __str__(self) -> str:
        tomb = " tombstoned" if self.deleted_at else ""
        return f"{self.provider}:{self.external_id or '—'} photo={self.unit_photo_id}{tomb}"

    @property
    def is_active(self) -> bool:
        return self.deleted_at is None and bool(self.external_id)
