from rest_framework import serializers

from apps.integrations.evisitor.exceptions import (
    EvisitorApiError,
    EvisitorConfigError,
    EvisitorValidationError,
)
from apps.integrations.evisitor.eligibility import guest_requires_evisitor
from apps.integrations.evisitor.residence_address import validate_evisitor_residence_address
from apps.integrations.evisitor.summary import (
    evisitor_progress_for_reservation,
    evisitor_status_for_guest,
    evisitor_summary_for_guests,
    evisitor_summary_for_reservation,
)
from apps.integrations.evisitor.messages import resolve_evisitor_error_message
from apps.reservations.checkin import CheckInBlockedError, get_check_in_block_reason, validate_reservation_check_in
from apps.reservations.checkout import CheckoutBlockedError, perform_reservation_checkout
from apps.reservations.confirmation_pdf import reservation_confirmation_pdf_url
from apps.reservations.face_photo import guest_face_photo_url
from apps.reservations.guest_slots import guests_for_checkout
from apps.reservations.guest_checkin_progress import checkin_progress_for_reservation
from apps.reservations.nationality_display import reservation_nationality_iso2
from apps.reservations.models import (
    EvisitorGuestStatus,
    Guest,
    Reservation,
    ReservationUnit,
)
from apps.reservations.availability import validate_unit_available_for_booking
from apps.reservations.reservation_units import joined_room_names


def payment_status_key(raw: str) -> str:
    value = (raw or "").strip().lower()
    if not value:
        return "unknown"
    if "booking" in value:
        return "booking"
    if any(token in value for token in ("plać", "plac", "paid", "naplać", "naplac")):
        return "paid"
    if any(token in value for token in ("neplać", "neplac", "unpaid", "duguje")):
        return "unpaid"
    if "kartic" in value or "card" in value:
        return "card"
    if "gotov" in value or "cash" in value:
        return "cash"
    return "other"


class GuestLiteSerializer(serializers.ModelSerializer):
    evisitor_status = serializers.SerializerMethodField()
    evisitor_error = serializers.SerializerMethodField()
    evisitor_required = serializers.SerializerMethodField()
    face_photo_url = serializers.SerializerMethodField()

    class Meta:
        model = Guest
        fields = (
            "id",
            "first_name",
            "last_name",
            "email",
            "phone",
            "date_of_birth",
            "sex",
            "address",
            "is_primary",
            "nationality",
            "document_number",
            "document_type",
            "date_of_issue",
            "date_of_expiry",
            "issuing_authority",
            "personal_id_number",
            "evisitor_status",
            "evisitor_error",
            "evisitor_required",
            "face_photo_url",
        )

    def get_evisitor_status(self, obj) -> str:
        return evisitor_status_for_guest(obj)

    def get_evisitor_required(self, obj) -> bool:
        return guest_requires_evisitor(obj)

    def get_evisitor_error(self, obj) -> str:
        if not guest_requires_evisitor(obj):
            return ""
        status = evisitor_status_for_guest(obj)
        if status not in (
            EvisitorGuestStatus.FAILED,
            EvisitorGuestStatus.CHECKOUT_FAILED,
        ):
            return ""
        submission = (
            obj.evisitor_submissions.filter(status=EvisitorGuestStatus.FAILED)
            .order_by("-created_at")
            .first()
        )
        if submission:
            return resolve_evisitor_error_message(
                user_message=submission.error_user_message or "",
                system_message=submission.error_system_message or "",
                fallback=submission.error_user_message or submission.error_system_message or "",
            )
        return ""

    def get_face_photo_url(self, obj) -> str:
        request = self.context.get("request")
        if not request:
            return ""
        return guest_face_photo_url(obj, request)


class ReservationUnitSerializer(serializers.ModelSerializer):
    room = serializers.IntegerField(source="unit_id", read_only=True, allow_null=True)
    room_code = serializers.SerializerMethodField()
    room_type = serializers.SerializerMethodField()

    class Meta:
        model = ReservationUnit
        fields = (
            "id",
            "sort_order",
            "room_name",
            "room_type",
            "room",
            "room_code",
            "amount",
        )

    def get_room_code(self, obj) -> str | None:
        if obj.unit_id and obj.unit:
            return obj.unit.code
        return None

    def get_room_type(self, obj):
        return None


