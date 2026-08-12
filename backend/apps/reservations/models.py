import uuid

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from apps.core.models import TenantScopedModel


def booking_confirmation_pdf_upload_to(instance, filename: str) -> str:
    code = (instance.booking_code or instance.external_id or str(instance.pk or "unknown")).strip()
    tenant_slug = instance.tenant.slug if instance.tenant_id else "unknown"
    return f"booking_confirmations/{tenant_slug}/{code}.pdf"


class Reservation(TenantScopedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        EXPECTED = "expected", "Expected"
        CHECKED_IN = "checked_in", "Checked in"
        CHECKED_OUT = "checked_out", "Checked out"
        CANCELED = "canceled", "Canceled"
        NO_SHOW = "no_show", "No show"
        REFUSED = "refused", "Refused"

    OPERATIONAL_STATUSES = frozenset(
        {Status.EXPECTED, Status.CHECKED_IN, Status.CHECKED_OUT, Status.CANCELED}
    )

    property = models.ForeignKey(
        "properties.Property",
        on_delete=models.CASCADE,
        related_name="reservations",
    )
    external_id = models.CharField(max_length=255, blank=True)
    legacy_id = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    booking_code = models.CharField(max_length=64, blank=True)
    check_in = models.DateField()
    check_out = models.DateField()
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.EXPECTED,
    )
    booking_status = models.CharField(max_length=64, blank=True)
    booker_name = models.CharField(max_length=255)
    booker_email = models.EmailField(blank=True)
    booker_phone = models.CharField(max_length=64, blank=True)
    booker_country = models.CharField(max_length=8, blank=True)
    booker_address = models.TextField(blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, default="EUR")
    source = models.CharField(max_length=64, blank=True)
    import_source = models.CharField(max_length=32, blank=True)
    booked_at = models.DateTimeField(null=True, blank=True)
    units_count = models.PositiveSmallIntegerField(null=True, blank=True)
    persons_count = models.PositiveSmallIntegerField(null=True, blank=True)
    adults_count = models.PositiveSmallIntegerField(null=True, blank=True)
    children_count = models.PositiveSmallIntegerField(null=True, blank=True)
    infants_count = models.PositiveSmallIntegerField(null=True, blank=True)
    expected_checkin_adults = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text=(
            "Check-in domain override: adults expected to submit identity documents. "
            "Null means use OTA adults_count. OTA importers must never write this field."
        ),
    )
    children_ages = models.CharField(max_length=128, blank=True)
    commission_percent = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    commission_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    payment_status = models.CharField(max_length=128, blank=True)
    payment_provider = models.CharField(max_length=255, blank=True)
    notes = models.TextField(blank=True)
    travel_purpose = models.CharField(max_length=128, blank=True)
    booking_device = models.CharField(max_length=64, blank=True)
    nights_count = models.PositiveSmallIntegerField(null=True, blank=True)
    canceled_at = models.DateTimeField(null=True, blank=True)
    details_pending = models.BooleanField(default=False)
    imported_at = models.DateTimeField(null=True, blank=True)
    xls_imported_at = models.DateTimeField(null=True, blank=True)
    pdf_imported_at = models.DateTimeField(null=True, blank=True)
    channel_modified_at = models.DateTimeField(null=True, blank=True)
    confirmation_pdf = models.FileField(
        upload_to=booking_confirmation_pdf_upload_to,
        blank=True,
        null=True,
    )
    whatsapp_welcome_sent_at = models.DateTimeField(null=True, blank=True)
    whatsapp_autocheckin_intro_email_sent_at = models.DateTimeField(null=True, blank=True)
    whatsapp_autocheckin_engaged_at = models.DateTimeField(null=True, blank=True)
    whatsapp_autocheckin_waived_at = models.DateTimeField(null=True, blank=True)
    whatsapp_autocheckin_docs_deadline_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Scheduled Celery ETA for docs deadline (check_in_time + 30 min).",
    )
    whatsapp_autocheckin_session_lost = models.BooleanField(
        default=False,
        help_text="Welcome template sent but guest never engaged before check_in_time - 1h.",
    )
    guest_stated_arrival_at = models.DateTimeField(null=True, blank=True)
    guest_stated_arrival_text = models.CharField(max_length=255, blank=True)
    invoice_email_waiting_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When set, inbound capture waits for a single usable invoice email.",
    )
    booking_payout_received_at = models.DateField(null=True, blank=True, db_index=True)
    booking_payout_id = models.CharField(max_length=64, blank=True, db_index=True)
    booking_payout_net = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    booking_payout_service_fee = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    booking_payout_line = models.ForeignKey(
        "reservations.BookingPayoutLine",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="applied_reservations",
    )

    class FinancialSource(models.TextChoices):
        BOOKING_PDF = "booking_pdf", "Booking PDF"
        BOOKING_PAYOUT = "booking_payout", "Booking payout CSV"
        CHANNEL_MANAGER = "channel_manager", "Channel manager"
        MANUAL = "manual", "Manual"

    financial_source = models.CharField(
        max_length=32,
        choices=FinancialSource.choices,
        blank=True,
    )
    financial_synced_at = models.DateTimeField(null=True, blank=True)
    financial_synced_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reservation_financial_syncs",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-check_in", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "booking_code"],
                condition=models.Q(booking_code__gt=""),
                name="reservations_reservation_unique_tenant_booking_code",
            ),
            models.UniqueConstraint(
                fields=["tenant", "external_id"],
                condition=models.Q(external_id__gt=""),
                name="reservations_reservation_unique_tenant_external_id",
            ),
            models.UniqueConstraint(
                fields=["tenant", "legacy_id"],
                condition=models.Q(legacy_id__isnull=False),
                name="reservations_reservation_unique_tenant_legacy_id",
            ),
        ]

    def __str__(self) -> str:
        label = self.booking_code or self.external_id or str(self.pk)
        return f"{label} ({self.check_in} → {self.check_out})"


