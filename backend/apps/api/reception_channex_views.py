from __future__ import annotations

from decimal import Decimal

from rest_framework import serializers, status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.api.permissions import DenyAdminScopes, HasReceptionAccess
from apps.api.reception_views import ReceptionReadView, ReceptionWriteView, TenantAPIView
from apps.integrations.channex.ari_service import (
    FULL_SYNC_DAYS,
    apply_availability_updates,
    apply_rate_updates,
    build_clamped_availability_updates,
    build_full_sync,
    get_active_channex_integration,
    push_channex_ari,
    seed_channel_rate_plans_from_config,
    sync_property,
)
from apps.properties.models import Property, Unit
from apps.integrations.channex.config import ChannexRuntimeConfig
from apps.integrations.models import ChannelRatePlan, ChannexAriOutbox, RatePlanDay, SalesChannel, UnitAvailabilityDay
from apps.integrations.pricing.obp import get_obp_policy, serialize_obp_policy, serialize_rate_obp_fields
from apps.integrations.pricing.sales_channel import parse_sales_channel
from apps.integrations.channex.ari_views import (
    AvailabilityUpdateItemSerializer,
    RateUpdateItemSerializer,
)
from apps.integrations.channex.exceptions import ChannexApiError, ChannexBookingIngestError
from apps.integrations.channex.tasks import flush_channex_ari_outbox_task
from apps.integrations.channel_manager.resolver import get_channel_manager, require_channex
from apps.reservations.availability import validate_unit_available_for_booking
from apps.reservations.models import Reservation, ReservationUnit
from apps.tenants.models import ChannelManager


def _guard_channex_tenant(tenant) -> None:
    try:
        require_channex(tenant)
    except Exception as exc:
        raise PermissionDenied(str(exc)) from exc


class ReceptionChannelStatusView(ReceptionReadView, APIView):
    def get(self, request):
        manager = get_channel_manager(request.tenant)
        pending_outbox = 0
        last_push = None
        if manager == ChannelManager.CHANNEX:
            pending_outbox = ChannexAriOutbox.objects.filter(
                tenant=request.tenant,
                status=ChannexAriOutbox.Status.PENDING,
            ).count()
            last_sent = (
                ChannexAriOutbox.objects.filter(
                    tenant=request.tenant,
                    status=ChannexAriOutbox.Status.SENT,
                )
                .order_by("-sent_at")
                .first()
            )
            if last_sent is not None:
                last_push = last_sent.sent_at.isoformat() if last_sent.sent_at else None

        return Response(
            {
                "channel_manager": manager,
                "pending_outbox": pending_outbox,
                "last_push": last_push,
            }
        )


def _channel_rate_plans_queryset(tenant, integration, sales_channel: str | None = None):
    config = ChannexRuntimeConfig.from_integration_dict(integration.get_config_dict())
    prop = sync_property(tenant, config)
    qs = ChannelRatePlan.objects.filter(
        tenant=tenant,
        property=prop,
        is_active=True,
    )
    if sales_channel is not None:
        qs = qs.filter(sales_channel=sales_channel)
    return qs.select_related("unit").order_by("unit__code", "sales_channel", "code")


def _serialize_channel_rate_plan(plan: ChannelRatePlan) -> dict:
    payload = {
        "id": plan.id,
        "unit_code": plan.unit.code,
        "unit_name": plan.unit.name or plan.unit.code,
        "sales_channel": plan.sales_channel,
        "code": plan.code,
        "title": plan.title or plan.code,
        "default_rate": format(plan.default_rate.quantize(Decimal("0.01")), "f"),
        "currency": plan.currency,
    }
    if plan.sales_channel == SalesChannel.BOOKING_COM:
        policy = get_obp_policy(plan.unit.code)
        payload["obp"] = serialize_obp_policy(policy, plan.default_rate, plan.unit.code)
    else:
        payload["obp"] = None
    return payload


def _serialize_channel_rate_day(row: RatePlanDay) -> dict:
    unit_code = row.rate_plan.unit.code
    base_rate = row.rate.quantize(Decimal("0.01"))
    payload = {
        "unit_id": row.rate_plan.unit_id,
        "unit_code": unit_code,
        "sales_channel": row.rate_plan.sales_channel,
        "rate_plan_code": row.rate_plan.code,
        "rate_plan_title": row.rate_plan.title or row.rate_plan.code,
        "currency": row.rate_plan.currency,
        "date": row.date.isoformat(),
        "rate": format(base_rate, "f"),
        "stop_sell": row.stop_sell,
        "min_stay_arrival": row.min_stay_arrival or 1,
    }
    if row.rate_plan.sales_channel == SalesChannel.BOOKING_COM:
        payload.update(serialize_rate_obp_fields(base_rate, unit_code))
    return payload


class ChannelRatePlanUpdateItemSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    default_rate = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0"))


class ReceptionChannelRatePlansView(TenantAPIView, APIView):
    permission_classes = [HasReceptionAccess, DenyAdminScopes]

    def get_permissions(self):
        if self.request.method == "GET":
            self.required_scopes = ["reception:read"]
        else:
            self.required_scopes = ["reception:write"]
        return [permission() for permission in self.permission_classes]

    def get(self, request):
        _guard_channex_tenant(request.tenant)
        try:
            sales_channel = parse_sales_channel(request.query_params.get("sales_channel"))
        except ValueError as exc:
            raise ValidationError({"sales_channel": str(exc)}) from exc
        integration = get_active_channex_integration(request.tenant.slug)
        queryset = _channel_rate_plans_queryset(
            request.tenant, integration, sales_channel=sales_channel
        )
        if not queryset.exists() and sales_channel == SalesChannel.BOOKING_COM:
            seed_channel_rate_plans_from_config(integration)
            queryset = _channel_rate_plans_queryset(
                request.tenant, integration, sales_channel=sales_channel
            )
        return Response({"results": [_serialize_channel_rate_plan(plan) for plan in queryset]})

    def patch(self, request):
        _guard_channex_tenant(request.tenant)
        serializer = ChannelRatePlanUpdateItemSerializer(
            data=request.data.get("updates", []),
            many=True,
        )
        serializer.is_valid(raise_exception=True)
        integration = get_active_channex_integration(request.tenant.slug)
        try:
            sales_channel = parse_sales_channel(request.query_params.get("sales_channel"))
        except ValueError as exc:
            raise ValidationError({"sales_channel": str(exc)}) from exc
        allowed_ids = set(
            _channel_rate_plans_queryset(
                request.tenant, integration, sales_channel=sales_channel
            ).values_list("id", flat=True)
        )
        updated = 0
        for item in serializer.validated_data:
            plan_id = item["id"]
            if plan_id not in allowed_ids:
                raise PermissionDenied(f"Rate plan id={plan_id} is not available for this tenant.")
            rows = ChannelRatePlan.objects.filter(pk=plan_id, tenant=request.tenant).update(
                default_rate=item["default_rate"]
            )
            updated += rows
        queryset = _channel_rate_plans_queryset(
            request.tenant, integration, sales_channel=sales_channel
        )
        return Response(
            {
                "updated": updated,
                "results": [_serialize_channel_rate_plan(plan) for plan in queryset],
            }
        )


class ReceptionChannelCalendarAriView(ReceptionReadView, APIView):
    def get(self, request):
        from django.utils.dateparse import parse_date

        _guard_channex_tenant(request.tenant)
        integration = get_active_channex_integration(request.tenant.slug)
        config = ChannexRuntimeConfig.from_integration_dict(integration.get_config_dict())
        prop = sync_property(request.tenant, config)

        date_from = parse_date((request.query_params.get("from") or "").strip())
        date_to = parse_date((request.query_params.get("to") or "").strip())
        if date_from is None or date_to is None:
            raise ValidationError({"detail": "Query params from and to (YYYY-MM-DD) are required."})
        if date_to < date_from:
            raise ValidationError({"detail": "to must be on or after from."})

        try:
            sales_channel = parse_sales_channel(request.query_params.get("sales_channel"))
        except ValueError as exc:
            raise ValidationError({"sales_channel": str(exc)}) from exc

        availability_rows = UnitAvailabilityDay.objects.filter(
            tenant=request.tenant,
            unit__property=prop,
            date__gte=date_from,
            date__lt=date_to,
        ).select_related("unit")

        rate_rows = RatePlanDay.objects.filter(
            tenant=request.tenant,
            rate_plan__property=prop,
            rate_plan__sales_channel=sales_channel,
            date__gte=date_from,
            date__lt=date_to,
        ).select_related("rate_plan", "rate_plan__unit")

        return Response(
            {
                "availability": [
                    {
                        "unit_id": row.unit_id,
                        "date": row.date.isoformat(),
                        "availability": row.availability,
                    }
                    for row in availability_rows
                ],
                "rates": [_serialize_channel_rate_day(row) for row in rate_rows],
            }
        )