class ReservationTimelineSerializer(serializers.ModelSerializer):
    check_in_date = serializers.DateField(source="check_in", read_only=True)
    check_out_date = serializers.DateField(source="check_out", read_only=True)
    total_amount = serializers.DecimalField(
        source="amount",
        max_digits=12,
        decimal_places=2,
        read_only=True,
        allow_null=True,
    )
    guests = GuestLiteSerializer(many=True, read_only=True)
    units = ReservationUnitSerializer(many=True, read_only=True)
    guests_count = serializers.IntegerField(read_only=True)
    primary_guest_name = serializers.SerializerMethodField()
    primary_guest_nationality_iso2 = serializers.SerializerMethodField()
    room_codes = serializers.SerializerMethodField()
    room_name = serializers.SerializerMethodField()
    effective_units_count = serializers.SerializerMethodField()
    payment_status_key = serializers.SerializerMethodField()
    evisitor_summary = serializers.SerializerMethodField()
    evisitor_progress = serializers.SerializerMethodField()
    evisitor_checkin = serializers.SerializerMethodField()
    checkin_progress = serializers.SerializerMethodField()
    check_in_allowed = serializers.SerializerMethodField()
    check_in_blocked_code = serializers.SerializerMethodField()
    confirmation_pdf_url = serializers.SerializerMethodField()
    invoice_summary = serializers.SerializerMethodField()
    expected_arrival_at = serializers.SerializerMethodField()
    booking_payout_received = serializers.SerializerMethodField()
    property_slug = serializers.CharField(source="property.slug", read_only=True)
    property_name = serializers.CharField(source="property.name", read_only=True)

    class Meta:
        model = Reservation
        fields = (
            "id",
            "external_id",
            "property_slug",
            "property_name",
            "room_name",
            "units",
            "room_codes",
            "check_in_date",
            "check_out_date",
            "status",
            "booking_status",
            "total_amount",
            "currency",
            "booker_name",
            "booker_email",
            "booker_phone",
            "booker_address",
            "booker_country",
            "payment_provider",
            "commission_percent",
            "commission_amount",
            "travel_purpose",
            "booking_device",
            "units_count",
            "effective_units_count",
            "persons_count",
            "adults_count",
            "children_count",
            "infants_count",
            "children_ages",
            "notes",
            "payment_status",
            "payment_status_key",
            "nights_count",
            "booked_at",
            "canceled_at",
            "source",
            "import_source",
            "pdf_imported_at",
            "xls_imported_at",
            "confirmation_pdf_url",
            "guests_count",
            "primary_guest_name",
            "primary_guest_nationality_iso2",
            "guests",
            "evisitor_summary",
            "evisitor_progress",
            "evisitor_checkin",
            "checkin_progress",
            "check_in_allowed",
            "check_in_blocked_code",
            "guest_stated_arrival_text",
            "guest_stated_arrival_at",
            "expected_arrival_at",
            "invoice_summary",
            "booking_payout_received_at",
            "booking_payout_id",
            "booking_payout_net",
            "booking_payout_service_fee",
            "booking_payout_received",
        )

    def get_evisitor_checkin(self, obj) -> dict | None:
        # Only populated on PATCH check-in responses via serializer context.
        return self.context.get("evisitor_checkin")

    def get_booking_payout_received(self, obj) -> bool:
        return obj.booking_payout_received_at is not None

    def get_expected_arrival_at(self, obj):
        from apps.core.timezone import effective_guest_stated_arrival_at

        return effective_guest_stated_arrival_at(obj)

    def get_primary_guest_name(self, obj):
        primary_guest = next((g for g in obj.guests.all() if g.is_primary), None)
        if primary_guest:
            return f"{primary_guest.first_name} {primary_guest.last_name}".strip()
        return ""

    def get_primary_guest_nationality_iso2(self, obj):
        return reservation_nationality_iso2(obj)

    def get_room_codes(self, obj):
        codes = []
        for unit in obj.units.all():
            if unit.unit_id and unit.unit:
                codes.append(unit.unit.code)
        return codes

    def get_room_name(self, obj) -> str:
        return joined_room_names(obj)

    def get_effective_units_count(self, obj) -> int:
        unit_list = list(obj.units.all())
        from_units = len(unit_list)
        from_field = obj.units_count or 0
        return max(from_field, from_units)

    def get_payment_status_key(self, obj) -> str:
        return payment_status_key(obj.payment_status)

    def get_evisitor_summary(self, obj) -> str:
        return evisitor_summary_for_reservation(obj)

    def get_evisitor_progress(self, obj) -> dict[str, int]:
        return evisitor_progress_for_reservation(obj)

    def get_checkin_progress(self, obj) -> dict:
        return checkin_progress_for_reservation(obj)

    def _tenant_for_check_in(self, obj):
        request = self.context.get("request")
        return getattr(request, "tenant", None) or obj.tenant

    def get_check_in_blocked_code(self, obj) -> str | None:
        if obj.status != Reservation.Status.EXPECTED:
            return None
        block = get_check_in_block_reason(obj, tenant=self._tenant_for_check_in(obj))
        if block is None:
            return None
        return block.code

    def get_check_in_allowed(self, obj) -> bool:
        if obj.status != Reservation.Status.EXPECTED:
            return False
        return get_check_in_block_reason(obj, tenant=self._tenant_for_check_in(obj)) is None

    def get_confirmation_pdf_url(self, obj) -> str:
        request = self.context.get("request")
        if not request:
            return ""
        return reservation_confirmation_pdf_url(obj, request)

    def get_invoice_summary(self, obj):
        from apps.billing.models import TenantFiscalSettings

        tenant = self._tenant_for_check_in(obj)
        settings = TenantFiscalSettings.objects.filter(tenant=tenant).first()
        if settings is None or not settings.is_vat_registered:
            return None
        invoice = getattr(obj, "invoice", None)
        if invoice is None:
            return None
        return {
            "id": invoice.pk,
            "invoice_number": invoice.invoice_number,
            "fiscal_status": invoice.fiscal_status,
            "jir": invoice.jir,
            "zki": invoice.zki,
            "email_sent_at": invoice.email_sent_at.isoformat() if invoice.email_sent_at else None,
            "total": str(invoice.total),
            "currency": invoice.currency,
        }


