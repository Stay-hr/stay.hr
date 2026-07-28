import os

from django.core.management.base import BaseCommand

from apps.integrations.models import IntegrationConfig
from apps.integrations.whatsapp.welcome_template_config import (
    default_whatsapp_templates_block,
    merge_welcome_templates_into_config,
)
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = (
        "Create or update WhatsApp IntegrationConfig for a hotel tenant "
        "(Meta WhatsApp Cloud API only). Welcome template map is merge-only."
    )

    def add_arguments(self, parser):
        parser.add_argument("--tenant-slug", default="uzorita")
        parser.add_argument(
            "--phone-number-id",
            default="",
            help="Meta phone_number_id (or WHATSAPP_PHONE_NUMBER_ID env).",
        )
        parser.add_argument(
            "--display-phone-number",
            default="",
            help="Display number e.g. +385... (or WHATSAPP_DISPLAY_PHONE_NUMBER env).",
        )
        parser.add_argument(
            "--waba-id",
            default="",
            help="WhatsApp Business Account ID (or WHATSAPP_WABA_ID env).",
        )
        parser.add_argument(
            "--auto-reply",
            default="false",
            help="Enable inbound auto-reply (true/false). Default false for stateful autocheck-in flow.",
        )

    def handle(self, *args, **options):
        phone_number_id = (
            options["phone_number_id"] or os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
        ).strip()
        display_phone_number = (
            options["display_phone_number"]
            or os.getenv("WHATSAPP_DISPLAY_PHONE_NUMBER", "")
        ).strip()
        waba_id = (options["waba_id"] or os.getenv("WHATSAPP_WABA_ID", "")).strip()

        tenant = Tenant.objects.filter(slug=options["tenant_slug"]).first()
        if tenant is None:
            self.stderr.write(self.style.ERROR(f"Tenant not found: {options['tenant_slug']}"))
            return

        auto_reply_raw = str(options["auto_reply"] or "false").strip().lower()
        auto_reply = auto_reply_raw not in ("0", "false", "no", "off")

        row = IntegrationConfig.objects.filter(
            tenant=tenant,
            provider=IntegrationConfig.Provider.WHATSAPP,
            property=None,
        ).first()
        created = row is None

        if created:
            if not phone_number_id:
                self.stderr.write(
                    self.style.ERROR(
                        "phone_number_id is required for create.\n"
                        "  export WHATSAPP_PHONE_NUMBER_ID='...'\n"
                        "  docker compose exec django python manage.py seed_uzorita_whatsapp_config"
                    )
                )
                return
            row = IntegrationConfig(
                tenant=tenant,
                provider=IntegrationConfig.Provider.WHATSAPP,
                property=None,
                is_active=True,
                routing_key=phone_number_id,
            )
            config: dict = {
                "phone_number_id": phone_number_id,
                "display_phone_number": display_phone_number,
                "waba_id": waba_id,
                "auto_reply": auto_reply,
                "whatsapp_templates": default_whatsapp_templates_block(),
            }
            added = sorted(config["whatsapp_templates"]["welcome"].keys())
        else:
            config = dict(row.get_config_dict())
            if phone_number_id:
                config["phone_number_id"] = phone_number_id
                row.routing_key = phone_number_id
            elif not str(config.get("phone_number_id") or "").strip():
                self.stderr.write(
                    self.style.ERROR(
                        "Existing row has no phone_number_id; pass --phone-number-id."
                    )
                )
                return
            if display_phone_number:
                config["display_phone_number"] = display_phone_number
            if waba_id:
                config["waba_id"] = waba_id
            # Only force auto_reply when explicitly provided via CLI default path —
            # keep applying the resolved flag (same as before) but merge welcome.
            config["auto_reply"] = auto_reply
            config, added = merge_welcome_templates_into_config(config)
            if not row.routing_key:
                row.routing_key = str(config.get("phone_number_id") or "")

        row.is_active = True
        row.set_config_dict(config)
        row.save()

        verb = "Created" if created else "Updated"
        welcome_keys = sorted(
            (config.get("whatsapp_templates") or {}).get("welcome") or {}
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb} WhatsApp IntegrationConfig id={row.pk} "
                f"(tenant={tenant.slug}, routing_key={row.routing_key}). "
                f"Welcome langs={len(welcome_keys)}"
                + (f" added={','.join(added)}" if added else " (no welcome keys added)")
                + ". Access token: WHATSAPP_ACCESS_TOKEN in .env."
            )
        )
