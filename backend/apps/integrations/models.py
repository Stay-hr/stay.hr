from typing import Any

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.models import TenantScopedModel
from apps.integrations.media_storage import get_private_media_storage
from apps.integrations.encryption import decrypt_config, encrypt_config


class IntegrationConfig(TenantScopedModel):
    class Provider(models.TextChoices):
        BOOKING = "booking", "Booking"
        EMAIL = "email", "Email"
        ICAL = "ical", "iCal"
        EVISITOR = "evisitor", "eVisitor"
        CHANNEX = "channex", "Channex"
        WHATSAPP = "whatsapp", "WhatsApp"
        OTHER = "other", "Other"

    property = models.ForeignKey(
        "properties.Property",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="integration_configs",
    )
    provider = models.CharField(max_length=20, choices=Provider.choices)
    routing_key = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
        help_text="Meta phone_number_id for WhatsApp tenant routing.",
    )
    config = models.JSONField(
        default=dict,
        blank=True,
        help_text="Legacy/plaintext fallback; novi zapisi koriste config_encrypted.",
    )
    config_encrypted = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    is_platform_default = models.BooleanField(
        default=False,
        help_text="Default platform WhatsApp config (at most one active per provider).",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["provider", "property_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "provider", "property"],
                name="integrations_config_unique_tenant_provider_property",
            ),
            models.UniqueConstraint(
                fields=["provider", "routing_key"],
                condition=models.Q(routing_key__gt=""),
                name="integrations_config_unique_provider_routing_key",
            ),
            models.UniqueConstraint(
                fields=["provider"],
                condition=models.Q(
                    provider="whatsapp",
                    is_platform_default=True,
                    is_active=True,
                ),
                name="integrations_config_unique_active_platform_whatsapp",
            ),
        ]

    def __str__(self) -> str:
        scope = self.property.slug if self.property_id else "tenant"
        return f"{self.get_provider_display()} ({self.tenant_id}, {scope})"

    def get_config_dict(self) -> dict[str, Any]:
        if self.config_encrypted:
            return decrypt_config(self.config_encrypted)
        return dict(self.config or {})

    def set_config_dict(self, data: dict[str, Any]) -> None:
        self.config = {}
        self.config_encrypted = encrypt_config(data) if data else ""


class ChannexBookingRevision(TenantScopedModel):
    """Tracks processed Channex booking revisions (idempotency + audit)."""

    revision_id = models.CharField(max_length=36, unique=True)
    booking_id = models.CharField(max_length=36, db_index=True)
    reservation = models.ForeignKey(
        "reservations.Reservation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="channex_revisions",
    )
    channex_status = models.CharField(max_length=32, blank=True)
    acknowledged_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-acknowledged_at"]
        indexes = [
            models.Index(fields=["tenant", "booking_id"]),
        ]

    def __str__(self) -> str:
        if self.reservation_id:
            return f"Channex revision {self.revision_id} → reservation {self.reservation_id}"
        return f"Channex revision {self.revision_id} (ack-only, booking {self.booking_id})"


class SalesChannel(models.TextChoices):
    DIRECT = "direct", "Direct / stay"
    BOOKING_COM = "booking_com", "Booking.com"
    AIRBNB = "airbnb", "Airbnb"


PUSH_ENABLED_SALES_CHANNELS = frozenset(
    {SalesChannel.BOOKING_COM, SalesChannel.AIRBNB},
)


class ChannelRatePlan(TenantScopedModel):
    """Unit rate plan per sales channel; Channex UUIDs when push-enabled."""

    property = models.ForeignKey(
        "properties.Property",
        on_delete=models.CASCADE,
        related_name="channel_rate_plans",
    )
    unit = models.ForeignKey(
        "properties.Unit",
        on_delete=models.CASCADE,
        related_name="channel_rate_plans",
    )
    sales_channel = models.CharField(
        max_length=32,
        choices=SalesChannel.choices,
        default=SalesChannel.BOOKING_COM,
    )
    code = models.CharField(max_length=32)
    title = models.CharField(max_length=128, blank=True)
    channex_room_type_id = models.CharField(max_length=36, blank=True, default="")
    channex_rate_plan_id = models.CharField(max_length=36, blank=True, default="")
    default_rate = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    currency = models.CharField(max_length=3, default="GBP")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["property_id", "unit_id", "sales_channel", "code"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "property", "unit", "code", "sales_channel"],
                name="integrations_rateplan_unique_tenant_property_unit_code_channel",
            ),
            models.UniqueConstraint(
                fields=["tenant", "channex_rate_plan_id"],
                condition=models.Q(channex_rate_plan_id__gt=""),
                name="integrations_rateplan_unique_tenant_channex_id",
            ),
        ]

    def is_push_enabled(self) -> bool:
        return (
            self.sales_channel in PUSH_ENABLED_SALES_CHANNELS
            and bool(self.channex_rate_plan_id)
        )

    def __str__(self) -> str:
        channex = self.channex_rate_plan_id[:8] if self.channex_rate_plan_id else "local"
        return f"{self.unit.code}/{self.code}@{self.sales_channel} → {channex}"


