from __future__ import annotations

from django.db import models

from apps.core.models import TenantScopedModel


def guest_outbound_media_upload_to(instance, filename: str) -> str:
    return (
        f"communications/guest-outbound/{instance.tenant_id}/"
        f"{instance.reservation_id}/{instance.pk}_{filename}"
    )


class GuestMessageIntent(models.TextChoices):
    CHECKIN = "checkin", "Check-in"
    REPLY = "reply", "Reply"
    CUSTOM = "custom", "Custom"
    WELCOME_TEMPLATE = "welcome_template", "Welcome template"


class GuestMessageChannel(models.TextChoices):
    EMAIL = "email", "Email"
    WHATSAPP = "whatsapp", "WhatsApp"
    BOOKING = "booking", "Booking.com"


class GuestOutboundMessageStatus(models.TextChoices):
    HANDOFF_WHATSAPP = "handoff_whatsapp", "WhatsApp handoff"
    QUEUED = "queued", "Queued"
    PENDING_SEND = "pending_send", "Pending send"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"


class GuestOutboundDeliveryStatus(models.TextChoices):
    SENT = "sent", "Sent"
    DELIVERED = "delivered", "Delivered"
    READ = "read", "Read"
    FAILED = "failed", "Failed"


class ConversationLanguageSource(models.TextChoices):
    OVERRIDE = "override", "Override"
    REPLY_LANGUAGE = "reply_language", "Reply language"
    MESSAGE = "message", "Message"
    CONVERSATION = "conversation", "Conversation"
    COUNTRY = "country", "Country"
    TENANT_DEFAULT = "tenant_default", "Tenant default"
    FALLBACK = "fallback", "Fallback"


class GuestMessageDraft(TenantScopedModel):
    """LLM compose attempt and optional send audit for a reservation."""

    reservation = models.ForeignKey(
        "reservations.Reservation",
        on_delete=models.CASCADE,
        related_name="guest_message_drafts",
    )
    intent = models.CharField(
        max_length=16,
        choices=GuestMessageIntent.choices,
    )
    hint = models.TextField(blank=True, default="")
    llm_body_text = models.TextField(blank=True, default="")
    final_body_text = models.TextField(blank=True, default="")
    language = models.CharField(max_length=8, blank=True, default="")
    language_source = models.CharField(max_length=32, blank=True, default="")
    language_reason = models.CharField(max_length=255, blank=True, default="")
    channel = models.CharField(
        max_length=16,
        choices=GuestMessageChannel.choices,
        blank=True,
        default="",
    )
    llm_model = models.CharField(max_length=64, blank=True, default="")
    prompt_version = models.CharField(max_length=32, blank=True, default="")
    api_application = models.ForeignKey(
        "tenants.ApiApplication",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="guest_message_drafts",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["tenant", "reservation", "-created_at"]),
        ]
        verbose_name = "Guest message draft"
        verbose_name_plural = "Guest message drafts"

    def __str__(self) -> str:
        channel = self.channel or "—"
        return (
            f"Draft #{self.pk} {self.intent} ({channel}) "
            f"res={self.reservation_id}"
        )

    @property
    def edited(self) -> bool:
        llm = (self.llm_body_text or "").strip()
        final = (self.final_body_text or "").strip()
        if not llm or not final:
            return False
        return llm != final


class GuestOutboundMessage(TenantScopedModel):
    """Outbound guest message audit (email send or WhatsApp handoff)."""

    reservation = models.ForeignKey(
        "reservations.Reservation",
        on_delete=models.CASCADE,
        related_name="guest_outbound_messages",
    )
    draft = models.ForeignKey(
        GuestMessageDraft,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="outbound_messages",
    )
    channel = models.CharField(max_length=16, choices=GuestMessageChannel.choices)
    body_text = models.TextField()
    status = models.CharField(
        max_length=32,
        choices=GuestOutboundMessageStatus.choices,
    )
    to_email = models.EmailField(blank=True, default="")
    to_phone = models.CharField(max_length=64, blank=True, default="")
    wa_me_url = models.TextField(blank=True, default="")
    error_message = models.TextField(blank=True, default="")
    provider = models.CharField(
        max_length=32,
        blank=True,
        default="",
        help_text="Message provider, e.g. meta.",
    )
    provider_message_id = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text="Provider message ID, e.g. Meta wamid.",
    )
    delivery_status = models.CharField(
        max_length=16,
        choices=GuestOutboundDeliveryStatus.choices,
        blank=True,
        default="",
    )
    retry_count = models.PositiveSmallIntegerField(default=0)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    media_file = models.FileField(
        upload_to=guest_outbound_media_upload_to,
        blank=True,
        null=True,
    )
    api_application = models.ForeignKey(
        "tenants.ApiApplication",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="guest_outbound_messages",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["tenant", "reservation", "-created_at"]),
            models.Index(fields=["draft"]),
            models.Index(fields=["provider", "provider_message_id"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "provider_message_id"],
                condition=models.Q(provider_message_id__gt=""),
                name="guest_outbound_unique_provider_message_id",
            ),
            models.UniqueConstraint(
                fields=["draft", "channel"],
                condition=models.Q(draft__isnull=False),
                name="guest_outbound_unique_draft_channel",
            ),
        ]
        verbose_name = "Guest outbound message"
        verbose_name_plural = "Guest outbound messages"

    def __str__(self) -> str:
        return (
            f"Outbound #{self.pk} {self.channel} {self.status} "
            f"res={self.reservation_id}"
        )


