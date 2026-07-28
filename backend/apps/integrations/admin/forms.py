from __future__ import annotations

import json
from typing import Any

from django import forms

from django.contrib import messages

from apps.integrations.config_secrets import PROVIDER_SECRET_KEYS
from apps.integrations.models import IntegrationConfig
from apps.integrations.whatsapp.welcome_template_config import (
    default_whatsapp_templates_block,
    inspect_welcome_map,
)

SECRET_KEEP_HELP = "Leave blank to keep the current value."


def _json_dumps(value: Any) -> str:
    if not value:
        return ""
    return json.dumps(value, indent=2, ensure_ascii=False)


def _parse_json_field(raw: str, field_name: str) -> Any:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise forms.ValidationError(f"{field_name} must be valid JSON: {exc}") from exc


class IntegrationConfigAdminForm(forms.ModelForm):
    class Meta:
        model = IntegrationConfig
        fields = (
            "tenant",
            "property",
            "provider",
            "routing_key",
            "is_active",
        )

    def __init__(self, *args, admin_request=None, **kwargs):
        self._admin_request = admin_request
        super().__init__(*args, **kwargs)
        provider = self._resolved_provider()
        self._provider_secret_fields: list[str] = []
        self._provider_config_fields: list[str] = []
        if provider:
            self._build_provider_fields(provider)

    def _resolved_provider(self) -> str:
        if self.data.get("provider"):
            return str(self.data.get("provider"))
        if self.instance.pk and self.instance.provider:
            return self.instance.provider
        if self._admin_request is not None:
            get_provider = self._admin_request.GET.get("provider")
            if get_provider:
                return str(get_provider)
        return str(self.initial.get("provider") or "")

    def _existing_config(self) -> dict[str, Any]:
        if self.instance.pk:
            return dict(self.instance.get_config_dict())
        return {}

    def _secret_field(self, name: str, *, label: str, help_text: str = SECRET_KEEP_HELP) -> None:
        self.fields[name] = forms.CharField(
            label=label,
            required=False,
            widget=forms.PasswordInput(render_value=False),
            help_text=help_text,
        )
        self._provider_secret_fields.append(name)

    def _config_char_field(
        self,
        name: str,
        *,
        label: str,
        initial: str = "",
        required: bool = False,
        help_text: str = "",
    ) -> None:
        self.fields[name] = forms.CharField(
            label=label,
            required=required,
            initial=initial,
            help_text=help_text,
        )
        self._provider_config_fields.append(name)

    def _config_bool_field(
        self,
        name: str,
        *,
        label: str,
        initial: bool = False,
        help_text: str = "",
    ) -> None:
        self.fields[name] = forms.BooleanField(
            label=label,
            required=False,
            initial=initial,
            help_text=help_text,
        )
        self._provider_config_fields.append(name)

    def _config_json_field(
        self,
        name: str,
        *,
        label: str,
        config_key: str,
        initial_value: Any = None,
        help_text: str = "",
    ) -> None:
        default_help = f"Stored as config.{config_key} (JSON array/object)."
        self.fields[name] = forms.CharField(
            label=label,
            required=False,
            widget=forms.Textarea(attrs={"rows": 8, "class": "vLargeTextField"}),
            initial=_json_dumps(initial_value),
            help_text=help_text or default_help,
        )
        self.fields[name].config_key = config_key  # type: ignore[attr-defined]
        self._provider_config_fields.append(name)

    def _build_provider_fields(self, provider: str) -> None:
        config = self._existing_config()
        if provider == IntegrationConfig.Provider.CHANNEX:
            self._build_channex_fields(config)
        elif provider == IntegrationConfig.Provider.WHATSAPP:
            self._build_whatsapp_fields(config)
        elif provider == IntegrationConfig.Provider.EVISITOR:
            self._build_evisitor_fields(config)

    def _build_channex_fields(self, config: dict[str, Any]) -> None:
        self._secret_field("api_key", label="Channex API key")
        self._secret_field("webhook_secret", label="Channex webhook secret")
        self._config_char_field(
            "environment",
            label="Environment",
            initial=str(config.get("environment") or "staging"),
            help_text="staging or production",
        )
        self._config_char_field(
            "base_url",
            label="Base URL",
            initial=str(config.get("base_url") or ""),
        )
        self._config_char_field(
            "property_id",
            label="Channex property ID",
            initial=str(config.get("property_id") or ""),
            required=False,
        )
        self._config_char_field(
            "sync_property_slug",
            label="Sync property slug",
            initial=str(config.get("sync_property_slug") or ""),
        )
        self._config_char_field(
            "certification_property_slug",
            label="Certification property slug",
            initial=str(config.get("certification_property_slug") or ""),
        )
        self._config_bool_field(
            "use_generated_ari",
            label="Use generated ARI (cert mode)",
            initial=bool(config.get("use_generated_ari")),
        )
        self._config_json_field(
            "room_types_json",
            label="Room types mapping (JSON)",
            config_key="room_types",
            initial_value=config.get("room_types") or [],
        )
        self._config_json_field(
            "booking_test_rooms_json",
            label="Booking test rooms (JSON)",
            config_key="booking_test_rooms",
            initial_value=config.get("booking_test_rooms") or [],
        )

    def _build_whatsapp_fields(self, config: dict[str, Any]) -> None:
        existing_phone = str(config.get("phone_number_id") or "").strip()
        phone_immutable = bool(self.instance.pk and existing_phone)
        self._config_char_field(
            "phone_number_id",
            label="Phone number ID",
            initial=existing_phone,
            required=not self.instance.pk,
            help_text="Meta phone_number_id; synced to routing_key. Immutable after create.",
        )
        if phone_immutable:
            self.fields["phone_number_id"].disabled = True
        self._config_char_field(
            "display_phone_number",
            label="Display phone number",
            initial=str(config.get("display_phone_number") or ""),
            help_text="E.164 display number for UI and wa.me links only.",
        )
        self._config_char_field(
            "waba_id",
            label="WABA ID",
            initial=str(config.get("waba_id") or ""),
            help_text="Optional; required for template sync/create. Falls back to WHATSAPP_WABA_ID.",
        )
        self._config_bool_field(
            "auto_reply",
            label="Auto reply enabled",
            initial=bool(config.get("auto_reply", True)),
        )
        if self.instance.pk and getattr(self.instance, "is_platform_default", False):
            self._config_bool_field(
                "is_platform_default",
                label="Platform default WhatsApp",
                initial=True,
                help_text="Default outbound/inbound platform number.",
            )
            self._provider_config_fields.append("is_platform_default")
        self._config_json_field(
            "whatsapp_templates_json",
            label="WhatsApp templates (JSON)",
            config_key="whatsapp_templates",
            initial_value=config.get("whatsapp_templates")
            or default_whatsapp_templates_block(),
            help_text=(
                "welcome map is merge-filled from the registry on seed. "
                "Non-empty custom values are kept; names should start with stay_welcome_."
            ),
        )

    def _build_evisitor_fields(self, config: dict[str, Any]) -> None:
        self._secret_field("password", label="eVisitor password")
        self._secret_field("api_key", label="eVisitor API key")
        self._config_bool_field(
            "enabled",
            label="Enabled",
            initial=bool(config.get("enabled")),
        )
        self._config_char_field(
            "env",
            label="Environment",
            initial=str(config.get("env") or "test"),
        )
        self._config_char_field(
            "base_url",
            label="Base URL",
            initial=str(config.get("base_url") or ""),
        )
        self._config_char_field(
            "username",
            label="Username",
            initial=str(config.get("username") or ""),
        )
        self._config_char_field(
            "facility_code",
            label="Facility code",
            initial=str(config.get("facility_code") or ""),
        )
        self._config_char_field(
            "default_arrival_organisation",
            label="Default arrival organisation",
            initial=str(config.get("default_arrival_organisation") or "I"),
        )
        self._config_char_field(
            "default_offered_service_type",
            label="Default offered service type",
            initial=str(config.get("default_offered_service_type") or "noćenje"),
        )
        self._config_char_field(
            "default_payment_category",
            label="Default payment category",
            initial=str(config.get("default_payment_category") or "14"),
        )
        self._config_char_field(
            "default_stay_time_from",
            label="Default stay time from",
            initial=str(config.get("default_stay_time_from") or "15:00"),
            help_text=(
                "Fallback when no Property is set. For reservations, Property "
                "check_in_time / check_out_time take precedence."
            ),
        )
        self._config_char_field(
            "default_stay_time_until",
            label="Default stay time until",
            initial=str(config.get("default_stay_time_until") or "11:00"),
            help_text=(
                "Fallback when no Property is set. For reservations, Property "
                "check_in_time / check_out_time take precedence."
            ),
        )

    def clean(self):
        cleaned = super().clean()
        provider = cleaned.get("provider") or self._resolved_provider()

        if provider == IntegrationConfig.Provider.CHANNEX:
            for field_name, label in (
                ("room_types_json", "Room types mapping"),
                ("booking_test_rooms_json", "Booking test rooms"),
            ):
                if field_name in self.fields:
                    raw = cleaned.get(field_name)
                    try:
                        parsed = _parse_json_field(raw or "", label)
                    except forms.ValidationError as exc:
                        raise forms.ValidationError({field_name: exc.messages}) from exc
                    if parsed is not None and not isinstance(parsed, list):
                        raise forms.ValidationError(
                            {field_name: f"{label} must be a JSON array."}
                        )

        if provider == IntegrationConfig.Provider.WHATSAPP:
            field_name = "whatsapp_templates_json"
            if field_name in self.fields:
                raw = cleaned.get(field_name)
                try:
                    parsed = _parse_json_field(raw or "", "WhatsApp templates")
                except forms.ValidationError as exc:
                    raise forms.ValidationError({field_name: exc.messages}) from exc
                if parsed is not None and not isinstance(parsed, dict):
                    raise forms.ValidationError(
                        {field_name: "WhatsApp templates must be a JSON object."}
                    )
                elif parsed is not None:
                    welcome = parsed.get("welcome")
                    inspection = inspect_welcome_map(welcome, label="welcome")
                    if inspection.errors:
                        raise forms.ValidationError(
                            {field_name: inspection.errors}
                        )
                    if inspection.warnings and self._admin_request is not None:
                        for warning in inspection.warnings:
                            messages.warning(self._admin_request, warning)
            if self.instance.pk:
                existing = self._existing_config()
                existing_phone = str(existing.get("phone_number_id") or "").strip()
                submitted_phone = str(
                    self.data.get("phone_number_id")
                    or self.cleaned_data.get("phone_number_id")
                    or ""
                ).strip()
                if existing_phone:
                    if submitted_phone and submitted_phone != existing_phone:
                        raise forms.ValidationError(
                            {
                                "phone_number_id": (
                                    "phone_number_id cannot be changed after create. "
                                    "Create a new IntegrationConfig or use "
                                    "migrate_whatsapp_phone_number."
                                )
                            }
                        )
                    self.cleaned_data["phone_number_id"] = existing_phone
            phone_number_id = str(cleaned.get("phone_number_id") or "").strip()
            if not self.instance.pk and not phone_number_id:
                raise forms.ValidationError(
                    {"phone_number_id": "phone_number_id is required for WhatsApp integrations."}
                )

        return cleaned

    def _merge_config(self) -> dict[str, Any]:
        provider = self.cleaned_data.get("provider") or self._resolved_provider()
        config = self._existing_config()

        json_field_map = {
            "room_types_json": "room_types",
            "booking_test_rooms_json": "booking_test_rooms",
            "whatsapp_templates_json": "whatsapp_templates",
        }

        for field_name in self._provider_config_fields:
            if field_name not in self.cleaned_data:
                continue
            if field_name == "skip_verify":
                continue
            value = self.cleaned_data[field_name]
            if field_name in json_field_map:
                parsed = _parse_json_field(value or "", field_name)
                if parsed is not None:
                    config[json_field_map[field_name]] = parsed
                continue
            if isinstance(self.fields[field_name], forms.BooleanField):
                config[field_name] = bool(value)
            else:
                config[field_name] = value if value is not None else ""

        for secret_name in self._provider_secret_fields:
            if secret_name not in self.cleaned_data:
                continue
            secret_value = (self.cleaned_data.get(secret_name) or "").strip()
            if secret_value:
                config[secret_name] = secret_value

        if provider == IntegrationConfig.Provider.WHATSAPP:
            phone_number_id = str(config.get("phone_number_id") or "").strip()
            if not phone_number_id and self.instance.pk:
                existing_phone = str(
                    self._existing_config().get("phone_number_id") or ""
                ).strip()
                if existing_phone:
                    phone_number_id = existing_phone
                    config["phone_number_id"] = phone_number_id
            if phone_number_id:
                self.instance.routing_key = phone_number_id
            for legacy_key in ("access_token", "provider", "api_base_url"):
                config.pop(legacy_key, None)
            if getattr(self.instance, "is_platform_default", False) or self.cleaned_data.get(
                "is_platform_default"
            ):
                self.instance.is_platform_default = bool(
                    self.cleaned_data.get("is_platform_default")
                )

        return config

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.set_config_dict(self._merge_config())
        if commit:
            instance.save()
            self.save_m2m()
        return instance