class ChannelBulkRateItemSerializer(serializers.Serializer):
    rate_plan_code = serializers.CharField()
    rate = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False, allow_null=True
    )
    min_stay_arrival = serializers.IntegerField(required=False, min_value=1)
    min_stay_through = serializers.IntegerField(required=False, min_value=1)
    max_stay = serializers.IntegerField(required=False, min_value=0)
    stop_sell = serializers.BooleanField(required=False)
    closed_to_arrival = serializers.BooleanField(required=False)
    closed_to_departure = serializers.BooleanField(required=False)

    def validate(self, attrs):
        has_rate = attrs.get("rate") is not None
        has_restriction = any(
            attrs.get(field) is not None
            for field in (
                "min_stay_arrival",
                "min_stay_through",
                "max_stay",
                "stop_sell",
                "closed_to_arrival",
                "closed_to_departure",
            )
        )
        if not has_rate and not has_restriction:
            raise serializers.ValidationError(
                "Each rate item needs at least a rate or one restriction field."
            )
        return attrs


class ChannelBulkApplySerializer(serializers.Serializer):
    unit_code = serializers.CharField(required=False)
    unit_codes = serializers.ListField(
        child=serializers.CharField(), required=False, min_length=1
    )
    sales_channel = serializers.ChoiceField(
        choices=SalesChannel.choices,
        default=SalesChannel.BOOKING_COM,
        required=False,
    )
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    rates = ChannelBulkRateItemSerializer(many=True, required=False, default=list)
    availability = serializers.IntegerField(required=False, allow_null=True, min_value=0)

    def validate(self, attrs):
        unit_codes = list(attrs.pop("unit_codes", None) or [])
        unit_code = attrs.pop("unit_code", None)
        if unit_code:
            unit_codes.append(unit_code)
        seen: set[str] = set()
        unique_codes: list[str] = []
        for code in unit_codes:
            if code not in seen:
                seen.add(code)
                unique_codes.append(code)
        if not unique_codes:
            raise serializers.ValidationError(
                {"unit_codes": "Provide at least one unit_code or unit_codes entry."}
            )
        attrs["unit_codes"] = unique_codes

        date_from = attrs["date_from"]
        date_to = attrs["date_to"]
        if date_to < date_from:
            raise serializers.ValidationError({"date_to": "Must be on or after date_from."})

        rates = attrs.get("rates") or []
        availability = attrs.get("availability")
        if not rates and availability is None:
            raise serializers.ValidationError(
                "Provide at least one rate update or availability change."
            )
        return attrs


class ReceptionChannelBulkApplyView(ReceptionWriteView, APIView):
    def post(self, request):
        _guard_channex_tenant(request.tenant)
        serializer = ChannelBulkApplySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        integration = get_active_channex_integration(request.tenant.slug)
        unit_codes = data["unit_codes"]
        sales_channel = data.get("sales_channel") or SalesChannel.BOOKING_COM
        rate_queue_push = sales_channel == SalesChannel.BOOKING_COM

        rate_items: list[dict] = []
        for unit_code in unit_codes:
            for item in data.get("rates") or []:
                payload: dict = {
                    "unit_code": unit_code,
                    "rate_plan_code": item["rate_plan_code"],
                    "sales_channel": sales_channel,
                    "date_from": data["date_from"],
                    "date_to": data["date_to"],
                }
                if item.get("rate") is not None:
                    payload["rate"] = item["rate"]
                for field in (
                    "min_stay_arrival",
                    "min_stay_through",
                    "max_stay",
                    "stop_sell",
                    "closed_to_arrival",
                    "closed_to_departure",
                ):
                    if field in item:
                        payload[field] = item[field]
                rate_items.append(payload)

        if rate_items:
            rate_serializer = RateUpdateItemSerializer(data=rate_items, many=True)
            rate_serializer.is_valid(raise_exception=True)
            rate_items = rate_serializer.validated_data

        availability_items: list[dict] = []
        protected_nights: list[dict] = []
        if data.get("availability") is not None:
            requested = int(data["availability"])
            config = ChannexRuntimeConfig.from_integration_dict(integration.get_config_dict())
            prop = sync_property(request.tenant, config)
            units_by_code = {
                unit.code: unit
                for unit in Unit.objects.filter(
                    tenant=request.tenant,
                    property=prop,
                    code__in=unit_codes,
                    is_active=True,
                )
            }
            for unit_code in unit_codes:
                unit = units_by_code.get(unit_code)
                if unit is None:
                    raise ValidationError({"unit_codes": f"Unit not found: {unit_code}"})
                clamped_items, protected = build_clamped_availability_updates(
                    request.tenant,
                    unit,
                    data["date_from"],
                    data["date_to"],
                    requested,
                )
                availability_items.extend(clamped_items)
                if protected:
                    protected_nights.append(
                        {"unit_code": unit_code, "dates": protected}
                    )
            if availability_items:
                avail_serializer = AvailabilityUpdateItemSerializer(
                    data=availability_items, many=True
                )
                avail_serializer.is_valid(raise_exception=True)
                availability_items = avail_serializer.validated_data

        try:
            rate_rows = (
                apply_rate_updates(integration, rate_items, queue_push=rate_queue_push)
                if rate_items
                else []
            )
            avail_queue_push = bool(availability_items)
            avail_rows = (
                apply_availability_updates(
                    integration, availability_items, queue_push=avail_queue_push
                )
                if availability_items
                else []
            )
            should_push = rate_queue_push or avail_queue_push
            if should_push and request.query_params.get("async", "").lower() in {"1", "true", "yes"}:
                flush_channex_ari_outbox_task.delay(request.tenant.slug)
                pushed = []
            elif should_push:
                pushed = push_channex_ari(integration)
            else:
                pushed = []
        except (ChannexApiError, ChannexBookingIngestError) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        return Response(
            {
                "rate_days_updated": len(rate_rows),
                "availability_days_updated": len(avail_rows),
                "protected_nights": protected_nights,
                "push_results": pushed,
            },
            status=status.HTTP_200_OK,
        )