class ReservationUnit(TenantScopedModel):
    reservation = models.ForeignKey(
        Reservation,
        on_delete=models.CASCADE,
        related_name="units",
    )
    unit = models.ForeignKey(
        "properties.Unit",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reservation_units",
    )
    legacy_id = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    sort_order = models.PositiveSmallIntegerField(default=0)
    room_name = models.CharField(max_length=256)
    amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["reservation_id", "sort_order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["reservation", "sort_order"],
                name="reservations_unit_unique_sort_order_per_reservation",
            ),
            models.UniqueConstraint(
                fields=["tenant", "legacy_id"],
                condition=models.Q(legacy_id__isnull=False),
                name="reservations_unit_unique_tenant_legacy_id",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.reservation_id} #{self.sort_order}: {self.room_name}"


class EvisitorGuestStatus(models.TextChoices):
    NOT_SENT = "not_sent", "Nije poslano"
    PENDING = "pending", "U tijeku"
    SENT = "sent", "Poslano"
    CHECKED_OUT = "checked_out", "Odjavljeno"
    FAILED = "failed", "Neuspješno"
    CHECKOUT_FAILED = "checkout_failed", "Odjava nije uspjela"


class Guest(TenantScopedModel):
    reservation = models.ForeignKey(
        Reservation,
        on_delete=models.CASCADE,
        related_name="guests",
    )
    legacy_id = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100, blank=True)
    name = models.CharField(max_length=255)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=32, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    document_number = models.CharField(max_length=64, blank=True)
    nationality = models.CharField(max_length=2, blank=True)
    sex = models.CharField(max_length=16, blank=True)
    address = models.TextField(blank=True)
    date_of_issue = models.DateField(null=True, blank=True)
    date_of_expiry = models.DateField(null=True, blank=True)
    issuing_authority = models.CharField(max_length=255, blank=True)
    personal_id_number = models.CharField(max_length=64, blank=True)
    document_additional_number = models.CharField(max_length=64, blank=True)
    additional_personal_id_number = models.CharField(max_length=64, blank=True)
    document_code = models.CharField(max_length=16, blank=True)
    document_type = models.CharField(max_length=64, blank=True)
    document_country = models.CharField(max_length=64, blank=True)
    document_country_iso2 = models.CharField(max_length=2, blank=True)
    document_country_iso3 = models.CharField(max_length=3, blank=True)
    document_country_numeric = models.CharField(max_length=8, blank=True)
    mrz_raw_text = models.TextField(blank=True)
    mrz_verified = models.BooleanField(null=True, blank=True)
    is_primary = models.BooleanField(default=False)
    evisitor_status = models.CharField(max_length=16, blank=True, default="")
    evisitor_registration_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["reservation_id", "-is_primary", "last_name", "first_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["reservation"],
                condition=models.Q(is_primary=True),
                name="reservations_guest_unique_primary_per_reservation",
            ),
            models.UniqueConstraint(
                fields=["tenant", "legacy_id"],
                condition=models.Q(legacy_id__isnull=False),
                name="reservations_guest_unique_tenant_legacy_id",
            ),
            models.UniqueConstraint(
                fields=["reservation", "document_number"],
                condition=~models.Q(document_number=""),
                name="reservations_guest_unique_doc_per_reservation",
            ),
        ]

    def __str__(self) -> str:
        return self.name or f"{self.first_name} {self.last_name}".strip()

    def save(self, *args, **kwargs):
        full = f"{self.first_name} {self.last_name}".strip()
        if full:
            self.name = full
        super().save(*args, **kwargs)


