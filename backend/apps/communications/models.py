from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models

from apps.core.models import TenantScopedModel


def guest_outbound_media_upload_to(instance, filename: str) -> str:
    return (
        f"communications/guest-outbound/{instance.tenant_id}/"
        f"{instance.reservation_id}/{instance.pk}_{filename}"
    )


def guest_message_media_upload_to(instance, filename: str) -> str:
    return (
        f"communications/guest-message/{instance.tenant_id}/"
        f"{instance.conversation_id}/{instance.pk}_{filename}"
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


class GuestMessageDirection(models.TextChoices):
    INBOUND = "inbound", "Inbound"
    OUTBOUND = "outbound", "Outbound"


class GuestMessageSourceProvider(models.TextChoices):
    CHANNEX = "channex", "Channex"
    WABA = "waba", "WhatsApp Cloud API"
    IMAP = "imap", "IMAP"
    SMTP = "smtp", "SMTP"
    STAY_OUTBOUND = "stay_outbound", "Stay outbound"


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


class Conversation(TenantScopedModel):
    """1:1 reservation conversation (ADR 0019 Phase D). Dual-write from D2; GET still uses timeline."""

    reservation = models.OneToOneField(
        "reservations.Reservation",
        on_delete=models.CASCADE,
        related_name="conversation",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Conversation"
        verbose_name_plural = "Conversations"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "reservation"],
                name="communications_conversation_tenant_reservation_uniq",
            ),
        ]

    def __str__(self) -> str:
        return f"Conversation res={self.reservation_id}"

    def clean(self) -> None:
        super().clean()
        if not self.reservation_id or not self.tenant_id:
            return
        reservation_tenant_id = getattr(self.reservation, "tenant_id", None)
        if reservation_tenant_id is None:
            return
        if reservation_tenant_id != self.tenant_id:
            raise ValidationError(
                {"tenant": "Conversation tenant must match reservation.tenant_id."}
            )