class ReceptionChannelFullSyncView(ReceptionWriteView, APIView):
    def post(self, request):
        _guard_channex_tenant(request.tenant)
        integration = get_active_channex_integration(request.tenant.slug)
        seed_channel_rate_plans_from_config(integration)
        days = int(request.data.get("days") or FULL_SYNC_DAYS)
        try:
            availability_values, restriction_values = build_full_sync(
                integration, days=days
            )
            if request.query_params.get("async", "").lower() in {"1", "true", "yes"}:
                flush_channex_ari_outbox_task.delay(request.tenant.slug)
                pushed = []
            else:
                pushed = push_channex_ari(integration)
        except (ChannexApiError, ChannexBookingIngestError) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        return Response(
            {
                "availability_value_batches": len(availability_values),
                "restrictions_value_batches": len(restriction_values),
                "push_results": pushed,
            },
            status=status.HTTP_200_OK,
        )


class ReceptionChannelRatesView(ReceptionWriteView, APIView):
    def patch(self, request):
        _guard_channex_tenant(request.tenant)
        serializer = RateUpdateItemSerializer(data=request.data.get("updates", []), many=True)
        serializer.is_valid(raise_exception=True)
        integration = get_active_channex_integration(request.tenant.slug)
        try:
            rows = apply_rate_updates(integration, serializer.validated_data)
            if request.query_params.get("async", "").lower() in {"1", "true", "yes"}:
                flush_channex_ari_outbox_task.delay(request.tenant.slug)
                pushed = []
            else:
                pushed = push_channex_ari(integration)
        except (ChannexApiError, ChannexBookingIngestError) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        return Response({"updated_days": len(rows), "push_results": pushed})


class ReceptionChannelAvailabilityView(ReceptionWriteView, APIView):
    def patch(self, request):
        _guard_channex_tenant(request.tenant)
        serializer = AvailabilityUpdateItemSerializer(
            data=request.data.get("updates", []), many=True
        )
        serializer.is_valid(raise_exception=True)
        integration = get_active_channex_integration(request.tenant.slug)
        try:
            rows = apply_availability_updates(integration, serializer.validated_data)
            if request.query_params.get("async", "").lower() in {"1", "true", "yes"}:
                flush_channex_ari_outbox_task.delay(request.tenant.slug)
                pushed = []
            else:
                pushed = push_channex_ari(integration)
        except (ChannexApiError, ChannexBookingIngestError) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        return Response({"updated_days": len(rows), "push_results": pushed})


class ReceptionReservationCreateSerializer(serializers.Serializer):
    property_slug = serializers.SlugField()
    unit_id = serializers.IntegerField()
    check_in = serializers.DateField()
    check_out = serializers.DateField()
    booker_name = serializers.CharField(max_length=255)
    booker_phone = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")

    def validate_booker_phone(self, value: str) -> str:
        from apps.reservations.phone_validation import validate_booker_phone

        return validate_booker_phone(value)

    def validate(self, attrs):
        if attrs["check_out"] <= attrs["check_in"]:
            raise serializers.ValidationError(
                {"check_out": "Check-out must be after check-in."}
            )

        tenant = self.context["tenant"]
        try:
            prop = Property.objects.get(tenant=tenant, slug=attrs["property_slug"])
        except Property.DoesNotExist as exc:
            raise serializers.ValidationError({"property_slug": "Property not found."}) from exc

        unit = (
            Unit.objects.for_tenant(tenant)
            .filter(pk=attrs["unit_id"], property=prop, is_active=True)
            .first()
        )
        if unit is None:
            raise serializers.ValidationError({"unit_id": "Unit not found for this property."})

        try:
            validate_unit_available_for_booking(
                tenant,
                unit,
                attrs["check_in"],
                attrs["check_out"],
            )
        except ValueError as exc:
            raise serializers.ValidationError({"unit_id": str(exc)}) from exc

        attrs["property"] = prop
        attrs["unit"] = unit
        return attrs