_ALLOWED_STATUS_TRANSITIONS = {
    Reservation.Status.EXPECTED: {
        Reservation.Status.CHECKED_IN,
        Reservation.Status.CANCELED,
        Reservation.Status.NO_SHOW,
    },
    Reservation.Status.CHECKED_IN: {Reservation.Status.CHECKED_OUT},
    Reservation.Status.CHECKED_OUT: set(),
    Reservation.Status.CANCELED: set(),
    Reservation.Status.NO_SHOW: set(),
}


class ReservationUpdateSerializer(serializers.ModelSerializer):
    waived_fees = serializers.BooleanField(required=False, default=True, write_only=True)

    class Meta:
        model = Reservation
        fields = (
            "status",
            "check_in",
            "check_out",
            "waived_fees",
            "guest_stated_arrival_text",
            "guest_stated_arrival_at",
        )

    def validate(self, attrs):
        instance = self.instance
        if instance is None:
            return attrs

        dates_in_payload = "check_in" in attrs or "check_out" in attrs
        if not dates_in_payload:
            return attrs

        if instance.status != Reservation.Status.EXPECTED:
            raise serializers.ValidationError(
                {"detail": "Dates can only be changed for expected reservations."}
            )

        check_in = attrs.get("check_in", instance.check_in)
        check_out = attrs.get("check_out", instance.check_out)
        if check_out <= check_in:
            raise serializers.ValidationError(
                {"check_out": "Check-out must be after check-in."}
            )

        unit_link = (
            instance.units.select_related("unit")
            .order_by("sort_order", "id")
            .first()
        )
        if unit_link is None or unit_link.unit_id is None:
            raise serializers.ValidationError({"detail": "Reservation has no assigned unit."})

        request = self.context.get("request")
        tenant = getattr(request, "tenant", None) or instance.tenant
        try:
            validate_unit_available_for_booking(
                tenant,
                unit_link.unit,
                check_in,
                check_out,
                exclude_reservation_id=instance.pk,
            )
        except ValueError as exc:
            raise serializers.ValidationError({"check_in": str(exc)}) from exc

        attrs["check_in"] = check_in
        attrs["check_out"] = check_out
        return attrs

    def update(self, instance, validated_data):
        waived_fees = validated_data.pop("waived_fees", True)
        new_status = validated_data.get("status", instance.status)
        self._evisitor_checkin = None
        if (
            instance.status == Reservation.Status.EXPECTED
            and new_status == Reservation.Status.CHECKED_IN
        ):
            from apps.reservations.reservation_checkin_complete import (
                perform_reception_checkin,
            )

            request = self.context.get("request")
            tenant = getattr(request, "tenant", None) or instance.tenant
            try:
                result = perform_reception_checkin(instance, tenant=tenant)
            except CheckInBlockedError as exc:
                raise serializers.ValidationError({"status": exc.message}) from exc
            instance.refresh_from_db()
            validated_data["status"] = instance.status
            if result.get("evisitor") is not None:
                self._evisitor_checkin = {
                    **result["evisitor"],
                    "correlation_id": result.get("correlation_id"),
                }
        if (
            instance.status == Reservation.Status.EXPECTED
            and new_status == Reservation.Status.NO_SHOW
        ):
            from apps.integrations.channex.ari_service import get_active_channex_integration
            from apps.integrations.channex.exceptions import ChannexBookingIngestError
            from apps.integrations.channex.no_show_service import (
                is_channex_no_show_eligible,
                report_no_show_for_reservation,
            )

            if is_channex_no_show_eligible(instance):
                try:
                    integration = get_active_channex_integration(instance.tenant.slug)
                    report_no_show_for_reservation(
                        integration,
                        instance,
                        waived_fees=waived_fees,
                    )
                except ChannexBookingIngestError as exc:
                    raise serializers.ValidationError({"status": str(exc)}) from exc
            validated_data["booking_status"] = "no_show"

        if (
            instance.status == Reservation.Status.CHECKED_IN
            and new_status == Reservation.Status.CHECKED_OUT
        ):
            try:
                perform_reservation_checkout(instance, source="manual")
            except CheckoutBlockedError as exc:
                if exc.code == "evisitor_incomplete":
                    raise serializers.ValidationError(
                        {
                            "status": (
                                "Odjava nije moguća dok svi gosti nisu prijavljeni u eVisitor."
                            )
                        }
                    ) from exc
                if exc.code == "evisitor_none":
                    raise serializers.ValidationError(
                        {
                            "status": (
                                "Odjava nije moguća dok svi gosti nisu prijavljeni u eVisitor."
                            )
                        }
                    ) from exc
                if exc.code == "evisitor_checkout_failed":
                    payload = {
                        "code": "evisitor_checkout_failed",
                        "message": str(exc)
                        or "Odjava u eVisitoru nije uspjela za neke goste; "
                        "gosti su i dalje prijavljeni.",
                        "failed_guests": list(exc.failed_guests or []),
                    }
                    raise serializers.ValidationError({"status": payload}) from exc
                if exc.code == "fiscal_config_incomplete":
                    raise serializers.ValidationError(
                        {
                            "status": (
                                "Odjava nije moguća: PDV/fiskalni podaci tenant-a nisu potpuno "
                                "postavljeni u adminu."
                            )
                        }
                    ) from exc
                raise serializers.ValidationError(
                    {"status": "Nedozvoljen prijelaz statusa rezervacije."}
                ) from exc
            except EvisitorValidationError as exc:
                field_errors = getattr(exc, "field_errors", None) or {}
                if field_errors:
                    raise serializers.ValidationError(
                        {"status": "; ".join(f"{k}: {v}" for k, v in field_errors.items())}
                    ) from exc
                raise serializers.ValidationError({"status": str(exc)}) from exc
            except (EvisitorApiError, EvisitorConfigError) as exc:
                user_msg = getattr(exc, "user_message", "") or str(exc)
                raise serializers.ValidationError(
                    {"status": f"eVisitor odjava nije uspjela: {user_msg}"}
                ) from exc
            instance.refresh_from_db()
            validated_data["status"] = instance.status
        return super().update(instance, validated_data)

    def validate_status(self, value):
        allowed = {choice[0] for choice in Reservation.Status.choices}
        if value not in allowed:
            raise serializers.ValidationError("Nepoznat status rezervacije.")

        instance = getattr(self, "instance", None)
        if instance is not None:
            current = instance.status
            if value == current:
                return value
            next_allowed = _ALLOWED_STATUS_TRANSITIONS.get(current, set())
            if value not in next_allowed:
                raise serializers.ValidationError(
                    "Nedozvoljen prijelaz statusa rezervacije."
                )
            if (
                current == Reservation.Status.EXPECTED
                and value == Reservation.Status.CHECKED_IN
            ):
                request = self.context.get("request")
                tenant = getattr(request, "tenant", None) or instance.tenant
                try:
                    validate_reservation_check_in(instance, tenant=tenant)
                except CheckInBlockedError as exc:
                    raise serializers.ValidationError({"status": exc.message}) from exc
            if (
                current == Reservation.Status.CHECKED_IN
                and value == Reservation.Status.CHECKED_OUT
                and evisitor_summary_for_guests(
                    guests_for_checkout(instance),
                    reference_date=instance.check_in,
                )
                != "complete"
            ):
                raise serializers.ValidationError(
                    "Odjava nije moguća dok svi punoljetni gosti nisu prijavljeni u eVisitor."
                )
        return value


