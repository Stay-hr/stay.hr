from datetime import time

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.properties.models import AfterHoursArrivalPolicy, Property, SelfServiceMode
from apps.properties.guest_info import normalize_guest_info
from apps.properties.uzorita_guest_info import UZORITA_GUEST_INFO
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Seed Property.guest_info for Uzorita (localized WhatsApp/check-in texts and LLM facts)."

    def add_arguments(self, parser):
        parser.add_argument("--tenant-slug", default="uzorita")
        parser.add_argument("--property-slug", default="uzorita")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print normalized guest_info without saving.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        tenant = Tenant.objects.filter(slug=options["tenant_slug"]).first()
        if tenant is None:
            self.stderr.write(self.style.ERROR(f"Tenant not found: {options['tenant_slug']}"))
            return

        prop = Property.objects.filter(tenant=tenant, slug=options["property_slug"]).first()
        if prop is None:
            self.stderr.write(
                self.style.ERROR(
                    f"Property not found: {options['property_slug']} "
                    f"(tenant {options['tenant_slug']})"
                )
            )
            return

        normalized = normalize_guest_info(UZORITA_GUEST_INFO)
        text_keys = sorted((normalized.get("texts") or {}).keys())
        guide_steps = len((normalized.get("guide") or {}).get("steps") or [])

        if options["dry_run"]:
            self.stdout.write(
                self.style.WARNING(
                    f"Dry run — would set guest_info on {prop.name} "
                    f"({len(text_keys)} text keys, maps={normalized['links']['maps_url']}, "
                    f"guide_steps={guide_steps}, self_service_mode=schedule weekdays=[1])"
                )
            )
            return

        prop.guest_info = normalized
        prop.check_in_latest_time = time(22, 0)
        prop.after_hours_arrival_policy = AfterHoursArrivalPolicy.CONTACT
        prop.after_hours_contact_phone = "+385998388513"
        prop.guest_arrival_auto_reply_enabled = True
        prop.guest_parking_auto_reply_enabled = True
        prop.guest_invoice_auto_reply_enabled = True
        # PR-C: key guide visible on Tuesdays only (Python weekday 1).
        prop.self_service_mode = SelfServiceMode.SCHEDULE
        prop.self_service_config = {"weekdays": [1]}
        contact = prop.contact if isinstance(prop.contact, dict) else {}
        contact = {
            **contact,
            "phone": contact.get("phone") or "+385998388513",
            "whatsapp": contact.get("whatsapp") or "+385998388513",
        }
        prop.contact = contact
        prop.save(
            update_fields=[
                "guest_info",
                "contact",
                "check_in_latest_time",
                "after_hours_arrival_policy",
                "after_hours_contact_phone",
                "guest_arrival_auto_reply_enabled",
                "guest_parking_auto_reply_enabled",
                "guest_invoice_auto_reply_enabled",
                "self_service_mode",
                "self_service_config",
                "updated_at",
            ]
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"Updated guest_info for property {prop.slug} "
                f"(tenant {tenant.slug}, {len(text_keys)} text keys, "
                f"{guide_steps} guide steps, self_service_mode=schedule weekdays=[1])."
            )
        )
