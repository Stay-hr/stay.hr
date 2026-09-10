"""Terminal disposition for failed Channex ARI outbox rows — never a retry."""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import date, datetime, time

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.integrations.models import ChannexAriOutbox, IntegrationConfig
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

ERROR_FAMILY_WIDTH = 100


def payload_date_span(rows) -> tuple[str | None, str | None]:
    """
    Earliest and latest date any of these payloads would touch.

    Availability values come in two shapes: per-day (``date``) and compressed
    ranges (``date_from`` / ``date_to``). Reading only one shape understates the
    window, which is the whole reason these rows must not be replayed.
    """
    low = high = None
    for row in rows:
        for value in row.values or []:
            for key in ("date", "date_from", "date_to"):
                day = value.get(key)
                if not day:
                    continue
                if low is None or day < low:
                    low = day
                if high is None or day > high:
                    high = day
    return low, high


class Command(BaseCommand):
    help = (
        "Mark failed Channex ARI outbox rows as abandoned. Never retries and "
        "never contacts Channex: a failed row holds a point-in-time snapshot "
        "that may cover dates far into the future, so replaying it can undo "
        "correct availability. Dry-run by default."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant-slug",
            default="",
            help="Limit to one tenant (default: every tenant).",
        )
        parser.add_argument(
            "--created-before",
            default="",
            help=(
                "Only rows created strictly before this date (YYYY-MM-DD), read "
                "as midnight in the project timezone. Required with --apply so "
                "the cohort is explicit and a newer failure cannot be swept in."
            ),
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Perform the FAILED -> ABANDONED transition.",
        )

    def handle(self, *args, **options):
        tenant_slug = (options["tenant_slug"] or "").strip()
        if tenant_slug and not Tenant.objects.filter(slug=tenant_slug).exists():
            raise CommandError(f"No tenant with slug {tenant_slug!r}.")

        cutoff = self._parse_cutoff(options["created_before"])
        if options["apply"] and cutoff is None:
            raise CommandError(
                "--apply requires --created-before YYYY-MM-DD: the cohort must "
                "be explicit so a freshly failed row cannot be abandoned."
            )

        # Report every failed row, then mark which of them the cutoff selects.
        rows = list(
            self._failed_rows(tenant_slug=tenant_slug, cutoff=None)
            .select_related("tenant")
            .order_by("tenant__slug", "created_at", "pk")
        )
        by_tenant: dict[str, list[ChannexAriOutbox]] = defaultdict(list)
        for row in rows:
            by_tenant[row.tenant.slug].append(row)

        window = f"created before {cutoff.date()}" if cutoff else "no cutoff"
        self.stdout.write(
            f"Failed ARI outbox rows: {len(rows)} across "
            f"{len(by_tenant)} tenant(s) ({window})"
        )

        in_scope = 0
        for slug in sorted(by_tenant):
            in_scope += self._report_tenant(slug, by_tenant[slug], cutoff=cutoff)

        if not options["apply"]:
            self.stdout.write(
                f"Dry-run: would abandon {in_scope} row(s). "
                "Pass --apply together with --created-before to execute."
            )
            return

        abandoned = self._apply(tenant_slug=tenant_slug, cutoff=cutoff)
        self.stdout.write(self.style.SUCCESS(f"Abandoned {abandoned} row(s)."))

    def _report_tenant(
        self,
        slug: str,
        rows: list[ChannexAriOutbox],
        *,
        cutoff: datetime | None,
    ) -> int:
        eligible = [r for r in rows if cutoff is None or r.created_at < cutoff]
        created = [r.created_at for r in rows]
        low, high = payload_date_span(rows)
        integration_active = IntegrationConfig.objects.filter(
            tenant__slug=slug,
            provider=IntegrationConfig.Provider.CHANNEX,
            is_active=True,
        ).exists()

        kinds = ", ".join(
            f"{kind}={count}"
            for kind, count in sorted(Counter(r.kind for r in rows).items())
        )
        self.stdout.write(
            f"  {slug}: {len(rows)} row(s) [{kinds}] "
            f"created {min(created).date()}..{max(created).date()} "
            f"payload {low or '-'}..{high or '-'} "
            f"channex_integration={'active' if integration_active else 'inactive'}"
        )
        if cutoff is not None:
            self.stdout.write(
                f"    in scope: {len(eligible)}, "
                f"outside cutoff: {len(rows) - len(eligible)}"
            )
        for message, count in Counter(
            (r.error_message or "")[:ERROR_FAMILY_WIDTH] for r in rows
        ).most_common():
            self.stdout.write(f"    {count}x {message}")
        return len(eligible)

    def _apply(self, *, tenant_slug: str, cutoff: datetime) -> int:
        """
        Re-read the cohort under a row lock; the dry-run above is only a preview.

        No Channex call happens anywhere in this command: the rows are being
        retired, not sent.
        """
        with transaction.atomic():
            ids = list(
                self._failed_rows(tenant_slug=tenant_slug, cutoff=cutoff)
                # of=("self",) pins the lock to the outbox rows, so adding a
                # select_related later cannot silently widen the lock scope.
                .select_for_update(of=("self",))
                .values_list("pk", flat=True)
            )
            if not ids:
                return 0

            # auto_now does not fire through QuerySet.update(), and updated_at
            # carries the disposition timestamp.
            now = timezone.now()
            ChannexAriOutbox.objects.filter(pk__in=ids).update(
                status=ChannexAriOutbox.Status.ABANDONED,
                updated_at=now,
            )
            logger.info(
                "channex ari outbox abandoned count=%s tenant=%s",
                len(ids),
                tenant_slug or "*",
                extra={
                    "event": "channex_ari_outbox_abandoned",
                    "count": len(ids),
                    "tenant": tenant_slug or "*",
                    "created_before": cutoff.isoformat(),
                    "outbox_ids": ids,
                },
            )
        return len(ids)

    @staticmethod
    def _failed_rows(*, tenant_slug: str, cutoff: datetime | None):
        rows = ChannexAriOutbox.objects.filter(
            status=ChannexAriOutbox.Status.FAILED
        )
        if tenant_slug:
            rows = rows.filter(tenant__slug=tenant_slug)
        if cutoff is not None:
            rows = rows.filter(created_at__lt=cutoff)
        return rows

    @staticmethod
    def _parse_cutoff(raw: str) -> datetime | None:
        raw = (raw or "").strip()
        if not raw:
            return None
        try:
            day = date.fromisoformat(raw)
        except ValueError as exc:
            raise CommandError(
                f"Invalid --created-before {raw!r}; use YYYY-MM-DD."
            ) from exc
        # A naive datetime on a DateTimeField leaves the boundary undefined, so
        # the cutoff is made aware explicitly. The comparison stays exclusive.
        return timezone.make_aware(datetime.combine(day, time.min))
