from datetime import date
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.communications.guest_compose import render_evisitor_registered_message
from apps.integrations.models import IntegrationConfig, WhatsAppMessage
from apps.integrations.tests.test_whatsapp_webhook import TEST_FERNET_KEY
from apps.integrations.whatsapp.evisitor_reply import (
    maybe_send_evisitor_registered_whatsapp_reply,
)
from apps.properties.models import Property
from apps.reservations.models import EvisitorGuestStatus, Guest, Reservation
from apps.tenants.models import Tenant

TEST_WHATSAPP_ACCESS_TOKEN = "test-whatsapp-access-token"


@override_settings(
    STAY_INTEGRATION_FERNET_KEY=TEST_FERNET_KEY,
)
class WhatsAppEvisitorRegisteredReplyTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="uzorita", name="Uzorita", default_language="hr")
        self.property = Property.objects.create(tenant=self.tenant, name="Uzorita", slug="uzorita")
        self.integration = IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.WHATSAPP,
            routing_key="1068791909660300",
            is_active=True,
        )
        self.integration.set_config_dict(
            {
                "phone_number_id": "1068791909660300",
                "auto_reply": False,
            }
        )
        self.integration.save()

        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booker_name="Audrius Kavaliauskas",
            booker_phone="+37061284340",
            booker_country="LT",
            adults_count=2,
            check_in=date(2026, 6, 7),
            check_out=date(2026, 6, 9),
            status=Reservation.Status.CHECKED_IN,
        )
        WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            reservation=self.reservation,
            wamid="wamid.inbound.evisitor",
            wa_id="37061284340",
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type="text",
            body="Hello",
            raw_payload={"type": "text", "text": {"body": "Hello"}},
        )
        self.adult1 = Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="AUDRIUS",
            last_name="KAVALIAUSKAS",
            name="AUDRIUS KAVALIAUSKAS",
            is_primary=True,
            document_number="16772087",
            date_of_birth=date(1989, 2, 13),
            nationality="LT",
            sex="M",
            evisitor_status=EvisitorGuestStatus.SENT,
        )
        self.adult2 = Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="LAURA",
            last_name="MATONYTE",
            name="LAURA MATONYTE",
            is_primary=False,
            document_number="1625626",
            date_of_birth=date(1992, 10, 10),
            nationality="LT",
            sex="F",
            evisitor_status=EvisitorGuestStatus.NOT_SENT,
        )

    def test_render_evisitor_registered_en_for_lt_booker(self):
        text = render_evisitor_registered_message(self.reservation)
        self.assertIn("You are now registered in eVisitor", text)
        self.assertIn("pleasant stay", text)

    @patch.dict("os.environ", {"WHATSAPP_EVISITOR_REGISTERED_REPLY": "true", "WHATSAPP_ACCESS_TOKEN": TEST_WHATSAPP_ACCESS_TOKEN})
    def test_skips_when_evisitor_incomplete(self):
        result = maybe_send_evisitor_registered_whatsapp_reply(self.reservation)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "evisitor_incomplete")

    @patch.dict("os.environ", {"WHATSAPP_EVISITOR_REGISTERED_REPLY": "true", "WHATSAPP_ACCESS_TOKEN": TEST_WHATSAPP_ACCESS_TOKEN})
    @patch("apps.integrations.whatsapp.evisitor_reply.send_text_message")
    def test_sends_when_all_required_guests_sent(self, mock_send):
        mock_send.return_value = {"messages": [{"id": "wamid.out.evisitor"}]}
        self.adult2.evisitor_status = EvisitorGuestStatus.SENT
        self.adult2.save(update_fields=["evisitor_status"])

        result = maybe_send_evisitor_registered_whatsapp_reply(self.reservation)

        self.assertEqual(result["status"], "sent")
        mock_send.assert_called_once()
        body = mock_send.call_args.kwargs["body"]
        self.assertIn("You are now registered in eVisitor", body)

    @patch.dict("os.environ", {"WHATSAPP_EVISITOR_REGISTERED_REPLY": "true", "WHATSAPP_ACCESS_TOKEN": TEST_WHATSAPP_ACCESS_TOKEN})
    @patch("apps.integrations.whatsapp.evisitor_reply.send_text_message")
    def test_skips_duplicate(self, mock_send):
        mock_send.return_value = {"messages": [{"id": "wamid.out.evisitor"}]}
        self.adult2.evisitor_status = EvisitorGuestStatus.SENT
        self.adult2.save(update_fields=["evisitor_status"])

        maybe_send_evisitor_registered_whatsapp_reply(self.reservation)
        result = maybe_send_evisitor_registered_whatsapp_reply(self.reservation)

        self.assertEqual(result["status"], "already_sent")
        mock_send.assert_called_once()
