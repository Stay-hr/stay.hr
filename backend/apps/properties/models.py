from datetime import time
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models

from apps.core.models import TenantScopedModel


class AfterHoursArrivalPolicy(models.TextChoices):
    CONTACT = "contact", "Contact phone"
    NOT_ALLOWED = "not_allowed", "Entry not allowed after latest time"


class SelfServiceMode(models.TextChoices):
    OFF = "off", "Off"
    ALWAYS = "always", "Always"
    SCHEDULE = "schedule", "Weekly schedule"
    CALENDAR = "calendar", "Calendar dates"


class Property(TenantScopedModel):
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=64)
    address = models.TextField(blank=True)
    contact = models.JSONField(default=dict, blank=True)
    branding = models.JSONField(default=dict, blank=True)
    guest_info = models.JSONField(
        default=dict,
        blank=True,
        help_text="Guest FAQ, localized WhatsApp/check-in texts, maps link, entrance image path.",
    )
    settings_version = models.PositiveIntegerField(
        default=1,
        help_text="Optimistic-concurrency token for Property Settings PATCH (ADR 0008).",
    )
    timezone = models.CharField(max_length=64, blank=True)
    language = models.CharField(max_length=10, blank=True)
    check_in_time = models.TimeField(default=time(15, 0))
    check_out_time = models.TimeField(default=time(11, 0))
    check_in_latest_time = models.TimeField(
        null=True,
        blank=True,
        help_text="Latest self-service arrival time (property local). Null = no upper bound.",
    )
    after_hours_arrival_policy = models.CharField(
        max_length=16,
        choices=AfterHoursArrivalPolicy.choices,
        default=AfterHoursArrivalPolicy.CONTACT,
    )
    after_hours_contact_phone = models.CharField(
        max_length=32,
        blank=True,
        help_text="Phone for late arrivals outside the window. Falls back to property.contact.",
    )
    guest_arrival_auto_reply_enabled = models.BooleanField(
        default=True,
        help_text="Auto-reply on guest arrival-time messages (WhatsApp, email, Channex).",
    )
    guest_parking_auto_reply_enabled = models.BooleanField(
        default=True,
        help_text="Auto-reply when guest asks about parking (WhatsApp, email, Channex).",
    )
    self_service_mode = models.CharField(
        max_length=16,
        choices=SelfServiceMode.choices,
        default=SelfServiceMode.OFF,
        help_text=(
            "When the guest-portal key guide card is shown: "
            "off, always, weekday schedule, or explicit calendar dates."
        ),
    )
    self_service_config = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            'Schedule: {"weekdays": [1]} (Python weekday, Tuesday=1). '
            'Calendar: {"dates": ["YYYY-MM-DD", ...]}.'
        ),
    )
    whatsapp_autocheckin_enabled = models.BooleanField(default=False)
    whatsapp_autocheckin_time = models.TimeField(default=time(8, 0))
    whatsapp_autocheckin_email_lead_minutes = models.PositiveSmallIntegerField(
        default=30,
        help_text="Intro email this many minutes before whatsapp_autocheckin_time (same day).",
    )
    # Messaging orchestration schedules (ADR 0010). Null = inherit Tenant → Platform.
    pre_arrival_days_before = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text="Override: days before check-in for CHECKIN_INFO/LINK. Null = inherit.",
    )
    pre_arrival_send_time = models.TimeField(
        null=True,
        blank=True,
        help_text="Override: local send clock for pre-arrival. Null = inherit.",
    )
    pre_arrival_schedule_strategy = models.CharField(
        max_length=16,
        blank=True,
        default="",
        help_text="Override: FIXED_TIME | FIRST_AFTER | IMMEDIATE. Blank = inherit.",
    )
    welcome_days_before = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text="Override: days before check-in for generic WELCOME schedule. Null = inherit.",
    )
    welcome_send_time = models.TimeField(
        null=True,
        blank=True,
        help_text="Override: local send clock for welcome_*. Null = inherit.",
    )
    welcome_schedule_strategy = models.CharField(
        max_length=16,
        blank=True,
        default="",
        help_text="Override: FIXED_TIME | FIRST_AFTER | IMMEDIATE. Blank = inherit.",
    )
    whatsapp_welcome_days_before = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text="Override: days before check-in for WhatsApp WELCOME. Null = inherit.",
    )
    whatsapp_welcome_send_time = models.TimeField(
        null=True,
        blank=True,
        help_text=(
            "Override: local send clock for WhatsApp welcome. Null falls back to "
            "whatsapp_autocheckin_time then Tenant → Platform."
        ),
    )
    whatsapp_welcome_schedule_strategy = models.CharField(
        max_length=16,
        blank=True,
        default="",
        help_text="Override: FIXED_TIME | FIRST_AFTER | IMMEDIATE. Blank = inherit.",
    )
    guest_checkin_opens_days_before = models.PositiveSmallIntegerField(
        default=7,
        help_text=(
            "Days before arrival when guest web check-in opens (session opens_at, wizard link validity)."
        ),
    )
    financial_report_recipients = models.TextField(
        blank=True,
        default="",
        help_text="Comma-separated emails for monthly property financial report (Celery).",
    )
    tourist_tax_zone = models.ForeignKey(
        "tourist_tax.TouristTaxZone",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="properties",
    )
    tourist_tax_category = models.ForeignKey(
        "tourist_tax.TouristTaxAccommodationCategory",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="properties",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "slug"],
                name="properties_property_unique_tenant_slug",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        super().clean()
        if self.whatsapp_autocheckin_enabled and self.whatsapp_autocheckin_time is None:
            raise ValidationError(
                {"whatsapp_autocheckin_time": "Vrijeme je obavezno kad je autocheck-in uključen."}
            )