class UnitAvailabilityDay(TenantScopedModel):
    unit = models.ForeignKey(
        "properties.Unit",
        on_delete=models.CASCADE,
        related_name="availability_days",
    )
    date = models.DateField()
    availability = models.PositiveSmallIntegerField(default=1)
    synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["date", "unit_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "unit", "date"],
                name="integrations_unitavail_unique_tenant_unit_date",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "unit", "synced_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.unit.code} {self.date}: {self.availability}"


class RatePlanDay(TenantScopedModel):
    rate_plan = models.ForeignKey(
        ChannelRatePlan,
        on_delete=models.CASCADE,
        related_name="days",
    )
    date = models.DateField()
    rate = models.DecimalField(max_digits=10, decimal_places=2)
    min_stay_arrival = models.PositiveSmallIntegerField(null=True, blank=True)
    min_stay_through = models.PositiveSmallIntegerField(null=True, blank=True)
    max_stay = models.PositiveSmallIntegerField(null=True, blank=True)
    stop_sell = models.BooleanField(default=False)
    closed_to_arrival = models.BooleanField(default=False)
    closed_to_departure = models.BooleanField(default=False)
    synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["date", "rate_plan_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "rate_plan", "date"],
                name="integrations_rateplanday_unique_tenant_plan_date",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "rate_plan", "synced_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.rate_plan} {self.date}: {self.rate}"


class ChannexAriOutbox(TenantScopedModel):
    class Kind(models.TextChoices):
        AVAILABILITY = "availability", "Availability"
        RESTRICTIONS = "restrictions", "Rates & restrictions"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        # Terminal disposition for a failed push that must never be retried:
        # the payload is a point-in-time snapshot superseded by later pushes.
        ABANDONED = "abandoned", "Abandoned"

    property = models.ForeignKey(
        "properties.Property",
        on_delete=models.CASCADE,
        related_name="channex_ari_outbox",
    )
    kind = models.CharField(max_length=16, choices=Kind.choices)
    values = models.JSONField(default=list)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    channex_task_ids = models.JSONField(default=list, blank=True)
    error_message = models.TextField(blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "property", "kind", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.status} ({len(self.values)} values)"


class UnitAvailabilityBlock(TenantScopedModel):
    """Manual calendar block created from stay.hr reception."""

    class CreatedVia(models.TextChoices):
        STAY = "stay", "stay.hr"

    unit = models.ForeignKey(
        "properties.Unit",
        on_delete=models.CASCADE,
        related_name="availability_blocks",
    )
    reservation = models.ForeignKey(
        "reservations.Reservation",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="availability_blocks",
    )
    check_in = models.DateField()
    check_out = models.DateField()
    block_ref = models.CharField(max_length=64)
    created_via = models.CharField(
        max_length=16,
        choices=CreatedVia.choices,
        default=CreatedVia.STAY,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["check_in", "unit_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "block_ref"],
                name="integrations_unitblock_unique_tenant_block_ref",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "unit", "check_in"]),
        ]

    def __str__(self) -> str:
        return f"{self.unit.code} block {self.check_in}..{self.check_out}"


def whatsapp_outbound_media_upload_to(instance, filename: str) -> str:
    tenant_id = getattr(instance, "tenant_id", None) or "unknown"
    message_id = getattr(instance, "pk", None) or "unknown"
    return f"whatsapp_outbound/{tenant_id}/{message_id}/{filename}"


def channex_message_media_upload_to(instance, filename: str) -> str:
    tenant_id = getattr(instance, "tenant_id", None) or "unknown"
    message_id = getattr(instance, "pk", None) or "unknown"
    return f"channex_messages/{tenant_id}/{message_id}/{filename}"


class WhatsAppMessage(TenantScopedModel):
    class Direction(models.TextChoices):
        INBOUND = "inbound", "Inbound"
        OUTBOUND = "outbound", "Outbound"

    class Source(models.TextChoices):
        CLOUD_API = "cloud_api", "Cloud API"
        BUSINESS_APP = "business_app", "Business App"

    integration = models.ForeignKey(
        IntegrationConfig,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="whatsapp_messages",
    )
    reservation = models.ForeignKey(
        "reservations.Reservation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="whatsapp_messages",
    )
    wamid = models.CharField(max_length=128, unique=True)
    wa_id = models.CharField(max_length=32, db_index=True)
    phone_number_id = models.CharField(max_length=32, blank=True)
    direction = models.CharField(max_length=16, choices=Direction.choices)
    source = models.CharField(
        max_length=16,
        choices=Source.choices,
        default=Source.CLOUD_API,
    )
    message_type = models.CharField(max_length=32, blank=True)
    body = models.TextField(blank=True)
    media_file = models.FileField(
        upload_to=whatsapp_outbound_media_upload_to,
        blank=True,
        default="",
    )
    raw_payload = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "wa_id", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.direction} {self.wamid} ({self.wa_id})"