_GUEST_WRITABLE_FIELDS = (
    "first_name",
    "last_name",
    "email",
    "date_of_birth",
    "document_number",
    "nationality",
    "sex",
    "address",
    "date_of_issue",
    "date_of_expiry",
    "issuing_authority",
    "personal_id_number",
    "document_additional_number",
    "additional_personal_id_number",
    "document_code",
    "document_type",
    "document_country",
    "document_country_iso2",
    "document_country_iso3",
    "document_country_numeric",
    "mrz_raw_text",
    "mrz_verified",
    "is_primary",
)


class GuestDetailSerializer(serializers.ModelSerializer):
    face_photo_url = serializers.SerializerMethodField()

    class Meta:
        model = Guest
        fields = ("id", "reservation", "face_photo_url", *_GUEST_WRITABLE_FIELDS)
        read_only_fields = ("id", "reservation", "face_photo_url")

    def get_face_photo_url(self, obj) -> str:
        request = self.context.get("request")
        if not request:
            return ""
        return guest_face_photo_url(obj, request)

    def validate_address(self, value):
        raw = (value or "").strip()
        if not raw:
            return ""
        result = validate_evisitor_residence_address(raw)
        if not result.valid:
            raise serializers.ValidationError(
                result.errors[0]
                if result.errors
                else "Nije moguće odrediti grad prebivališta (CityOfResidence)."
            )
        return result.normalized_address

    def update(self, instance, validated_data):
        if validated_data.get("is_primary", False):
            (
                Guest.objects.filter(reservation=instance.reservation)
                .exclude(pk=instance.pk)
                .update(is_primary=False)
            )
        return super().update(instance, validated_data)


class GuestCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Guest
        fields = _GUEST_WRITABLE_FIELDS
        extra_kwargs = {
            "first_name": {"required": False, "allow_blank": True},
            "last_name": {"required": False, "allow_blank": True},
        }

    def validate_address(self, value):
        raw = (value or "").strip()
        if not raw:
            return ""
        result = validate_evisitor_residence_address(raw)
        if not result.valid:
            raise serializers.ValidationError(
                result.errors[0]
                if result.errors
                else "Nije moguće odrediti grad prebivališta (CityOfResidence)."
            )
        return result.normalized_address

    def validate(self, attrs):
        first_name = (attrs.get("first_name") or "").strip()
        last_name = (attrs.get("last_name") or "").strip()
        if not first_name:
            attrs["first_name"] = "Novi"
        else:
            attrs["first_name"] = first_name
        if not last_name:
            attrs["last_name"] = "gost"
        else:
            attrs["last_name"] = last_name
        return attrs

    def create(self, validated_data):
        reservation = self.context["reservation"]
        tenant = self.context["tenant"]
        if "is_primary" not in validated_data:
            validated_data["is_primary"] = not Guest.objects.filter(
                reservation=reservation
            ).exists()
        if validated_data.get("is_primary", False):
            Guest.objects.filter(reservation=reservation).update(is_primary=False)
        return Guest.objects.create(
            tenant=tenant,
            reservation=reservation,
            **validated_data,
        )