class ReceptionReservationCreateView(ReceptionWriteView, APIView):
    def post(self, request):
        manager = get_channel_manager(request.tenant)
        if manager not in {ChannelManager.CHANNEX, ChannelManager.NONE}:
            raise PermissionDenied(
                "Manual reception reservation create is not enabled for this tenant."
            )

        serializer = ReceptionReservationCreateSerializer(
            data=request.data,
            context={"tenant": request.tenant},
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        prop = data["property"]
        unit = data["unit"]

        reservation = Reservation.objects.create(
            tenant=request.tenant,
            property=prop,
            check_in=data["check_in"],
            check_out=data["check_out"],
            booker_name=data["booker_name"],
            booker_phone=data.get("booker_phone") or "",
            import_source="manual",
            source="reception",
            status=Reservation.Status.EXPECTED,
        )
        ReservationUnit.objects.create(
            tenant=request.tenant,
            reservation=reservation,
            unit=unit,
            sort_order=0,
            room_name=unit.name or unit.code,
        )

        from apps.api.reception_serializers import ReservationTimelineSerializer

        detail = Reservation.objects.filter(pk=reservation.pk).first()
        if detail is None:
            raise NotFound("Reservation not found after create.")
        output = ReservationTimelineSerializer(detail, context={"request": request})
        return Response(output.data, status=status.HTTP_201_CREATED)


class ChannexMessageSendSerializer(serializers.Serializer):
    message = serializers.CharField(max_length=4000, trim_whitespace=True)


class ReceptionReservationChannexMessagesView(TenantAPIView, APIView):
    permission_classes = [HasReceptionAccess, DenyAdminScopes]

    def get_permissions(self):
        if self.request.method == "GET":
            self.required_scopes = ["reception:read"]
        else:
            self.required_scopes = ["reception:write"]
        return [permission() for permission in self.permission_classes]

    def _reservation(self, request, reservation_id: int) -> Reservation:
        reservation = (
            Reservation.objects.filter(tenant=request.tenant, pk=reservation_id)
            .select_related("property")
            .first()
        )
        if reservation is None:
            raise NotFound("Reservation not found.")
        return reservation

    def get(self, request, reservation_id: int):
        _guard_channex_tenant(request.tenant)
        reservation = self._reservation(request, reservation_id)
        try:
            integration = get_active_channex_integration(request.tenant.slug)
        except ChannexBookingIngestError as exc:
            raise ValidationError(str(exc)) from exc

        try:
            from apps.integrations.channex.message_service import (
                list_messages_for_reservation,
                serialize_channex_message,
            )

            # ADR 0019 Phase A: GET never pulls Channex. ``sync`` query is ignored.
            rows = list_messages_for_reservation(
                integration,
                reservation,
                sync_if_empty=False,
                force_sync=False,
            )
        except ChannexBookingIngestError as exc:
            raise ValidationError(str(exc)) from exc
        except ChannexApiError as exc:
            raise ValidationError(str(exc)) from exc

        return Response(
            {
                "reservation_id": reservation.pk,
                "messages": [serialize_channex_message(row) for row in rows],
            }
        )

    def post(self, request, reservation_id: int):
        _guard_channex_tenant(request.tenant)
        reservation = self._reservation(request, reservation_id)
        serializer = ChannexMessageSendSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            integration = get_active_channex_integration(request.tenant.slug)
        except ChannexBookingIngestError as exc:
            raise ValidationError(str(exc)) from exc

        try:
            from apps.integrations.channex.message_service import (
                send_message_for_reservation,
                serialize_channex_message,
            )

            row = send_message_for_reservation(
                integration,
                reservation,
                serializer.validated_data["message"],
            )
        except ChannexBookingIngestError as exc:
            raise ValidationError(str(exc)) from exc
        except ChannexApiError as exc:
            raise ValidationError(str(exc)) from exc

        return Response(
            serialize_channex_message(row),
            status=status.HTTP_201_CREATED,
        )
