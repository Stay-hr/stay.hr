"""Outbox models for the Messaging Orchestration Engine (ADR 0010).

MessageDispatch is the outbox (planned → delivered/failed/skipped/cancelled).
Snapshots (timezone, policy, render, recipient) are frozen at create/bind time
and must not be recomputed from live property/guest state.
"""

from __future__ import annotations

import uuid

from django.db import models

from apps.core.models import TenantScopedModel


class MessageTriggerKind(models.TextChoices):
    """v1 trigger kinds; ADR reserves booking/payment/check-in kinds for later."""

    TIME = "TIME", "Time"
    CRON = "CRON", "Cron"
    MANUAL = "MANUAL", "Manual"


class MessageScheduleStrategy(models.TextChoices):
    FIXED_TIME = "FIXED_TIME", "Fixed time"
    FIRST_AFTER = "FIRST_AFTER", "First after"
    IMMEDIATE = "IMMEDIATE", "Immediate"


class MessageDispatchStatus(models.TextChoices):
    PLANNED = "planned", "Planned"
    QUEUED = "queued", "Queued"
    DISPATCHING = "dispatching", "Dispatching"
    DELIVERED = "delivered", "Delivered"
    FAILED = "failed", "Failed"
    SKIPPED = "skipped", "Skipped"
    CANCELLED = "cancelled", "Cancelled"


class MessageReplayReason(models.TextChoices):
    MANUAL = "MANUAL", "Manual"
    PROVIDER_OUTAGE = "PROVIDER_OUTAGE", "Provider outage"
    BUGFIX = "BUGFIX", "Bugfix"
    SUPPORT = "SUPPORT", "Support"


class MessageRecipientType(models.TextChoices):
    BOOKER = "booker", "Booker"
    GUEST = "guest", "Guest"
    CUSTOM = "custom", "Custom"


class MessageErrorCategory(models.TextChoices):
    NETWORK = "NETWORK", "Network"
    AUTH = "AUTH", "Auth"
    VALIDATION = "VALIDATION", "Validation"
    RATE_LIMIT = "RATE_LIMIT", "Rate limit"
    PROVIDER = "PROVIDER", "Provider"
    UNKNOWN = "UNKNOWN", "Unknown"


class MessageDispatchEventType(models.TextChoices):
    DISPATCH_CREATED = "DISPATCH_CREATED", "Dispatch created"
    RENDERED = "RENDERED", "Rendered"
    CHANNEL_SELECTED = "CHANNEL_SELECTED", "Channel selected"
    FALLBACK = "FALLBACK", "Fallback"
    DEFERRED = "DEFERRED", "Deferred"
    DELIVERED = "DELIVERED", "Delivered"
    FAILED = "FAILED", "Failed"
    CANCELLED = "CANCELLED", "Cancelled"
    SKIPPED = "SKIPPED", "Skipped"
    REPLAYED = "REPLAYED", "Replayed"


