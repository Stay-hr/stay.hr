"""Ops: materialize TIME dispatches without (or with) outbox claim.

Default is materialize-only (planned rows → STOP) so production data can be
probed before Phase 6 dispatcher cutover::

    python manage.py run_message_scheduler --materialize-only --property-id 2

Full cycle (expire + cancel + materialize + claim → dispatching, no send)::

    python manage.py run_message_scheduler --claim
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.communications.messaging.bootstrap import bootstrap_messaging_engine
from apps.communications.messaging.models import MessageDispatch, MessageDispatchStatus
from apps.communications.messaging.scheduler import run_scheduler_cycle


class Command(BaseCommand):
    help = (
        "Run messaging scheduler cycle. Default: materialize-only "
        "(create planned MessageDispatch rows, do not claim/dispatch)."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--materialize-only",
            action="store_true",
            default=True,
            help="Expire/cancel/materialize only; leave rows planned (default).",
        )
        parser.add_argument(
            "--claim",
            action="store_true",
            help="Also claim due rows (planned/queued → dispatching). No provider send.",
        )
        parser.add_argument(
            "--tenant-id",
            type=int,
            default=None,
            help="Limit to one tenant id.",
        )
        parser.add_argument(
            "--property-id",
            type=int,
            default=None,
            help="Limit materialization to one property id.",
        )
        parser.add_argument(
            "--claim-limit",
            type=int,
            default=50,
            help="Max rows to claim when --claim is set (default 50).",
        )
        parser.add_argument(
            "--now",
            type=str,
            default=None,
            help="Inject clock as ISO datetime (for replay/probe). Uses property TZ awareness.",
        )
        parser.add_argument(
            "--show-planned",
            action="store_true",
            help="After the cycle, print planned/queued counts for the filter scope.",
        )

    def handle(self, *args, **options) -> None:
        bootstrap_messaging_engine(validate=True)

        claim = bool(options["claim"])
        # --claim wins over default materialize-only.
        if options["materialize_only"] and not claim:
            claim = False

        now = None
        raw_now = options.get("now")
        if raw_now:
            parsed = parse_datetime(raw_now)
            if parsed is None:
                raise CommandError(f"Invalid --now datetime: {raw_now!r}")
            if timezone.is_naive(parsed):
                parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
            now = parsed

        summary = run_scheduler_cycle(
            now=now,
            tenant_id=options.get("tenant_id"),
            property_id=options.get("property_id"),
            claim_limit=int(options["claim_limit"]),
            claim=claim,
        )

        mode = "claim" if claim else "materialize-only"
        self.stdout.write(
            self.style.SUCCESS(
                f"scheduler_cycle mode={mode} "
                f"expired={summary['expired']} "
                f"cancelled={summary['cancelled']} "
                f"materialized={summary['materialized']} "
                f"claimed={summary['claimed']}"
            )
        )

        if options.get("show_planned"):
            qs = MessageDispatch.objects.filter(
                status__in=(
                    MessageDispatchStatus.PLANNED,
                    MessageDispatchStatus.QUEUED,
                ),
                archived_at__isnull=True,
            )
            if options.get("tenant_id") is not None:
                qs = qs.filter(tenant_id=options["tenant_id"])
            if options.get("property_id") is not None:
                qs = qs.filter(reservation__property_id=options["property_id"])
            planned = qs.filter(status=MessageDispatchStatus.PLANNED).count()
            queued = qs.filter(status=MessageDispatchStatus.QUEUED).count()
            self.stdout.write(f"outbox planned={planned} queued={queued}")
