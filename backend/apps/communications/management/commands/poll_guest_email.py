from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.communications.guest_email_ingest import poll_tenant_guest_inbox
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Poll tenant guest IMAP inbox for Booking.com email replies."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant",
            type=str,
            default="uzorita",
            help="Tenant slug (default: uzorita).",
        )
        parser.add_argument(
            "--since-uid",
            type=int,
            default=None,
            help="Override IMAP UID cursor (fetch UID since-uid+1).",
        )
        parser.add_argument(
            "--no-notify",
            action="store_true",
            help="Skip FCM push notifications for ingested messages.",
        )

    def handle(self, *args, **options):
        slug = (options["tenant"] or "").strip()
        tenant = Tenant.objects.filter(slug=slug).select_related("reception_settings").first()
        if tenant is None:
            raise CommandError(f"Tenant not found: {slug}")

        settings = getattr(tenant, "reception_settings", None)
        if settings is None or not settings.has_guest_smtp_password:
            raise CommandError(
                f"Tenant {slug} has no guest SMTP/IMAP credentials configured."
            )

        result = poll_tenant_guest_inbox(
            tenant,
            since_uid=options["since_uid"],
            notify=not options["no_notify"],
        )
        self.stdout.write(
            f"ingested={result.ingested} skipped={result.skipped} "
            f"errors={result.errors} max_uid={result.max_uid}"
            + (" locked=1 (another poll/replay holds the tenant lock)" if result.skipped_locked else "")
            + (f" BLOCKED={result.blocked_reason} (cursor untouched; see GuestImapIssue)" if result.blocked_reason else "")
            + (f" issues_open={result.issues_open}" if result.issues_open else "")
            + (f" quarantined={result.quarantined}" if result.quarantined else "")
        )