class MessageDispatch(TenantScopedModel):
    """Outbox row for one planned/queued/delivered orchestration send."""

    reservation = models.ForeignKey(
        "reservations.Reservation",
        on_delete=models.CASCADE,
        related_name="message_dispatches",
    )
    definition_key = models.CharField(
        max_length=64,
        db_index=True,
        help_text="MessageDefinition key, e.g. CHECKIN_INFO, CHECKIN_LINK, WELCOME.",
    )
    plan_key = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="ReminderPlan / plan source key that materialized this dispatch.",
    )
    trigger = models.CharField(
        max_length=16,
        choices=MessageTriggerKind.choices,
    )
    correlation_id = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        db_index=True,
        help_text="End-to-end correlation across materialization and attempts.",
    )
    parent_dispatch = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="child_dispatches",
        help_text="Lineage for replay / resend / follow-up.",
    )
    replay_reason = models.CharField(
        max_length=32,
        choices=MessageReplayReason.choices,
        blank=True,
        default="",
        help_text="Required on MANUAL replay; optional for other lineage creates.",
    )

    # Timing snapshot (frozen at create — do not recompute from live property TZ)
    due_at = models.DateTimeField(
        db_index=True,
        help_text="UTC due timestamp frozen at create.",
    )
    timezone = models.CharField(
        max_length=64,
        help_text="IANA timezone frozen at create (e.g. Europe/Zagreb).",
    )
    local_due_at = models.DateTimeField(
        help_text=(
            "Local wall-clock due time frozen at create; interpret with timezone. "
            "Not recomputed if property timezone changes later."
        ),
    )
    expires_at = models.DateTimeField(null=True, blank=True)
    schedule_strategy = models.CharField(
        max_length=16,
        choices=MessageScheduleStrategy.choices,
        default=MessageScheduleStrategy.FIXED_TIME,
    )

    status = models.CharField(
        max_length=16,
        choices=MessageDispatchStatus.choices,
        default=MessageDispatchStatus.PLANNED,
        db_index=True,
    )
    archived_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Soft-archive only; dispatches are never hard-deleted.",
    )

    policy_version = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text="Frozen channel-policy version when bound (hash/semver + definition).",
    )

    # Render snapshot (set once; fallback must not re-render)
    rendered_body = models.TextField(blank=True, default="")
    rendered_subject = models.TextField(blank=True, default="")
    language = models.CharField(max_length=8, blank=True, default="")
    template_version = models.CharField(max_length=64, blank=True, default="")
    render_context = models.JSONField(default=dict, blank=True)
    render_checksum = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="SHA-256 hex of normalized rendered body (+ subject).",
    )

    # Recipient snapshot (who was targeted; later contact changes do not rewrite)
    recipient_type = models.CharField(
        max_length=16,
        choices=MessageRecipientType.choices,
        blank=True,
        default="",
    )
    recipient_email = models.EmailField(blank=True, default="")
    recipient_phone = models.CharField(max_length=64, blank=True, default="")
    recipient_booking_thread_id = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text="Booking.com / channel messaging thread id when applicable.",
    )

    fallback_used = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["due_at", "id"]
        indexes = [
            models.Index(
                fields=["tenant", "status", "due_at"],
                name="msg_dispatch_claim_idx",
            ),
            models.Index(
                fields=["tenant", "reservation", "definition_key"],
                name="msg_dispatch_dedupe_idx",
            ),
            models.Index(
                fields=["tenant", "definition_key", "status"],
                name="msg_dispatch_def_status_idx",
            ),
        ]
        verbose_name = "Message dispatch"
        verbose_name_plural = "Message dispatches"

    def __str__(self) -> str:
        return (
            f"Dispatch #{self.pk} {self.definition_key} {self.status} "
            f"res={self.reservation_id}"
        )


class MessageDeliveryAttempt(TenantScopedModel):
    """One provider send attempt for a MessageDispatch (DeliveryResult fields)."""

    dispatch = models.ForeignKey(
        MessageDispatch,
        on_delete=models.CASCADE,
        related_name="attempts",
    )
    channel = models.CharField(
        max_length=16,
        help_text="Provider channel, e.g. booking, email, whatsapp.",
    )
    provider = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Registered provider name from ProviderRegistry.",
    )
    attempt_number = models.PositiveSmallIntegerField(default=1)
    success = models.BooleanField(default=False)
    duration_ms = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Wall-clock duration; dispatcher may fill if provider omits.",
    )
    error_category = models.CharField(
        max_length=16,
        choices=MessageErrorCategory.choices,
        blank=True,
        default="",
    )
    retryable = models.BooleanField(default=False)
    error_code = models.CharField(max_length=64, blank=True, default="")
    error_message = models.TextField(blank=True, default="")
    provider_message_id = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text="Provider message id, e.g. Meta wamid.",
    )
    outbound_message = models.ForeignKey(
        "communications.GuestOutboundMessage",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="messaging_delivery_attempts",
        help_text="Timeline-compatible GuestOutboundMessage written for this attempt.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["dispatch_id", "attempt_number", "id"]
        indexes = [
            models.Index(
                fields=["tenant", "dispatch", "attempt_number"],
                name="msg_attempt_dispatch_idx",
            ),
            models.Index(
                fields=["provider", "provider_message_id"],
                name="msg_attempt_provider_idx",
            ),
        ]
        verbose_name = "Message delivery attempt"
        verbose_name_plural = "Message delivery attempts"

    def __str__(self) -> str:
        outcome = "ok" if self.success else "fail"
        return (
            f"Attempt #{self.pk} dispatch={self.dispatch_id} "
            f"{self.channel}/{self.provider} {outcome}"
        )


class MessageDispatchEvent(TenantScopedModel):
    """Append-only audit trail for dispatch lifecycle."""

    dispatch = models.ForeignKey(
        MessageDispatch,
        on_delete=models.CASCADE,
        related_name="events",
    )
    event_type = models.CharField(
        max_length=32,
        choices=MessageDispatchEventType.choices,
    )
    payload = models.JSONField(default=dict, blank=True)
    attempt = models.ForeignKey(
        MessageDeliveryAttempt,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="events",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(
                fields=["tenant", "dispatch", "created_at"],
                name="msg_event_dispatch_idx",
            ),
            models.Index(
                fields=["tenant", "event_type", "created_at"],
                name="msg_event_type_idx",
            ),
        ]
        verbose_name = "Message dispatch event"
        verbose_name_plural = "Message dispatch events"

    def __str__(self) -> str:
        return f"Event #{self.pk} {self.event_type} dispatch={self.dispatch_id}"