def whatsapp_media_archive_upload_to(instance, filename: str) -> str:
    """Server-built path only: the guest-supplied filename never reaches the disk."""
    bucket = (
        "personal"
        if instance.sensitivity == WhatsAppMediaArchive.Sensitivity.PERSONAL_DOCUMENT_CANDIDATE
        else "standard"
    )
    stamp = instance.created_at
    year, month = (stamp.year, f"{stamp.month:02d}") if stamp else ("unknown", "00")
    return f"whatsapp_media/{bucket}/{instance.tenant_id}/{year}/{month}/{filename}"


class WhatsAppMediaArchive(TenantScopedModel):
    """Durable private copy of media carried by a WhatsAppMessage (inbound or echo).

    Independent of document intake: this is the raw-bytes record, intake is processing.
    """

    class Kind(models.TextChoices):
        IMAGE = "image", "Image"
        DOCUMENT = "document", "Document"
        VIDEO = "video", "Video"
        AUDIO = "audio", "Audio"
        VOICE = "voice", "Voice note"
        STICKER = "sticker", "Sticker"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        DOWNLOADING = "downloading", "Downloading"
        ARCHIVED = "archived", "Archived"
        FAILED = "failed", "Failed (will retry)"
        PERMANENT_FAILED = "permanent_failed", "Failed (permanent)"

    class Sensitivity(models.TextChoices):
        STANDARD = "standard", "Standard"
        PERSONAL_DOCUMENT_CANDIDATE = "personal_document_candidate", "Personal document candidate"

    # FK + unique(message, media_id) rather than OneToOne: Cloud API carries one media
    # per message today, but echoes / header media may not stay that way.
    message = models.ForeignKey(
        WhatsAppMessage,
        on_delete=models.CASCADE,
        related_name="media_archives",
    )
    wamid = models.CharField(max_length=128, db_index=True)
    media_id = models.CharField(max_length=128, db_index=True)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    sensitivity = models.CharField(
        max_length=32, choices=Sensitivity.choices, default=Sensitivity.STANDARD
    )
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.PENDING)

    declared_mime = models.CharField(max_length=128, blank=True)
    mime_type = models.CharField(max_length=128, blank=True)
    original_filename = models.CharField(max_length=255, blank=True)
    caption = models.TextField(blank=True)

    size_bytes = models.BigIntegerField(null=True, blank=True)
    sha256 = models.CharField(max_length=64, blank=True, db_index=True)
    file = models.FileField(
        storage=get_private_media_storage,
        upload_to=whatsapp_media_archive_upload_to,
        max_length=255,
        blank=True,
        default="",
    )

    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    # Recorded only. Nothing deletes on this date until a legal basis is defined.
    retention_until = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["message", "media_id"],
                name="uniq_whatsapp_media_archive_message_media",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="archived")
                | (~models.Q(file="") & ~models.Q(sha256="")),
                name="whatsapp_media_archived_has_file_and_sha",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "next_retry_at"], name="wa_media_status_retry_idx"),
            models.Index(fields=["tenant", "created_at"], name="wa_media_tenant_created_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.media_id} [{self.status}]"


class WhatsAppMediaFileDeletion(models.Model):
    """Deletion outbox: a file whose archive row was deleted and must be removed from disk.

    Written in the SAME database transaction as the row deletion (so a rollback leaves
    neither a deletion request nor a deleted file) and processed by a Celery task after
    commit; a beat sweeper retries whatever the broker lost. Deliberately has no foreign
    keys: it must outlive the tenant/message/archive rows whose deletion created it.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        DONE = "done", "Done"
        SKIPPED_IN_USE = "skipped_in_use", "Skipped (file still referenced)"
        DEAD = "dead", "Gave up (needs operator)"

    file_name = models.CharField(max_length=255, unique=True)
    archive_id = models.BigIntegerField(null=True, blank=True)
    tenant_id = models.BigIntegerField(null=True, blank=True)
    reason = models.CharField(max_length=32, default="archive_deleted")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "next_attempt_at"], name="wa_media_del_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.file_name} [{self.status}]"


class WhatsAppInboundRouting(TenantScopedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ROUTED = "routed", "Routed"
        AMBIGUOUS = "ambiguous", "Ambiguous"
        UNROUTED = "unrouted", "Unrouted"
        FAILED = "failed", "Failed"
        DISMISSED = "dismissed", "Dismissed"

    class RoutingMethod(models.TextChoices):
        THREAD = "thread", "Thread"
        BOOKING_CODE = "booking_code", "Booking code"
        PHONE = "phone", "Phone"
        MANUAL = "manual", "Manual"
        NONE = "", "—"

    message = models.OneToOneField(
        WhatsAppMessage,
        on_delete=models.CASCADE,
        related_name="inbound_routing",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    resolved_tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="whatsapp_inbound_routings",
    )
    resolved_reservation = models.ForeignKey(
        "reservations.Reservation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="whatsapp_inbound_routings",
    )
    routing_method = models.CharField(
        max_length=16,
        choices=RoutingMethod.choices,
        blank=True,
        default="",
    )
    candidate_reservations = models.JSONField(default=list, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="whatsapp_inbound_routings_resolved",
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "status", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"routing {self.message_id} → {self.status}"


class ChannexMessage(TenantScopedModel):
    class Direction(models.TextChoices):
        INBOUND = "inbound", "Inbound"
        OUTBOUND = "outbound", "Outbound"

    class Sender(models.TextChoices):
        GUEST = "guest", "Guest"
        PROPERTY = "property", "Property"

    integration = models.ForeignKey(
        IntegrationConfig,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="channex_messages",
    )
    reservation = models.ForeignKey(
        "reservations.Reservation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="channex_messages",
    )
    channex_booking_id = models.CharField(max_length=64, db_index=True)
    message_thread_id = models.CharField(max_length=64, blank=True)
    channex_message_id = models.CharField(max_length=128, unique=True)
    direction = models.CharField(max_length=16, choices=Direction.choices)
    sender = models.CharField(max_length=16, choices=Sender.choices)
    body = models.TextField(blank=True)
    have_attachment = models.BooleanField(default=False)
    media_file = models.FileField(
        upload_to=channex_message_media_upload_to,
        blank=True,
        null=True,
    )
    raw_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "reservation", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.direction} {self.channex_message_id} ({self.channex_booking_id})"


class ChannexReview(TenantScopedModel):
    integration = models.ForeignKey(
        IntegrationConfig,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="channex_reviews",
    )
    reservation = models.ForeignKey(
        "reservations.Reservation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="channex_reviews",
    )
    channex_review_id = models.CharField(max_length=128, unique=True)
    channex_booking_id = models.CharField(max_length=64, blank=True, db_index=True)
    ota = models.CharField(max_length=32, blank=True, db_index=True)
    ota_reservation_id = models.CharField(max_length=64, blank=True)
    ota_review_id = models.CharField(max_length=128, blank=True)
    guest_name = models.CharField(max_length=255, blank=True)
    content = models.TextField(blank=True)
    content_translations = models.JSONField(default=dict, blank=True)
    reply = models.TextField(blank=True)
    overall_score = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
    )
    scores = models.JSONField(default=list, blank=True)
    tags = models.JSONField(default=list, blank=True)
    is_replied = models.BooleanField(default=False)
    is_hidden = models.BooleanField(default=False)
    expired_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(null=True, blank=True, db_index=True)
    reply_sent_at = models.DateTimeField(null=True, blank=True)
    reply_scheduled_at = models.DateTimeField(null=True, blank=True)
    raw_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-received_at", "-created_at"]
        indexes = [
            models.Index(fields=["tenant", "received_at"]),
            models.Index(fields=["tenant", "reservation"]),
            models.Index(fields=["tenant", "is_replied", "received_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.ota} {self.channex_review_id} ({self.overall_score})"
