from datetime import date, datetime, time, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.communications.guest_compose import HINT_AUTO_CHECKIN_DOCS_EXPIRED
from apps.communications.models import GuestMessageDraft
from apps.integrations.models import IntegrationConfig, WhatsAppMessage
from apps.integrations.tests.test_whatsapp_webhook import TEST_FERNET_KEY
from apps.integrations.whatsapp.autocheckin_docs_deadline import (
    autocheckin_docs_deadline_at,
    autocheckin_docs_deadline_elapsed,
    mark_autocheckin_session_lost_for_due_reservations,
    schedule_autocheckin_docs_deadline,
)
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant

TEST_WHATSAPP_ACCESS_TOKEN = "test-whatsapp-access-token"
ZAGREB = ZoneInfo("Europe/Zagreb")


@override_settings(STAY_INTEGRATION_FERNET_KEY=TEST_FERNET_KEY)
class AutocheckinDocsDeadlineTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="uzorita", name="Uzorita", default_language="hr")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Uzorita",
            slug="uzorita",
            whatsapp_autocheckin_enabled=True,
            check_in_time=time(15, 0),
            timezone="Europe/Zagreb",
        )
        self.integration = IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.WHATSAPP,
            routing_key="1068791909660300",
            is_active=True,
        )
        self.integration.set_config_dict(
            {
                "phone_number_id": "1068791909660300",
            }
        )
        self.integration.save()
        self.today = date(2026, 6, 7)
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booker_name="Ana Anić",
            booker_phone="+385922222222",
            check_in=self.today,
            check_out=self.today + timedelta(days=2),
            status=Reservation.Status.EXPECTED,
            whatsapp_autocheckin_engaged_at=timezone.now(),
        )
        WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            reservation=self.reservation,
            wamid="wamid.in.guest",
            wa_id="385922222222",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="text",
            body="Da",
            raw_payload={},
        )

    def test_deadline_at_check_in_plus_30(self):
        deadline = autocheckin_docs_deadline_at(self.reservation)
        self.assertEqual(deadline, datetime(2026, 6, 7, 15, 30, tzinfo=ZAGREB))

    @patch("apps.integrations.whatsapp.autocheckin_docs_deadline.autocheckin_docs_deadline_elapsed.apply_async")
    @patch("apps.integrations.whatsapp.autocheckin_docs_deadline.property_local_now")
    def test_schedule_deadline_on_checkin_day(self, mock_now, mock_apply):
        mock_now.return_value = datetime(2026, 6, 7, 10, 0, tzinfo=ZAGREB)
        result = schedule_autocheckin_docs_deadline(self.reservation)
        self.assertEqual(result["status"], "scheduled")
        mock_apply.assert_called_once()
        self.reservation.refresh_from_db()
        self.assertIsNotNone(self.reservation.whatsapp_autocheckin_docs_deadline_at)

    @patch.dict("os.environ", {"WHATSAPP_ACCESS_TOKEN": TEST_WHATSAPP_ACCESS_TOKEN})
    @patch("apps.integrations.whatsapp.autocheckin_docs_deadline.notify_guest_docs_awaiting_arrival")
    @patch("apps.integrations.whatsapp.evisitor_reply.send_text_message")
    @patch("apps.integrations.whatsapp.autocheckin_docs_deadline.waive_whatsapp_autocheckin")
    def test_deadline_elapsed_waives_and_sends_welcome(
        self,
        mock_waive,
        mock_send,
        mock_welcome,
    ):
        mock_send.return_value = {"messages": [{"id": "wamid.out.expired"}]}
        mock_welcome.return_value = {"status": "sent", "channel": "whatsapp"}

        result = autocheckin_docs_deadline_elapsed(self.reservation.pk)

        self.assertEqual(result["status"], "handled")
        mock_waive.assert_called_once()
        mock_send.assert_called_once()
        mock_welcome.assert_called_once()
        self.assertTrue(
            GuestMessageDraft.objects.filter(
                reservation=self.reservation,
                hint=HINT_AUTO_CHECKIN_DOCS_EXPIRED,
            ).exists()
        )

    @patch("apps.integrations.whatsapp.autocheckin_docs_deadline.property_local_now")
    def test_session_lost_flag_at_t_minus_one_hour(self, mock_now):
        mock_now.return_value = datetime(2026, 6, 7, 14, 5, tzinfo=ZAGREB)
        self.reservation.whatsapp_welcome_sent_at = timezone.now()
        self.reservation.whatsapp_autocheckin_engaged_at = None
        self.reservation.save(
            update_fields=[
                "whatsapp_welcome_sent_at",
                "whatsapp_autocheckin_engaged_at",
                "updated_at",
            ]
        )

        result = mark_autocheckin_session_lost_for_due_reservations()

        self.assertEqual(result["marked"], 1)
        self.reservation.refresh_from_db()
        self.assertTrue(self.reservation.whatsapp_autocheckin_session_lost)

    @patch("apps.integrations.whatsapp.autocheckin_docs_deadline.property_local_now")
    def test_session_lost_skipped_outside_window(self, mock_now):
        mock_now.return_value = datetime(2026, 6, 7, 10, 0, tzinfo=ZAGREB)
        self.reservation.whatsapp_welcome_sent_at = timezone.now()
        self.reservation.save(update_fields=["whatsapp_welcome_sent_at", "updated_at"])

        result = mark_autocheckin_session_lost_for_due_reservations()

        self.assertEqual(result["marked"], 0)