class EvisitorSubmission(TenantScopedModel):
    guest = models.ForeignKey(
        Guest,
        on_delete=models.CASCADE,
        related_name="evisitor_submissions",
    )
    legacy_id = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    registration_id = models.UUIDField()
    status = models.CharField(max_length=16)
    submitted_at = models.DateTimeField(null=True, blank=True)
    error_user_message = models.TextField(blank=True)
    error_system_message = models.TextField(blank=True)
    request_payload = models.JSONField(default=dict, blank=True)
    response_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField()

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "legacy_id"],
                condition=models.Q(legacy_id__isnull=False),
                name="reservations_evisitor_unique_tenant_legacy_id",
            ),
        ]

    def __str__(self) -> str:
        return f"eVisitor {self.status} guest={self.guest_id}"


def id_document_face_upload_to(instance, filename: str) -> str:
    return f"id_documents/faces/{filename}"


def id_document_signature_upload_to(instance, filename: str) -> str:
    return f"id_documents/signatures/{filename}"


def id_document_front_upload_to(instance, filename: str) -> str:
    if getattr(instance, "_passport_photo", False):
        return f"id_documents/passports/{filename}"
    return f"id_documents/{filename}"


def id_document_back_upload_to(instance, filename: str) -> str:
    return f"id_documents/{filename}"


class IdDocument(models.Model):
    guest = models.ForeignKey(
        Guest,
        on_delete=models.CASCADE,
        related_name="id_documents",
    )
    image_path = models.CharField(max_length=500, blank=True, default="")
    face_photo = models.ImageField(
        upload_to=id_document_face_upload_to,
        null=True,
        blank=True,
    )
    signature_photo = models.ImageField(
        upload_to=id_document_signature_upload_to,
        null=True,
        blank=True,
    )
    front_photo = models.ImageField(
        upload_to=id_document_front_upload_to,
        null=True,
        blank=True,
    )
    back_photo = models.ImageField(
        upload_to=id_document_back_upload_to,
        null=True,
        blank=True,
    )
    extracted_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "id"]

    def __str__(self) -> str:
        return f"IdDocument #{self.pk} guest={self.guest_id}"


class DocumentScanStatus(models.TextChoices):
    OK = "ok", "OK"
    FAILED = "failed", "Failed"


def id_recognition_sample_upload_to(instance, filename: str) -> str:
    tenant_id = getattr(instance, "tenant_id", None) or "unknown"
    return f"id_recognition_samples/{tenant_id}/{filename}"