class GuestMessage(TenantScopedModel):
    """Logical UI row for one guest message (ADR 0019). Not provider-shaped."""

    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name="messages",
    )
    direction = models.CharField(
        max_length=16,
        choices=GuestMessageDirection.choices,
    )
    channel = models.CharField(
        max_length=16,
        choices=GuestMessageChannel.choices,
    )
    body = models.TextField(blank=True, default="")
    media_file = models.FileField(
        upload_to=guest_message_media_upload_to,
        blank=True,
        null=True,
    )
    occurred_at = models.DateTimeField()
    delivery_status = models.CharField(
        max_length=16,
        choices=GuestOutboundDeliveryStatus.choices,
        blank=True,
        default="",
    )
    is_visible = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Guest message"
        verbose_name_plural = "Guest messages"
        ordering = ["occurred_at", "id"]
        indexes = [
            models.Index(
                fields=["conversation", "occurred_at"],
                name="comm_gm_occurred_idx",
            ),
            models.Index(
                fields=["conversation", "is_visible"],
                name="comm_gm_visible_idx",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"GuestMessage #{self.pk} {self.direction} {self.channel} "
            f"conv={self.conversation_id}"
        )

    def clean(self) -> None:
        super().clean()
        if not self.conversation_id or not self.tenant_id:
            return
        conversation_tenant_id = getattr(self.conversation, "tenant_id", None)
        if conversation_tenant_id is None:
            return
        if conversation_tenant_id != self.tenant_id:
            raise ValidationError(
                {"tenant": "GuestMessage tenant must match Conversation tenant."}
            )


class GuestMessageSource(TenantScopedModel):
    """One external/raw identity for a logical GuestMessage (1..N sources)."""

    RAW_FK_FIELDS = (
        "channex_message",
        "whatsapp_message",
        "inbound_message",
        "outbound_message",
    )

    message = models.ForeignKey(
        GuestMessage,
        on_delete=models.CASCADE,
        related_name="sources",
    )
    provider = models.CharField(
        max_length=32,
        choices=GuestMessageSourceProvider.choices,
    )
    provider_message_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text="Stable provider id when supplied. NULL if omitted; never empty string.",
    )
    channex_message = models.ForeignKey(
        "integrations.ChannexMessage",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="guest_message_sources",
    )
    whatsapp_message = models.ForeignKey(
        "integrations.WhatsAppMessage",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="guest_message_sources",
    )
    inbound_message = models.ForeignKey(
        GuestInboundMessage,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="guest_message_sources",
    )
    outbound_message = models.ForeignKey(
        GuestOutboundMessage,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="guest_message_sources",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Guest message source"
        verbose_name_plural = "Guest message sources"
        indexes = [
            models.Index(
                fields=["tenant", "provider", "provider_message_id"],
                name="comm_gms_provider_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                name="guestmessagesource_exactly_one_raw_fk",
                condition=(
                    models.Q(
                        channex_message__isnull=False,
                        inbound_message__isnull=True,
                        outbound_message__isnull=True,
                        whatsapp_message__isnull=True,
                    )
                    | models.Q(
                        channex_message__isnull=True,
                        inbound_message__isnull=True,
                        outbound_message__isnull=True,
                        whatsapp_message__isnull=False,
                    )
                    | models.Q(
                        channex_message__isnull=True,
                        inbound_message__isnull=False,
                        outbound_message__isnull=True,
                        whatsapp_message__isnull=True,
                    )
                    | models.Q(
                        channex_message__isnull=True,
                        inbound_message__isnull=True,
                        outbound_message__isnull=False,
                        whatsapp_message__isnull=True,
                    )
                ),
            ),
            models.CheckConstraint(
                name="guestmessagesource_provider_message_id_not_blank",
                condition=~models.Q(provider_message_id=""),
            ),
            models.UniqueConstraint(
                fields=["tenant", "provider", "provider_message_id"],
                condition=models.Q(provider_message_id__isnull=False),
                name="guestmessagesource_unique_provider_message_id",
            ),
            models.UniqueConstraint(
                fields=["channex_message"],
                condition=models.Q(channex_message__isnull=False),
                name="guestmessagesource_unique_channex_message",
            ),
            models.UniqueConstraint(
                fields=["whatsapp_message"],
                condition=models.Q(whatsapp_message__isnull=False),
                name="guestmessagesource_unique_whatsapp_message",
            ),
            models.UniqueConstraint(
                fields=["inbound_message"],
                condition=models.Q(inbound_message__isnull=False),
                name="guestmessagesource_unique_inbound_message",
            ),
            models.UniqueConstraint(
                fields=["outbound_message"],
                condition=models.Q(outbound_message__isnull=False),
                name="guestmessagesource_unique_outbound_message",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"Source #{self.pk} {self.provider} "
            f"id={self.provider_message_id or '∅'} msg={self.message_id}"
        )

    def _raw_fk_count(self) -> int:
        return sum(
            1
            for name in self.RAW_FK_FIELDS
            if getattr(self, f"{name}_id") is not None
        )

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        if self.provider_message_id == "":
            errors["provider_message_id"] = (
                "Use NULL when the provider omitted an id; do not store an empty string."
            )
        if self._raw_fk_count() != 1:
            errors["__all__"] = "Exactly one raw pointer must be set."
        if self.message_id and self.tenant_id:
            message_tenant_id = getattr(self.message, "tenant_id", None)
            conversation_tenant_id = None
            conversation = getattr(self.message, "conversation", None)
            if conversation is not None:
                conversation_tenant_id = conversation.tenant_id
            if (
                message_tenant_id is not None
                and conversation_tenant_id is not None
                and (
                    self.tenant_id != message_tenant_id
                    or self.tenant_id != conversation_tenant_id
                )
            ):
                errors["tenant"] = (
                    "Source tenant must match GuestMessage → Conversation tenant."
                )
        if errors:
            raise ValidationError(errors)


class CanonicalConversationBackfill(models.Model):
    """Per-tenant D3 cutoff/complete and D4 read-flag. Default GET stays raw."""

    tenant = models.OneToOneField(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="canonical_backfill",
    )
    cutoff_at = models.DateTimeField(null=True, blank=True)
    cutoff_channex_id = models.PositiveIntegerField(null=True, blank=True)
    cutoff_whatsapp_id = models.PositiveIntegerField(null=True, blank=True)
    cutoff_inbound_id = models.PositiveIntegerField(null=True, blank=True)
    cutoff_outbound_id = models.PositiveIntegerField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    snapshot = models.JSONField(default=dict, blank=True)
    completed_by = models.CharField(max_length=128, blank=True, default="")
    read_canonical_at = models.DateTimeField(null=True, blank=True)
    read_canonical_by = models.CharField(max_length=128, blank=True, default="")
    read_snapshot = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Canonical conversation backfill"
        verbose_name_plural = "Canonical conversation backfills"

    def __str__(self) -> str:
        state = "complete" if self.completed_at else "open"
        read = "read-on" if self.read_canonical_at else "read-off"
        return f"CanonicalBackfill tenant={self.tenant_id} {state} {read}"


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


class PostCheckinSendClaimStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"


class PostCheckinSendClaim(TenantScopedModel):
    """G5 concurrency claim for post-checkin portal / arrival-ask sends.

    UNIQUE ``claim_key`` is the sole gate. Provider I/O runs outside the
    acquire transaction. ``failed`` may be reclaimed; ``pending``/``sent`` block.
    """

    reservation = models.ForeignKey(
        "reservations.Reservation",
        on_delete=models.CASCADE,
        related_name="post_checkin_send_claims",
    )
    claim_key = models.CharField(max_length=255)
    status = models.CharField(
        max_length=16,
        choices=PostCheckinSendClaimStatus.choices,
        default=PostCheckinSendClaimStatus.PENDING,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Post-checkin send claim"
        verbose_name_plural = "Post-checkin send claims"
        constraints = [
            models.UniqueConstraint(
                fields=["claim_key"],
                name="communications_postcheckin_claim_key_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["reservation", "status"]),
        ]

    def __str__(self) -> str:
        return f"Claim {self.claim_key} ({self.status})"


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
    "Conversation",
    "ConversationLanguageSource",
    "GuestInboundMessage",
    "GuestMessage",
    "GuestMessageChannel",
    "GuestMessageDirection",
    "GuestMessageDraft",
    "GuestMessageIntent",
    "GuestMessageSource",
    "GuestMessageSourceProvider",
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
    "PostCheckinSendClaim",
    "PostCheckinSendClaimStatus",
    "PRE_ARRIVAL_INTENTS",
    "WELCOME_INTENTS",
]
