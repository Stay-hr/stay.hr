"""Recompute-based cleanup of property-close blocks that no longer have a reason."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.integrations.channex.ari_service import (
    get_active_channex_integration,
    push_channex_ari,
)
from apps.integrations.channex.management_mixins import ChannexWriteCommandMixin
from apps.integrations.channex.reservation_availability_service import (
    PROPERTY_CLOSE_BLOCK_REF_PREFIX,
    SYNC_AVAILABILITY_STATUSES,
    compute_unit_availability,
    multi_room_assignment_is_consistent,
    property_whole_close_unit_codes,
    push_availability_range_for_unit,
    qualifies_for_whole_property_sync,
)
from apps.integrations.models import UnitAvailabilityBlock
from apps.properties.models import Unit
from apps.reservations.models import Reservation, ReservationUnit
from apps.tenants.models import Tenant

DELETE = "delete"
KEEP = "keep"


@dataclass(frozen=True)
class BlockDecision:
    block_id: int
    unit_code: str
    check_in: date
    check_out: date
    block_ref: str
    reservation_id: int | None
    decision: str
    reason: str

    def as_line(self) -> str:
        return (
            f"  #{self.block_id} {self.block_ref} {self.unit_code} "
            f"{self.check_in}..{self.check_out} {self.decision}: {self.reason}"
        )


@dataclass(frozen=True)
class ExposedNight:
    reservation_id: int
    unit_code: str
    night: date

    def as_line(self) -> str:
        return (
            f"  WARN missing property-close: reservation #{self.reservation_id} "
            f"leaves {self.unit_code} open on {self.night}"
        )


def _nights(check_in: date, check_out: date):
    current = check_in
    while current < check_out:
        yield current
        current += timedelta(days=1)


def property_close_blocks_in_range(
    tenant: Tenant,
    *,
    from_date: date,
    to_date: date | None,
    reservation_id: int | None = None,
):
    """Checkout-exclusive overlap, so a block starting before from_date is still found."""
    blocks = UnitAvailabilityBlock.objects.filter(
        tenant=tenant,
        block_ref__startswith=PROPERTY_CLOSE_BLOCK_REF_PREFIX,
        check_out__gt=from_date,
    )
    if to_date is not None:
        blocks = blocks.filter(check_in__lt=to_date)
    if reservation_id is not None:
        blocks = blocks.filter(reservation_id=reservation_id)
    return blocks.order_by("check_in", "unit__code", "pk")


def evaluate_block(block: UnitAvailabilityBlock) -> BlockDecision:
    """Does this block still have a reason to exist under the current gate?"""
    reservation = block.reservation
    if reservation is None:
        decision, reason = DELETE, "reservation no longer exists"
    elif reservation.status not in SYNC_AVAILABILITY_STATUSES:
        decision, reason = (
            DELETE,
            f"reservation #{reservation.pk} is {reservation.status}",
        )
    elif multi_room_assignment_is_consistent(reservation):
        decision, reason = (
            DELETE,
            f"reservation #{reservation.pk} holds all {reservation.units_count} booked rooms",
        )
    else:
        decision, reason = (
            KEEP,
            f"reservation #{reservation.pk} still needs the overbooking guard",
        )
    return BlockDecision(
        block_id=block.pk,
        unit_code=block.unit.code if block.unit_id else "-",
        check_in=block.check_in,
        check_out=block.check_out,
        block_ref=block.block_ref,
        reservation_id=block.reservation_id,
        decision=decision,
        reason=reason,
    )


def find_exposed_nights(
    tenant: Tenant,
    integration,
    *,
    from_date: date,
    to_date: date | None,
) -> list[ExposedNight]:
    """
    Nights where a reservation that qualifies for property close leaves a
    competing listing genuinely sellable.

    The predicate is availability, not bookkeeping: a missing block is only
    exposure when ``compute_unit_availability`` still returns > 0. Another
    reservation or block covering the same night keeps it at 0, and that is not
    a gap.
    """
    reservations = Reservation.objects.filter(
        tenant=tenant,
        status__in=SYNC_AVAILABILITY_STATUSES,
        check_out__gt=from_date,
    ).select_related("property", "tenant")
    if to_date is not None:
        reservations = reservations.filter(check_in__lt=to_date)

    exposed: list[ExposedNight] = []
    for reservation in reservations.order_by("check_in", "pk"):
        if not qualifies_for_whole_property_sync(reservation, integration):
            continue
        close_codes = property_whole_close_unit_codes(
            integration=integration,
            property=reservation.property,
        )
        if not close_codes:
            continue
        held_unit_ids = set(
            ReservationUnit.objects.filter(
                reservation=reservation,
                unit_id__isnull=False,
            ).values_list("unit_id", flat=True)
        )
        competing = (
            Unit.objects.filter(
                tenant=tenant,
                property=reservation.property,
                code__in=close_codes,
                is_active=True,
            )
            .exclude(pk__in=held_unit_ids)
            .order_by("code")
        )
        for unit in competing:
            for night in _nights(reservation.check_in, reservation.check_out):
                if compute_unit_availability(tenant, unit, night) > 0:
                    exposed.append(
                        ExposedNight(
                            reservation_id=reservation.pk,
                            unit_code=unit.code,
                            night=night,
                        )
                    )
    return exposed


class Command(ChannexWriteCommandMixin, BaseCommand):
    help = (
        "Delete property-close blocks that no longer have a reason under the "
        "multi-room consistency rule, then recompute availability for the "
        "affected nights. Dry-run by default."
    )

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument("--tenant-slug", required=True, help="Tenant slug.")
        parser.add_argument(
            "--from-date",
            default="",
            help="Start date YYYY-MM-DD (default: today).",
        )
        parser.add_argument(
            "--to-date",
            default="",
            help=(
                "End date YYYY-MM-DD, checkout-exclusive. Omit to cover "
                "everything from --from-date onwards; a short horizon silently "
                "leaves later blocks in place."
            ),
        )
        parser.add_argument(
            "--reservation-id",
            type=int,
            default=None,
            help="Limit to blocks of a single reservation.",
        )
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--apply",
            action="store_true",
            help="Delete obsolete blocks, recompute availability and push ARI.",
        )
        mode.add_argument(
            "--check",
            action="store_true",
            help="Read-only invariant mode; exit 1 when anything is off.",
        )

    def handle(self, *args, **options):
        tenant = self._get_tenant(options["tenant_slug"])
        integration = get_active_channex_integration(tenant.slug)
        from_date = self._parse_date(options["from_date"], "--from-date") or (
            timezone.localdate()
        )
        to_date = self._parse_date(options["to_date"], "--to-date")
        if to_date is not None and to_date <= from_date:
            raise CommandError("--to-date must be after --from-date.")
        reservation_id = options["reservation_id"]

        window = f"{from_date}..{to_date or 'open-ended'}"
        self.stdout.write(f"Tenant {tenant.slug} {window}")

        decisions = [
            evaluate_block(block)
            for block in property_close_blocks_in_range(
                tenant,
                from_date=from_date,
                to_date=to_date,
                reservation_id=reservation_id,
            ).select_related("unit", "reservation")
        ]
        obsolete = [row for row in decisions if row.decision == DELETE]

        self.stdout.write(f"A) property-close blocks in range: {len(decisions)}")
        for row in decisions:
            self.stdout.write(row.as_line())

        exposed = find_exposed_nights(
            tenant,
            integration,
            from_date=from_date,
            to_date=to_date,
        )
        self.stdout.write(f"B) missing property-close nights: {len(exposed)}")
        for gap in exposed:
            self.stdout.write(self.style.WARNING(gap.as_line()))

        if options["check"]:
            self._report_check(obsolete=obsolete, exposed=exposed)
            return

        if not options["apply"]:
            self.stdout.write(
                f"Dry-run: would delete {len(obsolete)} block(s), "
                f"keep {len(decisions) - len(obsolete)}. Pass --apply to execute."
            )
            return

        if exposed:
            raise CommandError(
                f"Refusing --apply: {len(exposed)} night(s) already lack a "
                "property-close guard. Resolve section B first."
            )

        with self.channex_write_context(options):
            deleted = self._apply(
                tenant,
                from_date=from_date,
                to_date=to_date,
                reservation_id=reservation_id,
            )
            if deleted:
                # Remote write only after the DB transaction committed.
                try:
                    push_channex_ari(integration)
                except Exception as exc:
                    self._report_flush_failure(tenant, deleted=deleted, exc=exc)

        self.stdout.write(
            self.style.SUCCESS(f"Applied: deleted {len(deleted)} block(s).")
            if deleted
            else "Applied: nothing to delete."
        )

    def _apply(
        self,
        tenant: Tenant,
        *,
        from_date: date,
        to_date: date | None,
        reservation_id: int | None,
    ) -> list[BlockDecision]:
        """
        Re-evaluate under a row lock and delete, then recompute locally.

        The dry-run above is a preview, never the input for this: every block is
        read and judged again here, so a reservation created between the two runs
        cannot be overwritten by a stale decision.
        """
        deleted: list[BlockDecision] = []
        with transaction.atomic():
            blocks = list(
                property_close_blocks_in_range(
                    tenant,
                    from_date=from_date,
                    to_date=to_date,
                    reservation_id=reservation_id,
                )
                # of=("self",) is required: reservation is a nullable FK, so
                # select_related makes it an outer join and a bare
                # select_for_update fails on PostgreSQL.
                .select_for_update(of=("self",)).select_related("unit", "reservation")
            )
            recompute: list[tuple[Unit, date, date]] = []
            for block in blocks:
                decision = evaluate_block(block)
                if decision.decision != DELETE:
                    continue
                deleted.append(decision)
                if block.unit_id:
                    recompute.append((block.unit, block.check_in, block.check_out))

            if not deleted:
                return deleted

            UnitAvailabilityBlock.objects.filter(
                pk__in=[row.block_id for row in deleted]
            ).delete()

            for row in deleted:
                self.stdout.write(f"  deleted #{row.block_id} {row.reason}")

            # Availability is always recomputed from compute_unit_availability,
            # never assumed to be 1 because a block went away.
            for unit, check_in, check_out in recompute:
                push_availability_range_for_unit(tenant, unit, check_in, check_out)
        return deleted

    def _report_flush_failure(
        self,
        tenant: Tenant,
        *,
        deleted: list[BlockDecision],
        exc: Exception,
    ) -> None:
        """
        The delete already committed, so the cleanup itself must not be redone.

        Only the remote push is missing. The affected outbox row is now FAILED and
        a plain flush selects PENDING rows only, so re-running the flush would not
        pick it up either — a full sync is the recovery path.
        """
        self.stderr.write(
            self.style.ERROR(
                f"Deleted {len(deleted)} block(s) and committed, but the Channex "
                f"push failed: {type(exc).__name__}: {exc}"
            )
        )
        self.stderr.write(
            "Do NOT re-run this command — the blocks are already gone and the DB "
            "state is correct (verify with --check). Repeat only the ARI push:\n"
            f"  python manage.py channex_ari_full_sync --tenant-slug {tenant.slug}\n"
            f"  python manage.py verify_channex_availability --tenant-slug {tenant.slug}"
        )
        raise SystemExit(1) from exc

    def _report_check(
        self,
        *,
        obsolete: list[BlockDecision],
        exposed: list[ExposedNight],
    ) -> None:
        if obsolete or exposed:
            self.stderr.write(
                self.style.ERROR(
                    f"Invariant broken: {len(obsolete)} obsolete block(s), "
                    f"{len(exposed)} unguarded night(s)."
                )
            )
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("Invariant holds."))

    @staticmethod
    def _get_tenant(slug: str) -> Tenant:
        tenant = Tenant.objects.filter(slug=slug).first()
        if tenant is None:
            raise CommandError(f"No tenant with slug {slug!r}.")
        return tenant

    @staticmethod
    def _parse_date(raw: str, flag: str) -> date | None:
        if not raw:
            return None
        try:
            return date.fromisoformat(raw.strip())
        except ValueError as exc:
            raise CommandError(f"Invalid {flag} {raw!r}; use YYYY-MM-DD.") from exc