class GuestInboundMessage(TenantScopedModel):
    """Manually imported or future-ingested inbound guest message (e.g. email reply)."""

    reservation = models.ForeignKey(
        "reservations.Reservation",
        on_delete=models.CASCADE,
        related_name="guest_inbound_messages",
    )
    channel = models.CharField(max_length=16, choices=GuestMessageChannel.choices)
    body_text = models.TextField()
    from_email = models.EmailField(blank=True, default="")
    raw_from = models.CharField(max_length=512, blank=True, default="")
    subject = models.CharField(max_length=200, blank=True, default="")
    message_id = models.CharField(max_length=255, blank=True, default="")
    received_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(fields=["tenant", "reservation", "created_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "message_id"],
                condition=models.Q(message_id__gt=""),
                name="guestinboundmessage_unique_message_id_per_tenant",
            ),
        ]
        verbose_name = "Guest inbound message"
        verbose_name_plural = "Guest inbound messages"

    def __str__(self) -> str:
        return f"Inbound #{self.pk} {self.channel} res={self.reservation_id}"


class GuestMessageThreadState(TenantScopedModel):
    """Per-reservation inbox flags (e.g. dismissed needs-reply)."""

    reservation = models.OneToOneField(
        "reservations.Reservation",
        on_delete=models.CASCADE,
        related_name="guest_message_thread_state",
    )
    reply_dismissed_at = models.DateTimeField(null=True, blank=True)
    conversation_language = models.CharField(max_length=8, blank=True, default="")
    conversation_language_source = models.CharField(
        max_length=32,
        choices=ConversationLanguageSource.choices,
        blank=True,
        default="",
    )
    conversation_language_updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Guest message thread state"
        verbose_name_plural = "Guest message thread states"
        indexes = [
            models.Index(fields=["tenant", "reservation"]),
        ]

    def __str__(self) -> str:
        return f"ThreadState res={self.reservation_id}"


class GuestMessageTranslationSource(models.TextChoices):
    WHATSAPP = "whatsapp", "WhatsApp"
    OUTBOUND = "outbound", "Outbound"
    BOOKING = "booking", "Booking.com"
    INBOUND = "inbound", "Inbound"


class GuestMessageTranslation(TenantScopedModel):
    """Cached OpenAI translation for a timeline message."""

    message_source = models.CharField(
        max_length=16,
        choices=GuestMessageTranslationSource.choices,
    )
    source_id = models.PositiveIntegerField()
    target_lang = models.CharField(max_length=8)
    translated_text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Guest message translation"
        verbose_name_plural = "Guest message translations"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "message_source", "source_id", "target_lang"],
                name="guestmessagetranslation_unique_cache",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "message_source", "source_id"]),
        ]

    def __str__(self) -> str:
        return (
            f"Translation {self.message_source}:{self.source_id} "
            f"→ {self.target_lang}"
        )


# Messaging Orchestration Engine outbox (ADR 0010) — registered on this app.
from apps.communications.messaging.intents import (  # noqa: E402
    PRE_ARRIVAL_INTENTS,
    WELCOME_INTENTS,
    MessageDefinitionKey,
)
from apps.communications.messaging.models import (  # noqa: E402
    MessageDeliveryAttempt,
    MessageDispatch,
    MessageDispatchEvent,
    MessageDispatchEventType,
    MessageDispatchStatus,
    MessageErrorCategory,
    MessageRecipientType,
    MessageReplayReason,
    MessageScheduleStrategy,
    MessageTriggerKind,
)

__all__ = [
    "ConversationLanguageSource",
    "GuestInboundMessage",
    "GuestMessageChannel",
    "GuestMessageDraft",
    "GuestMessageIntent",
    "GuestMessageThreadState",
    "GuestMessageTranslation",
    "GuestMessageTranslationSource",
    "GuestOutboundDeliveryStatus",
    "GuestOutboundMessage",
    "GuestOutboundMessageStatus",
    "MessageDefinitionKey",
    "MessageDeliveryAttempt",
    "MessageDispatch",
    "MessageDispatchEvent",
    "MessageDispatchEventType",
    "MessageDispatchStatus",
    "MessageErrorCategory",
    "MessageRecipientType",
    "MessageReplayReason",
    "MessageScheduleStrategy",
    "MessageTriggerKind",
    "PRE_ARRIVAL_INTENTS",
    "WELCOME_INTENTS",
]
