from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone

from apps.integrations.channex.booking_service import channex_external_id
from apps.integrations.channex.message_tasks import (
    channex_reconcile_membership_qs,
    sync_channex_messages_for_upcoming_checkins,
)
from apps.integrations.models import ChannexMessage
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant


class ChannexReconcileMembershipTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="uzorita", name="Uzorita")
        self.other_tenant = Tenant.objects.create(slug="other", name="Other")
        self.property = Property.objects.create(
            tenant=self.tenant,
            slug="uzorita",
            name="Uzorita",
            timezone="Europe/Zagreb",
        )
        self.other_property = Property.objects.create(
            tenant=self.other_tenant,
            slug="other",
            name="Other",
            timezone="Europe/Zagreb",
        )
        self.today = timezone.localdate()
        self._seq = 0

    def _reservation(self, **kwargs) -> Reservation:
        self._seq += 1
        booking_id = kwargs.pop("booking_id", f"booking-{self._seq}")
        defaults = {
            "tenant": self.tenant,
            "property": self.property,
            "external_id": channex_external_id(booking_id),
            "import_source": "channex",
            "check_in": self.today,
            "check_out": self.today + timedelta(days=2),
            "booker_name": f"Guest {self._seq}",
            "status": Reservation.Status.EXPECTED,
        }
        defaults.update(kwargs)
        return Reservation.objects.create(**defaults)

    def _message(self, reservation: Reservation, *, days_ago: float = 0) -> ChannexMessage:
        self._seq += 1
        row = ChannexMessage.objects.create(
            tenant=reservation.tenant,
            reservation=reservation,
            channex_booking_id=f"booking-msg-{self._seq}",
            channex_message_id=f"msg-{self._seq}",
            direction=ChannexMessage.Direction.INBOUND,
            sender=ChannexMessage.Sender.GUEST,
            body="hello",
        )
        if days_ago:
            ChannexMessage.objects.filter(pk=row.pk).update(
                created_at=timezone.now() - timedelta(days=days_ago)
            )
            row.refresh_from_db()
        return row

    def _member_ids(self) -> set[int]:
        return set(channex_reconcile_membership_qs(self.tenant).values_list("pk", flat=True))

    def test_a_includes_expected_check_in_today_through_plus_7d(self):
        included = [
            self._reservation(check_in=self.today),
            self._reservation(check_in=self.today + timedelta(days=7)),
        ]
        excluded = self._reservation(check_in=self.today + timedelta(days=8))
        ids = self._member_ids()
        self.assertTrue({row.pk for row in included}.issubset(ids))
        self.assertNotIn(excluded.pk, ids)

    def test_b_includes_all_checked_in(self):
        in_house = self._reservation(
            status=Reservation.Status.CHECKED_IN,
            check_in=self.today - timedelta(days=3),
            check_out=self.today + timedelta(days=2),
        )
        self.assertIn(in_house.pk, self._member_ids())

    def test_c_includes_checked_out_today_and_yesterday(self):
        today_out = self._reservation(
            status=Reservation.Status.CHECKED_OUT,
            check_in=self.today - timedelta(days=3),
            check_out=self.today,
        )
        yesterday = self._reservation(
            status=Reservation.Status.CHECKED_OUT,
            check_in=self.today - timedelta(days=4),
            check_out=self.today - timedelta(days=1),
        )
        older = self._reservation(
            status=Reservation.Status.CHECKED_OUT,
            check_in=self.today - timedelta(days=6),
            check_out=self.today - timedelta(days=2),
        )
        ids = self._member_ids()
        self.assertIn(today_out.pk, ids)
        self.assertIn(yesterday.pk, ids)
        self.assertNotIn(older.pk, ids)

    def test_d_includes_far_out_expected_with_recent_activity(self):
        far = self._reservation(check_in=self.today + timedelta(days=21))
        self._message(far, days_ago=1)
        self.assertIn(far.pk, self._member_ids())

    def test_d_excludes_stale_activity_outside_calendar(self):
        far = self._reservation(check_in=self.today + timedelta(days=21))
        self._message(far, days_ago=8)
        self.assertNotIn(far.pk, self._member_ids())

    def test_d_includes_older_checked_out_with_recent_activity(self):
        older = self._reservation(
            status=Reservation.Status.CHECKED_OUT,
            check_in=self.today - timedelta(days=10),
            check_out=self.today - timedelta(days=5),
        )
        self._message(older, days_ago=2)
        self.assertIn(older.pk, self._member_ids())

    def test_eligible_status_filter_applies_after_d(self):
        for status in (
            Reservation.Status.CANCELED,
            Reservation.Status.NO_SHOW,
            Reservation.Status.REFUSED,
            Reservation.Status.PENDING,
        ):
            with self.subTest(status=status):
                row = self._reservation(
                    status=status,
                    check_in=self.today,
                    check_out=self.today + timedelta(days=1),
                )
                self._message(row, days_ago=0)
                self.assertNotIn(row.pk, self._member_ids())

    def test_excludes_non_channex_and_other_tenant(self):
        manual = self._reservation(import_source="booking_pdf")
        other = self._reservation(
            tenant=self.other_tenant,
            property=self.other_property,
        )
        self._message(manual, days_ago=0)
        self._message(other, days_ago=0)
        ids = self._member_ids()
        self.assertNotIn(manual.pk, ids)
        self.assertNotIn(other.pk, ids)

    def test_unlinked_message_does_not_admit_a_reservation(self):
        far = self._reservation(check_in=self.today + timedelta(days=21))
        ChannexMessage.objects.create(
            tenant=self.tenant,
            reservation=None,
            channex_booking_id="orphan-booking",
            channex_message_id="orphan-msg",
            direction=ChannexMessage.Direction.INBOUND,
            sender=ChannexMessage.Sender.GUEST,
            body="orphan",
        )
        self.assertNotIn(far.pk, self._member_ids())

    def test_membership_is_deduped_when_a_and_d_overlap(self):
        row = self._reservation(check_in=self.today)
        self._message(row, days_ago=0)
        pks = list(channex_reconcile_membership_qs(self.tenant).values_list("pk", flat=True))
        self.assertEqual(pks.count(row.pk), 1)

    @patch("apps.integrations.channex.message_tasks.relink_unlinked_channex_messages", return_value=0)
    @patch("apps.integrations.channex.message_tasks.sync_booking_messages_from_channex", return_value=[])
    @patch("apps.integrations.channex.message_tasks.get_active_channex_integration")
    def test_task_calls_provider_once_per_member_and_skips_unsyncable(
        self,
        mock_integration,
        mock_sync,
        mock_relink,
    ):
        mock_integration.return_value = MagicMock()
        in_a_and_d = self._reservation(check_in=self.today)
        self._message(in_a_and_d, days_ago=0)
        unsyncable = self._reservation(
            check_in=self.today + timedelta(days=1),
            external_id="",
            import_source="channex",
        )
        canceled = self._reservation(status=Reservation.Status.CANCELED)
        self._message(canceled, days_ago=0)
        far_quiet = self._reservation(check_in=self.today + timedelta(days=21))

        result = sync_channex_messages_for_upcoming_checkins(tenant_slug="uzorita")

        called_ids = [call.args[1].pk for call in mock_sync.call_args_list]
        self.assertEqual(called_ids.count(in_a_and_d.pk), 1)
        self.assertNotIn(unsyncable.pk, called_ids)
        self.assertNotIn(canceled.pk, called_ids)
        self.assertNotIn(far_quiet.pk, called_ids)
        self.assertEqual(result["candidates"], 2)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["failed"], 0)
        mock_relink.assert_called_once_with(self.tenant)
