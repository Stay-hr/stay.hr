"""Staff booking intake: LLM parse draft → confirm via create_reception_reservation."""

from __future__ import annotations

from django.conf import settings
from django.db import models

from apps.core.models import TenantScopedModel


class BookingIntakeDraft(TenantScopedModel):
    """Staff paste → LLM proposal → confirm reservation.

    Lifecycle: draft → confirming → confirmed | discarded.
    Confirm is idempotent: a second confirm returns the same reservation.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        CONFIRMING = "confirming", "Confirming"
        CONFIRMED = "confirmed", "Confirmed"
        DISCARDED = "discarded", "Discarded"

    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )
    raw_text = models.TextField()
    parsed_json = models.JSONField(default=dict, blank=True)
    missing_fields = models.JSONField(default=list, blank=True)

    property_slug = models.SlugField(max_length=64, blank=True, default="")
    unit_id = models.PositiveIntegerField(null=True, blank=True)
    unit_code = models.CharField(max_length=64, blank=True, default="")
    check_in = models.DateField(null=True, blank=True)
    check_out = models.DateField(null=True, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, default="EUR")
    booker_name = models.CharField(max_length=255, blank=True, default="")
    booker_phone = models.CharField(max_length=64, blank=True, default="")
    booker_email = models.EmailField(blank=True, default="")
    booker_address = models.TextField(blank=True, default="")
    buyer_company_name = models.CharField(max_length=255, blank=True, default="")
    buyer_oib = models.CharField(max_length=11, blank=True, default="")
    buyer_address = models.TextField(blank=True, default="")
    invoice_email = models.EmailField(blank=True, default="")
    guest_first_name = models.CharField(max_length=100, blank=True, default="")
    guest_last_name = models.CharField(max_length=100, blank=True, default="")

    confirmed_reservation = models.ForeignKey(
        "reservations.Reservation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="booking_intake_drafts",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="booking_intake_drafts",
    )
    llm_model = models.CharField(max_length=64, blank=True, default="")
    prompt_version = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "status", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"BookingIntakeDraft#{self.pk} ({self.status})"
