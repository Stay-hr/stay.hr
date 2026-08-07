from datetime import date, timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.communications.guest_compose import HINT_DOCS_AWAITING_ARRIVAL
from apps.integrations.models import IntegrationConfig, WhatsAppMessage
from apps.integrations.tests.test_whatsapp_webhook import TEST_FERNET_KEY
from apps.integrations.whatsapp.guest_welcome_sequence import (
    send_guest_welcome_entrance_and_ask_arrival,
)
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant

TEST_WHATSAPP_ACCESS_TOKEN = "test-whatsapp-access-token"


@override_settings(STAY_INTEGRATION_FERNET_KEY=TEST_FERNET_KEY)
class GuestWelcomeSequenceTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="uzorita", name="Uzorita", default_language="hr")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Uzorita",
            slug="uzorita",
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
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booker_name="Ana Anić",
            booker_phone="+385922222222",
            check_in=date(2026, 6, 7),
            check_out=date(2026, 6, 9),
            status=Reservation.Status.EXPECTED,
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
            body="Hi",
            raw_payload={},
        )

    @patch.dict("os.environ", {"WHATSAPP_ACCESS_TOKEN": TEST_WHATSAPP_ACCESS_TOKEN})
    @patch("apps.integrations.whatsapp.guest_welcome_sequence.send_whatsapp_ask_arrival_time")
    @patch("apps.integrations.whatsapp.guest_welcome_sequence._send_checkin_complete_entrance_image")
    @patch("apps.integrations.whatsapp.evisitor_reply.send_text_message")
    def test_welcome_sequence_text_then_image_then_ask(
        self,
        mock_text,
        mock_entrance,
        mock_ask,
    ):
        mock_text.return_value = {"messages": [{"id": "wamid.out.welcome"}]}
        mock_entrance.return_value = {"status": "sent", "wamid": "wamid.out.img"}
        mock_ask.return_value = {"status": "sent", "wamid": "wamid.out.ask"}

        result = send_guest_welcome_entrance_and_ask_arrival(
            self.reservation,
            body="Welcome body",
            hint=HINT_DOCS_AWAITING_ARRIVAL,
        )

        self.assertEqual(result["status"], "sent")
        mock_text.assert_called_once()
        mock_entrance.assert_called_once()
        mock_ask.assert_called_once()
        self.assertEqual(mock_text.call_args.kwargs["body"], "Welcome body")
        self.assertEqual(mock_entrance.call_args.args[0].pk, self.reservation.pk)
        self.assertEqual(mock_ask.call_args.args[0].pk, self.reservation.pk)