class Unit(TenantScopedModel):
    property = models.ForeignKey(
        Property,
        on_delete=models.CASCADE,
        related_name="units",
    )
    code = models.CharField(max_length=32)
    name = models.CharField(max_length=255)
    capacity_max_guests = models.PositiveSmallIntegerField(default=2)
    capacity_adults = models.PositiveSmallIntegerField(default=2)
    capacity_children = models.PositiveSmallIntegerField(default=0)
    capacity_infants = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    default_nightly_rate = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
    )
    nightly_rate_currency = models.CharField(max_length=3, default="EUR")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "property", "code"],
                name="properties_unit_unique_tenant_property_code",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code} — {self.name}"

    def get_capacity_display(self) -> str:
        return (
            f"{self.capacity_max_guests} guests / {self.capacity_adults} adults / "
            f"{self.capacity_children} children / {self.capacity_infants} infants"
        )

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}

        if not 1 <= self.capacity_max_guests <= 50:
            errors["capacity_max_guests"] = "Maximum guests must be between 1 and 50."
        if not 1 <= self.capacity_adults <= 50:
            errors["capacity_adults"] = "Maximum adults must be between 1 and 50."
        if not 0 <= self.capacity_children <= 49:
            errors["capacity_children"] = "Maximum children must be between 0 and 49."
        if not 0 <= self.capacity_infants <= 49:
            errors["capacity_infants"] = "Maximum infants must be between 0 and 49."

        if not errors:
            if self.capacity_adults > self.capacity_max_guests:
                errors["capacity_adults"] = (
                    "Maximum adults cannot exceed maximum guests."
                )
            if self.capacity_children >= self.capacity_max_guests:
                errors["capacity_children"] = (
                    "Maximum children must be less than maximum guests."
                )
            if self.capacity_adults + self.capacity_children < self.capacity_max_guests:
                errors["capacity_max_guests"] = (
                    "Maximum guests cannot exceed the sum of maximum adults and children."
                )

        if errors:
            raise ValidationError(errors)

    def get_beds_display(self) -> str:
        parts = []
        for bed in self.beds.all():
            label = bed.get_bed_type_display().split(" / ")[0]
            parts.append(f"{label} x{bed.count}")
        return ", ".join(parts)

    def get_bathrooms_display(self) -> str:
        bathrooms = list(self.bathrooms.all())
        if not bathrooms:
            return ""
        if len(bathrooms) == 1:
            bath = bathrooms[0]
            parts = []
            if bath.is_private:
                parts.append("private")
            if bath.is_inside_room:
                parts.append("en-suite")
            detail = ", ".join(parts) if parts else "standard"
            return f"1 bathroom ({detail})"
        return f"{len(bathrooms)} bathrooms"


class BedType(models.TextChoices):
    TWIN = "twin", "Twin bed(s) / 90-130 cm wide"
    FULL = "full", "Full bed(s) / 131-150 cm wide"
    QUEEN = "queen", "Queen bed(s) / 151-180 cm wide"
    KING = "king", "King bed(s) / 181-210 cm wide"
    BUNK = "bunk", "Bunk bed / Variable size"
    SOFA = "sofa", "Sofa bed / Variable size"
    FUTON = "futon", "Futon bed(s) / Variable size"


class UnitBed(TenantScopedModel):
    unit = models.ForeignKey(
        Unit,
        on_delete=models.CASCADE,
        related_name="beds",
    )
    bed_type = models.CharField(max_length=16, choices=BedType.choices)
    count = models.PositiveSmallIntegerField(default=1)
    sort_order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["unit", "bed_type"],
                name="properties_unitbed_unique_unit_bed_type",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.unit.code} — {self.get_bed_type_display()} x{self.count}"

    def clean(self) -> None:
        super().clean()
        if not 1 <= self.count <= 10:
            raise ValidationError({"count": "Number of beds must be between 1 and 10."})


class UnitBathroom(TenantScopedModel):
    unit = models.ForeignKey(
        Unit,
        on_delete=models.CASCADE,
        related_name="bathrooms",
    )
    is_private = models.BooleanField(default=True)
    is_inside_room = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["unit", "sort_order"],
                name="properties_unitbathroom_unique_unit_sort_order",
            ),
        ]

    def __str__(self) -> str:
        privacy = "private" if self.is_private else "shared"
        location = "in room" if self.is_inside_room else "outside room"
        return f"{self.unit.code} — Bathroom {self.sort_order + 1} ({privacy}, {location})"


# ADR 0015 — unit listing photos (discovered by Django via this import)
from apps.properties.unit_photos.models import (  # noqa: E402,F401
    PhotoOutbox,
    UnitPhoto,
    UnitPhotoLink,
)