class IdRecognitionSampleSource(models.TextChoices):
    MRZ_PLUS = "mrz_plus", "MRZ Plus"
    MRZ_LEGACY = "mrz_legacy", "MRZ Legacy"


class IdRecognitionSample(TenantScopedModel):
    """Cropped document images for ID recognition model training."""

    reservation = models.ForeignKey(
        Reservation,
        on_delete=models.CASCADE,
        related_name="id_recognition_samples",
    )
    guest = models.ForeignKey(
        Guest,
        on_delete=models.CASCADE,
        related_name="id_recognition_samples",
    )
    image = models.ImageField(upload_to=id_recognition_sample_upload_to)
    source = models.CharField(
        max_length=32,
        choices=IdRecognitionSampleSource.choices,
        default=IdRecognitionSampleSource.MRZ_PLUS,
    )
    document_type = models.CharField(max_length=32, blank=True, default="")
    raw_mrz = models.TextField(blank=True, default="")
    ocr_text = models.TextField(blank=True, default="")
    device_id = models.CharField(max_length=128, blank=True, default="")
    parsed_snapshot = models.JSONField(default=dict, blank=True)
    content_sha256 = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "id"]

    def __str__(self) -> str:
        return f"IdRecognitionSample #{self.pk} guest={self.guest_id} source={self.source}"


class DocumentScanLog(TenantScopedModel):
    reservation = models.ForeignKey(
        Reservation,
        on_delete=models.CASCADE,
        related_name="document_scan_logs",
    )
    guest = models.ForeignKey(
        Guest,
        on_delete=models.CASCADE,
        related_name="document_scan_logs",
    )
    status = models.CharField(max_length=16, choices=DocumentScanStatus.choices)
    method = models.CharField(max_length=8, blank=True, default="")
    device_id = models.CharField(max_length=128, blank=True, default="")
    scanned_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    raw_payload = models.JSONField(default=dict, blank=True)
    suggested_fields = models.JSONField(default=dict, blank=True)
    corrected_fields = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "id"]

    def __str__(self) -> str:
        return f"DocumentScanLog #{self.pk} guest={self.guest_id}"


class DocumentIntakeJobStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    PROCESSING = "processing", "Processing"
    DONE = "done", "Done"
    FAILED = "failed", "Failed"
    APPLIED = "applied", "Applied"


class DocumentIntakeJobSource(models.TextChoices):
    HOSPIRA_BATCH = "hospira_batch", "Hospira batch"
    WHATSAPP = "whatsapp", "WhatsApp"
    WHATSAPP_OPERATOR = "whatsapp_operator", "WhatsApp operator"
    WEB_GUEST = "web_guest", "Web guest check-in"


def document_intake_image_upload_to(instance, filename: str) -> str:
    tenant_id = getattr(instance, "tenant_id", None) or "unknown"
    job_id = getattr(instance, "job_id", None) or "unknown"
    return f"document_intake/{tenant_id}/{job_id}/{filename}"


class DocumentIntakeJob(TenantScopedModel):
    """Batch of shared document photos awaiting OCR and guest matching."""

    status = models.CharField(
        max_length=16,
        choices=DocumentIntakeJobStatus.choices,
        default=DocumentIntakeJobStatus.QUEUED,
    )
    source = models.CharField(
        max_length=32,
        choices=DocumentIntakeJobSource.choices,
        blank=True,
        default="",
    )
    reservation = models.ForeignKey(
        "reservations.Reservation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="document_intake_jobs",
    )
    whatsapp_message = models.ForeignKey(
        "integrations.WhatsAppMessage",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="document_intake_jobs",
    )
    whatsapp_reply_sent = models.BooleanField(default=False)
    guest_checkin_slot_position = models.PositiveSmallIntegerField(null=True, blank=True)
    device_id = models.CharField(max_length=128, blank=True, default="")
    ocr_result = models.JSONField(default=dict, blank=True)
    matches = models.JSONField(default=list, blank=True)
    applied_result = models.JSONField(default=list, blank=True)
    error_message = models.TextField(blank=True, default="")
    processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "id"]

    def __str__(self) -> str:
        return f"DocumentIntakeJob #{self.pk} status={self.status}"


