"""Retry guest e-mails recorded in the GuestImapIssue ledger. Dry-run by default."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.communications.guest_imap_issue_retry import RetryError, format_retry_report, retry_issues
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = (
        "Retry open/quarantined guest IMAP message issues. Dry-run by default; messages are stored "
        "WITHOUT live automation unless --with-automation is passed."
    )

    def add_arguments(self, parser):
        parser.add_argument("--tenant", default="uzorita")
        parser.add_argument("--uid", type=int, action="append", help="Only this UID (repeatable).")
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--with-automation", action="store_true",
                            help="Run the live automation (LLM, replies, push) for retried messages.")

    def handle(self, *args, **options):
        tenant = Tenant.objects.filter(slug=(options["tenant"] or "").strip()).select_related("reception_settings").first()
        if tenant is None:
            raise CommandError(f"Tenant not found: {options['tenant']}")
        try:
            report = retry_issues(
                tenant, apply=options["apply"], uids=options["uid"], with_automation=options["with_automation"]
            )
        except RetryError as exc:
            raise CommandError(str(exc)) from exc
        for line in format_retry_report(report):
            self.stdout.write(line)
