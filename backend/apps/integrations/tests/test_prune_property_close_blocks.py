from datetime import date
from io import StringIO
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.db import connection
from django.test import TestCase

from apps.integrations.channex.reservation_availability_service import (
    PROPERTY_CLOSE_BLOCK_REF_PREFIX,
    UZORITA_WHOLE_PROPERTY_UNIT_CODES,
    compute_unit_availability,
)
from apps.integrations.management.commands.prune_property_close_blocks import (
    property_close_blocks_in_range,
)
from apps.integrations.models import IntegrationConfig, UnitAvailabilityBlock
from apps.properties.models import Property, Unit
from apps.reservations.models import Reservation, ReservationUnit
from apps.tenants.models import ChannelManager, Tenant, TenantReceptionSettings

APPLY_TARGET = (
    "apps.integrations.channex.reservation_availability_service.apply_availability_updates"
)
PUSH_TARGET = (
    "apps.integrations.management.commands.prune_property_close_blocks.push_channex_ari"
)


class PrunePropertyCloseBlocksTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="uzorita", name="Uzorita")
        TenantReceptionSettings.objects.create(
            tenant=self.tenant,
            channel_manager=ChannelManager.CHANNEX,
        )
        self.property = Property.objects.create(
            tenant=self.tenant,
            slug="uzorita",
            name="Uzorita",
            timezone="Europe/Zagreb",
        )
        self.units = {
            code: Unit.objects.create(
                tenant=self.tenant,
                property=self.property,
                code=code,
                name=code,
            )
            for code in sorted(UZORITA_WHOLE_PROPERTY_UNIT_CODES)
        }
        self.integration = IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.CHANNEX,
            is_active=True,
        )
        self.integration.set_config_dict(
            {
                "property_id": "prop-uzorita",
                "room_types": [
                    {"unit_code": code, "channex_room_type_id": f"rt-{code}"}
                    for code in sorted(UZORITA_WHOLE_PROPERTY_UNIT_CODES)
                ],
            }
        )
        self.integration.save()

    # --- fixtures -----------------------------------------------------------

    def _reservation(
        self,
        *,
        check_in: date,
        check_out: date,
        units_count,
        held_codes,
        status=Reservation.Status.EXPECTED,
    ) -> Reservation:
        reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            check_in=check_in,
            check_out=check_out,
            status=status,
            booker_name="Guest",
            units_count=units_count,
        )
        for sort_order, code in enumerate(held_codes):
            ReservationUnit.objects.create(
                tenant=self.tenant,
                reservation=reservation,
                unit=self.units[code],
                room_name=code,
                sort_order=sort_order,
            )
        return reservation

    def _close_block(
        self,
        reservation: Reservation,
        code: str,
        *,
        check_in: date | None = None,
        check_out: date | None = None,
    ) -> UnitAvailabilityBlock:
        unit = self.units[code]
        return UnitAvailabilityBlock.objects.create(
            tenant=self.tenant,
            unit=unit,
            reservation=reservation,
            check_in=check_in or reservation.check_in,
            check_out=check_out or reservation.check_out,
            block_ref=f"{PROPERTY_CLOSE_BLOCK_REF_PREFIX}{reservation.pk}:{unit.pk}",
            created_via=UnitAvailabilityBlock.CreatedVia.STAY,
        )

    def _consistent_stay_with_obsolete_blocks(self):
        """Correctly mapped 2-room stay whose competing listings are closed for nothing."""
        reservation = self._reservation(
            check_in=date(2026, 9, 10),
            check_out=date(2026, 9, 12),
            units_count=2,
            held_codes=("R1", "R3"),
        )
        return reservation, [
            self._close_block(reservation, "R2"),
            self._close_block(reservation, "R6"),
        ]

    def _suspect_stay_with_guarding_blocks(self):
        """Booking claims 2 rooms but only one is mapped — the guard must stay."""
        reservation = self._reservation(
            check_in=date(2026, 9, 20),
            check_out=date(2026, 9, 21),
            units_count=2,
            held_codes=("R1",),
        )
        return reservation, [
            self._close_block(reservation, code) for code in ("R2", "R3", "R6")
        ]

    def _run(self, *args, **options):
        out = StringIO()
        call_command(
            "prune_property_close_blocks",
            "--tenant-slug",
            "uzorita",
            "--from-date",
            "2026-09-01",
            *args,
            stdout=out,
            stderr=StringIO(),
            **options,
        )
        return out.getvalue()

    # --- dry-run ------------------------------------------------------------

    @patch(PUSH_TARGET)
    @patch(APPLY_TARGET)
    def test_dry_run_changes_nothing(self, mock_apply, mock_push):
        _, obsolete = self._consistent_stay_with_obsolete_blocks()
        self._suspect_stay_with_guarding_blocks()
        before = set(UnitAvailabilityBlock.objects.values_list("pk", flat=True))

        output = self._run()

        self.assertIn("would delete 2 block(s), keep 3", output)
        self.assertEqual(
            set(UnitAvailabilityBlock.objects.values_list("pk", flat=True)), before
        )
        mock_apply.assert_not_called()
        mock_push.assert_not_called()
        for block in obsolete:
            self.assertTrue(UnitAvailabilityBlock.objects.filter(pk=block.pk).exists())

    def test_range_overlap_is_checkout_exclusive(self):
        crossing_stay = self._reservation(
            check_in=date(2026, 9, 8),
            check_out=date(2026, 9, 12),
            units_count=2,
            held_codes=("R1", "R3"),
        )
        crossing = self._close_block(crossing_stay, "R2")
        earlier_stay = self._reservation(
            check_in=date(2026, 9, 5),
            check_out=date(2026, 9, 9),
            units_count=2,
            held_codes=("R1", "R3"),
        )
        ends_on_from_date = self._close_block(earlier_stay, "R6")

        found = [
            row.pk
            for row in property_close_blocks_in_range(
                self.tenant,
                from_date=date(2026, 9, 9),
                to_date=date(2026, 9, 10),
            )
        ]

        self.assertIn(crossing.pk, found)
        self.assertNotIn(ends_on_from_date.pk, found)

    # --- apply --------------------------------------------------------------

    @patch(PUSH_TARGET)
    @patch(APPLY_TARGET)
    def test_apply_deletes_obsolete_and_keeps_the_guard(self, mock_apply, mock_push):
        mock_apply.return_value = []
        mock_push.return_value = []
        _, obsolete = self._consistent_stay_with_obsolete_blocks()
        _, guarding = self._suspect_stay_with_guarding_blocks()

        output = self._run("--apply")

        self.assertIn("deleted 2 block(s)", output)
        self.assertFalse(
            UnitAvailabilityBlock.objects.filter(
                pk__in=[b.pk for b in obsolete]
            ).exists()
        )
        self.assertEqual(
            UnitAvailabilityBlock.objects.filter(
                pk__in=[b.pk for b in guarding]
            ).count(),
            3,
        )
        mock_push.assert_called_once()

    @patch(PUSH_TARGET)
    @patch(APPLY_TARGET)
    def test_apply_recomputes_zero_when_another_stay_holds_the_night(
        self, mock_apply, mock_push
    ):
        """Deleting a block must never imply the night became sellable."""
        mock_apply.return_value = []
        mock_push.return_value = []
        consistent = self._reservation(
            check_in=date(2026, 10, 1),
            check_out=date(2026, 10, 2),
            units_count=2,
            held_codes=("R1", "R2"),
        )
        self._close_block(consistent, "R3")
        self._reservation(
            check_in=date(2026, 10, 1),
            check_out=date(2026, 10, 2),
            units_count=1,
            held_codes=("R3",),
        )

        self._run("--apply")

        pushed = [
            update
            for call in mock_apply.call_args_list
            for update in call.args[1]
            if update["unit_code"] == "R3"
        ]
        self.assertEqual(
            pushed,
            [{"unit_code": "R3", "date": "2026-10-01", "availability": 0}],
        )
        self.assertEqual(
            compute_unit_availability(self.tenant, self.units["R3"], date(2026, 10, 1)),
            0,
        )

    @patch(PUSH_TARGET)
    @patch(APPLY_TARGET)
    def test_apply_refuses_while_a_night_is_unguarded(self, mock_apply, mock_push):
        _, obsolete = self._consistent_stay_with_obsolete_blocks()
        # Suspect stay with no blocks at all — R2, R3 and R6 are really sellable.
        self._reservation(
            check_in=date(2026, 9, 20),
            check_out=date(2026, 9, 21),
            units_count=2,
            held_codes=("R1",),
        )

        with self.assertRaises(CommandError) as ctx:
            self._run("--apply")

        self.assertIn("Refusing --apply", str(ctx.exception))
        self.assertEqual(
            UnitAvailabilityBlock.objects.filter(
                pk__in=[b.pk for b in obsolete]
            ).count(),
            2,
        )
        mock_apply.assert_not_called()
        mock_push.assert_not_called()

    @patch(PUSH_TARGET)
    @patch(APPLY_TARGET)
    def test_apply_deletes_block_of_cancelled_reservation(self, mock_apply, mock_push):
        mock_apply.return_value = []
        mock_push.return_value = []
        cancelled = self._reservation(
            check_in=date(2026, 11, 1),
            check_out=date(2026, 11, 2),
            units_count=2,
            held_codes=("R1",),
            status=Reservation.Status.CANCELED,
        )
        block = self._close_block(cancelled, "R2")

        self._run("--apply")

        self.assertFalse(UnitAvailabilityBlock.objects.filter(pk=block.pk).exists())

    # --- commit boundary ----------------------------------------------------

    @patch(PUSH_TARGET)
    @patch(APPLY_TARGET)
    def test_remote_push_runs_once_after_the_transaction_closes(
        self, mock_apply, mock_push
    ):
        self._consistent_stay_with_obsolete_blocks()
        depths: dict[str, list[int]] = {"local": [], "remote": []}

        def _local(*args, **kwargs):
            depths["local"].append(len(connection.savepoint_ids))
            return []

        def _remote(*args, **kwargs):
            depths["remote"].append(len(connection.savepoint_ids))
            return []

        mock_apply.side_effect = _local
        mock_push.side_effect = _remote

        self._run("--apply")

        self.assertEqual(len(depths["remote"]), 1)
        # The local recompute runs one savepoint deeper than the remote flush,
        # which proves push_channex_ari happens outside the command's atomic block.
        self.assertTrue(
            all(local > depths["remote"][0] for local in depths["local"]),
            f"expected remote push outside the transaction, got {depths}",
        )

    @patch(PUSH_TARGET)
    @patch(APPLY_TARGET)
    def test_failed_recompute_rolls_back_and_skips_the_remote_push(
        self, mock_apply, mock_push
    ):
        _, obsolete = self._consistent_stay_with_obsolete_blocks()
        mock_apply.side_effect = RuntimeError("channex local write failed")

        with self.assertRaises(RuntimeError):
            self._run("--apply")

        self.assertEqual(
            UnitAvailabilityBlock.objects.filter(
                pk__in=[b.pk for b in obsolete]
            ).count(),
            2,
        )
        mock_push.assert_not_called()

    @patch(PUSH_TARGET)
    @patch(APPLY_TARGET)
    def test_failed_remote_push_keeps_the_commit_and_tells_operator_not_to_rerun(
        self, mock_apply, mock_push
    ):
        mock_apply.return_value = []
        mock_push.side_effect = RuntimeError("Channex 429")
        _, obsolete = self._consistent_stay_with_obsolete_blocks()
        errors = StringIO()

        with self.assertRaises(SystemExit) as ctx:
            call_command(
                "prune_property_close_blocks",
                "--tenant-slug",
                "uzorita",
                "--from-date",
                "2026-09-01",
                "--apply",
                stdout=StringIO(),
                stderr=errors,
            )

        self.assertEqual(ctx.exception.code, 1)
        # Delete stays committed; only the remote push has to be repeated.
        self.assertFalse(
            UnitAvailabilityBlock.objects.filter(
                pk__in=[b.pk for b in obsolete]
            ).exists()
        )
        message = errors.getvalue()
        self.assertIn("Do NOT re-run this command", message)
        self.assertIn("channex_ari_full_sync --tenant-slug uzorita", message)

    # --- check mode ---------------------------------------------------------

    @patch(PUSH_TARGET)
    @patch(APPLY_TARGET)
    def test_check_exits_one_on_obsolete_block(self, mock_apply, mock_push):
        self._consistent_stay_with_obsolete_blocks()

        with self.assertRaises(SystemExit) as ctx:
            self._run("--check")

        self.assertEqual(ctx.exception.code, 1)
        mock_apply.assert_not_called()
        mock_push.assert_not_called()

    @patch(PUSH_TARGET)
    @patch(APPLY_TARGET)
    def test_check_passes_after_cleanup(self, mock_apply, mock_push):
        mock_apply.return_value = []
        mock_push.return_value = []
        self._consistent_stay_with_obsolete_blocks()
        self._suspect_stay_with_guarding_blocks()

        self._run("--apply")
        output = self._run("--check")

        self.assertIn("Invariant holds.", output)

    def test_check_exits_one_on_unguarded_night(self):
        self._reservation(
            check_in=date(2026, 9, 20),
            check_out=date(2026, 9, 21),
            units_count=2,
            held_codes=("R1",),
        )

        with self.assertRaises(SystemExit) as ctx:
            self._run("--check")

        self.assertEqual(ctx.exception.code, 1)

    # --- argument handling --------------------------------------------------

    def test_unknown_tenant_is_rejected(self):
        with self.assertRaises(CommandError):
            call_command(
                "prune_property_close_blocks",
                "--tenant-slug",
                "nope",
                stdout=StringIO(),
            )

    def test_to_date_must_follow_from_date(self):
        with self.assertRaises(CommandError):
            self._run("--to-date", "2026-08-01")
