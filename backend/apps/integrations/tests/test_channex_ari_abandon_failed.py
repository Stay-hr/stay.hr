from datetime import datetime, timezone as dt_timezone
from io import StringIO
from unittest.mock import MagicMock, patch

from django.core.management import CommandError, call_command
from django.test import TestCase

from apps.integrations.models import ChannexAriOutbox, IntegrationConfig
from apps.properties.models import Property
from apps.tenants.models import Tenant

CUTOFF = "2026-07-01"


class ChannexAriAbandonFailedTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="uzorita", name="Uzorita")
        self.property = Property.objects.create(
            tenant=self.tenant,
            slug="uzorita",
            name="Uzorita",
            timezone="Europe/Zagreb",
        )
        self.integration = IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.CHANNEX,
            is_active=True,
        )
        self.integration.set_config_dict(
            {
                "property_id": "prop-uzorita",
                "api_key": "key",
                "sync_property_slug": "uzorita",
            }
        )
        self.integration.save()

    # --- fixtures -----------------------------------------------------------

    def _outbox(
        self,
        *,
        status,
        created_at: datetime,
        tenant: Tenant | None = None,
        property: Property | None = None,
        kind=ChannexAriOutbox.Kind.AVAILABILITY,
        values=None,
        error_message="",
        task_ids=None,
    ) -> ChannexAriOutbox:
        row = ChannexAriOutbox.objects.create(
            tenant=tenant or self.tenant,
            property=property or self.property,
            kind=kind,
            status=status,
            values=values if values is not None else [{"date": "2026-12-20"}],
            error_message=error_message,
            channex_task_ids=task_ids or [],
        )
        # created_at is auto_now_add, so the cohort date has to be forced.
        ChannexAriOutbox.objects.filter(pk=row.pk).update(created_at=created_at)
        row.refresh_from_db()
        return row

    def _failed_in_cohort(self, **kwargs) -> ChannexAriOutbox:
        return self._outbox(
            status=ChannexAriOutbox.Status.FAILED,
            created_at=datetime(2026, 6, 30, 21, 0, tzinfo=dt_timezone.utc),
            error_message='Channex POST /availability failed (429): too_many_requests',
            **kwargs,
        )

    def _run(self, *args):
        out = StringIO()
        call_command(
            "channex_ari_abandon_failed", *args, stdout=out, stderr=StringIO()
        )
        return out.getvalue()

    def _status_of(self, row: ChannexAriOutbox) -> str:
        return ChannexAriOutbox.objects.get(pk=row.pk).status

    # --- dry-run ------------------------------------------------------------

    def test_dry_run_changes_nothing(self):
        row = self._failed_in_cohort()

        output = self._run()

        self.assertIn("would abandon 1 row(s)", output)
        self.assertEqual(self._status_of(row), ChannexAriOutbox.Status.FAILED)

    def test_dry_run_reports_payload_span_across_both_value_shapes(self):
        self._failed_in_cohort(
            values=[
                {"date": "2026-12-20", "availability": 1},
                {"date_from": "2027-10-01", "date_to": "2027-10-17", "availability": 0},
            ]
        )

        output = self._run()

        self.assertIn("payload 2026-12-20..2027-10-17", output)
        self.assertIn("channex_integration=active", output)

    def test_dry_run_separates_rows_outside_the_cutoff(self):
        self._failed_in_cohort()
        self._outbox(
            status=ChannexAriOutbox.Status.FAILED,
            created_at=datetime(2026, 9, 1, 8, 0, tzinfo=dt_timezone.utc),
        )

        output = self._run("--created-before", CUTOFF)

        self.assertIn("in scope: 1, outside cutoff: 1", output)
        self.assertIn("would abandon 1 row(s)", output)

    # --- apply guard --------------------------------------------------------

    def test_apply_without_cutoff_is_refused(self):
        row = self._failed_in_cohort()

        with self.assertRaises(CommandError) as ctx:
            self._run("--apply")

        self.assertIn("--created-before", str(ctx.exception))
        self.assertEqual(self._status_of(row), ChannexAriOutbox.Status.FAILED)

    def test_invalid_cutoff_is_refused(self):
        with self.assertRaises(CommandError):
            self._run("--created-before", "01.07.2026.")

    def test_unknown_tenant_is_refused(self):
        with self.assertRaises(CommandError):
            self._run("--tenant-slug", "nope")

    # --- apply --------------------------------------------------------------

    def test_apply_abandons_the_cohort_and_leaves_pending_and_sent(self):
        failed = self._failed_in_cohort()
        pending = self._outbox(
            status=ChannexAriOutbox.Status.PENDING,
            created_at=datetime(2026, 6, 10, 8, 0, tzinfo=dt_timezone.utc),
        )
        sent = self._outbox(
            status=ChannexAriOutbox.Status.SENT,
            created_at=datetime(2026, 6, 11, 8, 0, tzinfo=dt_timezone.utc),
        )

        output = self._run("--created-before", CUTOFF, "--apply")

        self.assertIn("Abandoned 1 row(s)", output)
        self.assertEqual(self._status_of(failed), ChannexAriOutbox.Status.ABANDONED)
        self.assertEqual(self._status_of(pending), ChannexAriOutbox.Status.PENDING)
        self.assertEqual(self._status_of(sent), ChannexAriOutbox.Status.SENT)

    def test_row_created_on_the_cutoff_day_is_left_alone(self):
        """The boundary is exclusive: midnight on the cutoff day is out of scope."""
        on_cutoff = self._outbox(
            status=ChannexAriOutbox.Status.FAILED,
            created_at=datetime(2026, 7, 1, 0, 0, tzinfo=dt_timezone.utc),
        )
        just_before = self._outbox(
            status=ChannexAriOutbox.Status.FAILED,
            created_at=datetime(2026, 6, 30, 23, 59, 59, tzinfo=dt_timezone.utc),
        )

        self._run("--created-before", CUTOFF, "--apply")

        self.assertEqual(self._status_of(on_cutoff), ChannexAriOutbox.Status.FAILED)
        self.assertEqual(
            self._status_of(just_before), ChannexAriOutbox.Status.ABANDONED
        )

    def test_apply_moves_updated_at_and_keeps_the_failure_record(self):
        row = self._failed_in_cohort(task_ids=["task-abc"])
        ChannexAriOutbox.objects.filter(pk=row.pk).update(
            updated_at=datetime(2026, 6, 30, 21, 0, tzinfo=dt_timezone.utc)
        )
        before = ChannexAriOutbox.objects.get(pk=row.pk)

        self._run("--created-before", CUTOFF, "--apply")

        after = ChannexAriOutbox.objects.get(pk=row.pk)
        # QuerySet.update() skips auto_now, so the command must set it itself.
        self.assertGreater(after.updated_at, before.updated_at)
        self.assertEqual(after.error_message, before.error_message)
        self.assertEqual(after.channex_task_ids, ["task-abc"])
        self.assertEqual(after.values, before.values)

    def test_tenant_scope_does_not_touch_another_tenant(self):
        other_tenant = Tenant.objects.create(slug="other", name="Other")
        other_property = Property.objects.create(
            tenant=other_tenant,
            slug="other",
            name="Other",
            timezone="Europe/Zagreb",
        )
        mine = self._failed_in_cohort()
        theirs = self._failed_in_cohort(
            tenant=other_tenant, property=other_property
        )

        self._run("--tenant-slug", "uzorita", "--created-before", CUTOFF, "--apply")

        self.assertEqual(self._status_of(mine), ChannexAriOutbox.Status.ABANDONED)
        self.assertEqual(self._status_of(theirs), ChannexAriOutbox.Status.FAILED)

    def test_works_for_tenant_whose_integration_is_inactive(self):
        """Decommissioned cert tenants can never flush, so they must be reachable."""
        self.integration.is_active = False
        self.integration.save(update_fields=["is_active"])
        row = self._failed_in_cohort()

        output = self._run("--created-before", CUTOFF, "--apply")

        self.assertIn("channex_integration=inactive", output)
        self.assertEqual(self._status_of(row), ChannexAriOutbox.Status.ABANDONED)

    # --- pipeline invariant -------------------------------------------------

    @patch("apps.integrations.channex.ari_service.ChannexClient")
    def test_abandoned_row_is_invisible_to_the_flush(self, mock_client_cls):
        from apps.integrations.channex.ari_service import flush_channex_ari_outbox

        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        self._failed_in_cohort()

        self._run("--created-before", CUTOFF, "--apply")
        results = flush_channex_ari_outbox(self.integration, client=mock_client)

        self.assertEqual(results, [])
        mock_client.update_availability.assert_not_called()
        mock_client.update_restrictions.assert_not_called()
