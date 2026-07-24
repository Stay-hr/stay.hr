"""Align property messaging schedules for Messaging Engine cutover (ADR 0010 Phase 7).

Sets WhatsApp welcome clocks to the platform cutover target (0d @ 11:15 FIXED_TIME)
and clears pre-arrival property overrides so platform 7d @ 09:00 FIXED_TIME applies.
"""

from __future__ import annotations

from datetime import time

from django.core.management.base import BaseCommand

from apps.communications.messaging.models import MessageScheduleStrategy
from apps.properties.models import Property
from apps.tenants.models import Tenant

# Cutover targets (match PLATFORM_SCHEDULE_DEFAULTS).
WELCOME_SEND_TIME = time(11, 15)
WELCOME_DAYS_BEFORE = 0
WELCOME_STRATEGY = MessageScheduleStrategy.FIXED_TIME


class Command(BaseCommand):
    help = (
        "Align messaging schedule fields for cutover (default: tenant uzorita). "
        "Sets whatsapp_autocheckin_time + whatsapp_welcome_* to 0d @ 11:15 FIXED_TIME; "
        "clears pre_arrival_* property overrides (inherit platform 7d @ 09:00)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant-slug",
            default="uzorita",
            help="Tenant to align (default: uzorita).",
        )
        parser.add_argument(
            "--property-slug",
            default="",
            help="Optional property slug filter (default: all properties for tenant).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show planned changes without saving.",
        )

    def handle(self, *args, **options):
        from apps.communications.messaging.schedule_settings import resolve_schedule

        tenant_slug = (options["tenant_slug"] or "").strip()
        property_slug = (options["property_slug"] or "").strip()
        dry_run = bool(options["dry_run"])

        tenant = Tenant.objects.filter(slug=tenant_slug).first()
        if tenant is None:
            self.stderr.write(self.style.ERROR(f"Tenant not found: {tenant_slug}"))
            return

        props = Property.objects.filter(tenant=tenant).order_by("pk")
        if property_slug:
            props = props.filter(slug=property_slug)
            if not props.exists():
                self.stderr.write(
                    self.style.ERROR(
                        f"Property not found: {property_slug} (tenant={tenant_slug})"
                    )
                )
                return

        updated = 0
        for prop in props:
            before_pre = resolve_schedule(property=prop, prefixes=("pre_arrival",))
            before_wel = resolve_schedule(
                property=prop,
                prefixes=("whatsapp_welcome", "welcome"),
            )
            self.stdout.write(
                f"{prop.slug} (id={prop.pk}) before: "
                f"pre={before_pre.days_before}d@{before_pre.send_time} "
                f"{before_pre.schedule_strategy}; "
                f"welcome={before_wel.days_before}d@{before_wel.send_time} "
                f"{before_wel.schedule_strategy}"
            )

            prop.whatsapp_autocheckin_time = WELCOME_SEND_TIME
            prop.whatsapp_welcome_days_before = WELCOME_DAYS_BEFORE
            prop.whatsapp_welcome_send_time = WELCOME_SEND_TIME
            prop.whatsapp_welcome_schedule_strategy = WELCOME_STRATEGY
            # Inherit platform pre-arrival (7d @ 09:00 FIXED_TIME).
            prop.pre_arrival_days_before = None
            prop.pre_arrival_send_time = None
            prop.pre_arrival_schedule_strategy = ""

            if dry_run:
                after_pre = resolve_schedule(property=prop, prefixes=("pre_arrival",))
                after_wel = resolve_schedule(
                    property=prop,
                    prefixes=("whatsapp_welcome", "welcome"),
                )
                self.stdout.write(
                    self.style.WARNING(
                        f"  [dry-run] would become: "
                        f"pre={after_pre.days_before}d@{after_pre.send_time} "
                        f"{after_pre.schedule_strategy}; "
                        f"welcome={after_wel.days_before}d@{after_wel.send_time} "
                        f"{after_wel.schedule_strategy}"
                    )
                )
                updated += 1
                continue

            prop.save(
                update_fields=[
                    "whatsapp_autocheckin_time",
                    "whatsapp_welcome_days_before",
                    "whatsapp_welcome_send_time",
                    "whatsapp_welcome_schedule_strategy",
                    "pre_arrival_days_before",
                    "pre_arrival_send_time",
                    "pre_arrival_schedule_strategy",
                    "updated_at",
                ]
            )
            prop.refresh_from_db()
            after_pre = resolve_schedule(property=prop, prefixes=("pre_arrival",))
            after_wel = resolve_schedule(
                property=prop,
                prefixes=("whatsapp_welcome", "welcome"),
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"  after: pre={after_pre.days_before}d@{after_pre.send_time} "
                    f"{after_pre.schedule_strategy}; "
                    f"welcome={after_wel.days_before}d@{after_wel.send_time} "
                    f"{after_wel.schedule_strategy}"
                )
            )
            updated += 1

        verb = "Would update" if dry_run else "Updated"
        self.stdout.write(self.style.SUCCESS(f"{verb} {updated} propert(y/ies)."))