class DocumentIntakeImage(TenantScopedModel):
    job = models.ForeignKey(
        DocumentIntakeJob,
        on_delete=models.CASCADE,
        related_name="images",
    )
    image = models.ImageField(upload_to=document_intake_image_upload_to)
    sort_order = models.PositiveSmallIntegerField(default=0)
    detected_side = models.CharField(max_length=16, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sort_order", "id"]

    def __str__(self) -> str:
        return f"DocumentIntakeImage #{self.pk} job={self.job_id}"


class WhatsAppDocumentBatchStatus(models.TextChoices):
    COLLECTING = "collecting", "Collecting"
    AWAITING_CONFIRM = "awaiting_confirm", "Awaiting confirm"
    AFTER_NO = "after_no", "After no"
    PROCESSING = "processing", "Processing"
    DONE = "done", "Done"


class WhatsAppDocumentBatchSession(TenantScopedModel):
    """Debounce WhatsApp document photos before batch OCR."""

    reservation = models.ForeignKey(
        Reservation,
        on_delete=models.CASCADE,
        related_name="whatsapp_document_batch_sessions",
    )
    job = models.ForeignKey(
        DocumentIntakeJob,
        on_delete=models.CASCADE,
        related_name="whatsapp_batch_sessions",
    )
    wa_id = models.CharField(max_length=32)
    status = models.CharField(
        max_length=20,
        choices=WhatsAppDocumentBatchStatus.choices,
        default=WhatsAppDocumentBatchStatus.COLLECTING,
    )
    last_media_at = models.DateTimeField(null=True, blank=True)
    prompt_sent_at = models.DateTimeField(null=True, blank=True)
    prompt_count = models.PositiveSmallIntegerField(default=0)
    after_no_at = models.DateTimeField(null=True, blank=True)
    confirm_interrupted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "id"]
        indexes = [
            models.Index(fields=["reservation", "status"]),
        ]

    def __str__(self) -> str:
        return f"WhatsAppDocumentBatchSession #{self.pk} reservation={self.reservation_id} status={self.status}"


class WhatsAppOperatorSessionStatus(models.TextChoices):
    COLLECTING = "collecting", "Collecting"
    AWAITING_CONFIRM = "awaiting_confirm", "Awaiting confirm"
    AWAITING_RES_PICK = "awaiting_res_pick", "Awaiting reservation pick"
    PROCESSING = "processing", "Processing"
    DONE = "done", "Done"
    FAILED = "failed", "Failed"


class WhatsAppOperatorSession(TenantScopedModel):
    """Operator WhatsApp document batch before check-in command."""

    operator_wa_id = models.CharField(max_length=32)
    job = models.ForeignKey(
        DocumentIntakeJob,
        on_delete=models.CASCADE,
        related_name="whatsapp_operator_sessions",
    )
    status = models.CharField(
        max_length=24,
        choices=WhatsAppOperatorSessionStatus.choices,
        default=WhatsAppOperatorSessionStatus.COLLECTING,
    )
    last_activity_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-last_activity_at", "id"]
        indexes = [
            models.Index(fields=["tenant", "operator_wa_id", "status"]),
        ]

    def __str__(self) -> str:
        return (
            f"WhatsAppOperatorSession #{self.pk} "
            f"operator={self.operator_wa_id} status={self.status}"
        )


class WhatsAppGuestAutocheckinSessionStatus(models.TextChoices):
    AWAITING_BOOKING_CODE = "awaiting_booking_code", "Awaiting booking code"


class WhatsAppArrivalConfirmSessionStatus(models.TextChoices):
    AWAITING_ARRIVED = "awaiting_arrived", "Awaiting arrived"
    AWAITING_TIME = "awaiting_time", "Awaiting time"
    DONE = "done", "Done"
    DECLINED = "declined", "Declined"


