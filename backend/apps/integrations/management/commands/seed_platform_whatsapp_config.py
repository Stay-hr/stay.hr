import os

from django.core.management.base import BaseCommand

from apps.integrations.models import IntegrationConfig
from apps.integrations.whatsapp.welcome_template_config import (
    default_whatsapp_templates_block,
    merge_welcome_templates_into_config,
)
from apps.tenants.constants import PLATFORM_TENANT_SLUG
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = (
        "Create or update platform default WhatsApp IntegrationConfig "
        "(Meta Cloud API). Welcome template map is merge-only."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--phone-number-id",
            default="1088787204326396",
            help="Meta phone_number_id (or WHATSAPP_PHONE_NUMBER_ID env).",
        )
        parser.add_argument(
            "--display-phone-number",
            default="+385976615439",
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
            help="Enable inbound auto-reply (true/false). Default false for platform router.",
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

        if not phone_number_id:
            self.stderr.write(
                self.style.ERROR(
                    "phone_number_id is required (arg or WHATSAPP_PHONE_NUMBER_ID env)."
                )
            )
            return

        tenant = Tenant.objects.filter(slug=PLATFORM_TENANT_SLUG, is_system=True).first()
        if tenant is None:
            self.stderr.write(
                self.style.ERROR(
                    f"Platform tenant not found (slug={PLATFORM_TENANT_SLUG}). Run migrations first."
                )
            )
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
            row = IntegrationConfig(
                tenant=tenant,
                provider=IntegrationConfig.Provider.WHATSAPP,
                property=None,
                is_active=True,
                is_platform_default=True,
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
            # Identity / routing fields update from CLI; welcome map is merge-only.
            config["phone_number_id"] = phone_number_id
            if display_phone_number:
                config["display_phone_number"] = display_phone_number
            if waba_id:
                config["waba_id"] = waba_id
            config["auto_reply"] = auto_reply
            config, added = merge_welcome_templates_into_config(config)

        row.routing_key = phone_number_id
        row.is_platform_default = True
        row.is_active = True
        row.set_config_dict(config)
        row.save()

        IntegrationConfig.objects.filter(
            provider=IntegrationConfig.Provider.WHATSAPP,
            routing_key=phone_number_id,
            is_active=True,
        ).exclude(pk=row.pk).update(is_active=False)

        verb = "Created" if created else "Updated"
        welcome_keys = sorted(
            (config.get("whatsapp_templates") or {}).get("welcome") or {}
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb} platform WhatsApp IntegrationConfig id={row.pk} "
                f"(routing_key={phone_number_id}, display={display_phone_number}). "
                f"Welcome langs={len(welcome_keys)}"
                + (f" added={','.join(added)}" if added else " (no welcome keys added)")
                + ". Token: WHATSAPP_ACCESS_TOKEN in .env (not stored in DB)."
            )
        )