class WhatsAppArrivalConfirmTrigger(models.TextChoices):
    GUEST_DEADLINE_PLUS_30 = "guest_deadline_plus_30", "Guest deadline +30"
    NIGHTLY_23H = "nightly_23h", "Nightly 23h"


class WhatsAppArrivalConfirmSession(TenantScopedModel):
    """Toni arrival gate: operator confirms guest arrival before check-in + eVisitor."""

    reservation = models.ForeignKey(
        Reservation,
        on_delete=models.CASCADE,
        related_name="whatsapp_arrival_confirm_sessions",
    )
    status = models.CharField(
        max_length=20,
        choices=WhatsAppArrivalConfirmSessionStatus.choices,
        default=WhatsAppArrivalConfirmSessionStatus.AWAITING_ARRIVED,
    )
    trigger = models.CharField(
        max_length=24,
        choices=WhatsAppArrivalConfirmTrigger.choices,
    )
    guest_stated_arrival_text = models.CharField(max_length=255, blank=True)
    guest_stated_arrival_at = models.DateTimeField(null=True, blank=True)
    prompted_at = models.DateTimeField(null=True, blank=True)
    responded_operator_wa_id = models.CharField(max_length=32, blank=True)
    confirmed_arrival_at = models.DateTimeField(null=True, blank=True)
    celery_task_id = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "id"]
        indexes = [
            models.Index(fields=["reservation", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["reservation"],
                condition=models.Q(
                    status__in=(
                        WhatsAppArrivalConfirmSessionStatus.AWAITING_ARRIVED,
                        WhatsAppArrivalConfirmSessionStatus.AWAITING_TIME,
                    )
                ),
                name="reservations_arrival_confirm_one_active_per_reservation",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"WhatsAppArrivalConfirmSession #{self.pk} "
            f"reservation={self.reservation_id} status={self.status}"
        )


class WhatsAppGuestAutocheckinSession(TenantScopedModel):
    """Guest WhatsApp thread awaiting booking code for autocheck-in."""

    wa_id = models.CharField(max_length=32)
    status = models.CharField(
        max_length=24,
        choices=WhatsAppGuestAutocheckinSessionStatus.choices,
        default=WhatsAppGuestAutocheckinSessionStatus.AWAITING_BOOKING_CODE,
    )
    last_activity_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-last_activity_at", "id"]
        indexes = [
            models.Index(fields=["tenant", "wa_id", "status"]),
        ]

    def __str__(self) -> str:
        return f"WhatsAppGuestAutocheckinSession #{self.pk} wa_id={self.wa_id}"


class GuestCheckInSessionStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    COMPLETED = "completed", "Completed"
    EXPIRED = "expired", "Expired"
    REVOKED = "revoked", "Revoked"


class GuestCheckInSessionCreatedFrom(models.TextChoices):
    EMAIL = "email", "Email"
    WHATSAPP_AUTOCHECKIN = "whatsapp_autocheckin", "WhatsApp autocheck-in"
    CHANNEX = "channex", "Channex"
    RECEPTION_MANUAL = "reception_manual", "Reception manual"


class GuestCheckInSession(TenantScopedModel):
    """Token-scoped guest web check-in session (one active per reservation)."""

    reservation = models.ForeignKey(
        Reservation,
        on_delete=models.CASCADE,
        related_name="guest_checkin_sessions",
    )
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    status = models.CharField(
        max_length=16,
        choices=GuestCheckInSessionStatus.choices,
        default=GuestCheckInSessionStatus.ACTIVE,
    )
    opens_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    ready_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="First time all required slots became ready (analytics; set once).",
    )
    created_from = models.CharField(
        max_length=32,
        choices=GuestCheckInSessionCreatedFrom.choices,
        help_text="Immutable first origin of this session (analytics).",
    )
    last_distributed_from = models.CharField(
        max_length=32,
        choices=GuestCheckInSessionCreatedFrom.choices,
        null=True,
        blank=True,
        help_text=(
            "Last channel that successfully delivered the check-in link. "
            "Null until first successful send; never set on ensure/reuse alone."
        ),
    )
    ops_version = models.PositiveIntegerField(
        default=0,
        help_text=(
            "Optimistic-lock revision for occupancy/commit/complete. "
            "Not bumped on draft autosave PATCH."
        ),
    )
    last_activity_at = models.DateTimeField(auto_now=True)
    wa_id = models.CharField(max_length=32, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "id"]
        indexes = [
            models.Index(fields=["reservation", "status"]),
            models.Index(fields=["token"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["reservation"],
                condition=models.Q(status=GuestCheckInSessionStatus.ACTIVE),
                name="reservations_guest_checkin_one_active_per_reservation",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"GuestCheckInSession #{self.pk} reservation={self.reservation_id} "
            f"status={self.status}"
        )


class GuestPortalAccessStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    REVOKED = "revoked", "Revoked"


class GuestPortalAccessCreatedFrom(models.TextChoices):
    WHATSAPP = "whatsapp", "WhatsApp"
    EMAIL = "email", "Email"
    RECEPTION_MANUAL = "reception_manual", "Reception manual"
    SYSTEM = "system", "System"


class GuestPortalAccess(TenantScopedModel):
    """Token-scoped guest portal link (one active per reservation; valid through stay)."""

    reservation = models.ForeignKey(
        Reservation,
        on_delete=models.CASCADE,
        related_name="guest_portal_accesses",
    )
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    status = models.CharField(
        max_length=16,
        choices=GuestPortalAccessStatus.choices,
        default=GuestPortalAccessStatus.ACTIVE,
    )
    opens_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    created_from = models.CharField(
        max_length=32,
        choices=GuestPortalAccessCreatedFrom.choices,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "id"]
        indexes = [
            models.Index(
                fields=["reservation", "status"],
                name="reservation_reserva_6f2a1c_idx",
            ),
            models.Index(fields=["token"], name="reservation_token_c8e4d1_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["reservation"],
                condition=models.Q(status=GuestPortalAccessStatus.ACTIVE),
                name="reservations_guest_portal_one_active_per_reservation",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"GuestPortalAccess #{self.pk} reservation={self.reservation_id} "
            f"status={self.status}"
        )


class MonthlyStatisticsOverride(TenantScopedModel):
    """Ručni mjesečni prihod/noći/provizija; nadjačava automatski zbroj iz rezervacija."""

    year = models.PositiveSmallIntegerField()
    month = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(12)],
    )
    revenue = models.DecimalField(max_digits=12, decimal_places=2)
    commission = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    nights = models.PositiveIntegerField()
    currency = models.CharField(max_length=3, default="EUR")
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-year", "-month"]
        verbose_name = "Ručna statistika (mjesec)"
        verbose_name_plural = "Ručna statistika (mjesec)"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "year", "month"],
                name="reservations_monthly_stats_override_tenant_year_month_uniq",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"{self.year}-{self.month:02d} — "
            f"{self.revenue} {self.currency}, {self.nights} noći"
        )


class ReservationVersionScope(models.TextChoices):
    MESSAGES = "messages", "Messages"
    PAYMENTS = "payments", "Payments"
    DOCUMENTS = "documents", "Documents"
    CHECKIN = "checkin", "Check-in"
    HOUSEKEEPING = "housekeeping", "Housekeeping"


class ReservationVersion(models.Model):
    reservation = models.ForeignKey(
        Reservation,
        on_delete=models.CASCADE,
        related_name="version_rows",
    )
    scope = models.CharField(max_length=32, choices=ReservationVersionScope.choices)
    version = models.PositiveBigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["reservation", "scope"],
                name="reservation_scope_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["reservation", "scope"]),
        ]

    def __str__(self) -> str:
        return f"ReservationVersion reservation={self.reservation_id} scope={self.scope} v={self.version}"


from apps.reservations.booking_payout_models import (  # noqa: E402, F401
    BookingPayoutImport,
    BookingPayoutImportStatus,
    BookingPayoutLine,
    BookingPayoutLineSyncResult,
    BookingPayoutMatchStatus,
    BookingPayoutWarningSeverity,
)
