from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings

from apps.integrations.models import IntegrationConfig, WhatsAppMessage
from apps.integrations.tests.test_whatsapp_webhook import TEST_FERNET_KEY
from apps.integrations.whatsapp.operator_arrival_confirm import (
    OPERATOR_ARRIVED_NO_PREFIX,
    OPERATOR_ARRIVED_YES_PREFIX,
    _default_confirmed_arrival_at,
    handle_operator_arrival_confirm_inbound,
    operator_arrived_no_button_id,
    operator_arrived_yes_button_id,
    schedule_arrival_confirm_prompt,
    send_arrival_confirm_prompt,
)
from apps.integrations.whatsapp.runtime_config import WhatsAppRuntimeConfig
from apps.properties.models import Property
from apps.reservations.models import (
    Reservation,
    ReservationUnit,
    WhatsAppArrivalConfirmSession,
    WhatsAppArrivalConfirmSessionStatus,
    WhatsAppArrivalConfirmTrigger,
)
from apps.tenants.models import Tenant, TenantReceptionSettings

ZAGREB = ZoneInfo("Europe/Zagreb")


@override_settings(
    STAY_INTEGRATION_FERNET_KEY=TEST_FERNET_KEY,
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class OperatorArrivalConfirmTests(TestCase):
    def setUp(self):
        self._prompts_enabled = patch(
            "apps.integrations.whatsapp.operator_arrival_confirm.WHATSAPP_ARRIVAL_CONFIRM_PROMPTS_ENABLED",
            True,
        )
        self._prompts_enabled.start()
        self.addCleanup(self._prompts_enabled.stop)
        self.tenant = Tenant.objects.create(slug="uzorita", name="Uzorita", default_language="hr")
        TenantReceptionSettings.objects.create(
            tenant=self.tenant,
            whatsapp_operator_phones=[{"name": "Toni", "phone": "+385998388513"}],
        )
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Uzorita",
            slug="uzorita",
            whatsapp_autocheckin_enabled=True,
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
        self.runtime = WhatsAppRuntimeConfig.from_integration_dict(self.integration.get_config_dict())
        self.operator_wa_id = "385998388513"
        self._inbound_wamid_counter = 0
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booker_name="Ana Anić",
            booking_code="BCOM-181",
            check_in=date(2026, 6, 7),
            check_out=date(2026, 6, 9),
            status=Reservation.Status.EXPECTED,
            guest_stated_arrival_text="~ 18-19",
            guest_stated_arrival_at=datetime(2026, 6, 7, 19, 0, tzinfo=ZAGREB),
        )
        ReservationUnit.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            sort_order=0,
            room_name="A1",
        )

    def _operator_row(self, *, button_id: str = "", body: str = "") -> WhatsAppMessage:
        self._inbound_wamid_counter += 1
        payload = {"type": "text", "text": {"body": body}}
        message_type = "text"
        if button_id:
            message_type = "interactive"
            payload = {
                "type": "interactive",
                "interactive": {
                    "type": "button_reply",
                    "button_reply": {"id": button_id, "title": body or "Da"},
                },
            }
        return WhatsAppMessage.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            reservation=self.reservation,
            wamid=f"wamid.in.arrival.{self._inbound_wamid_counter}",
            wa_id=self.operator_wa_id,
            phone_number_id="1068791909660300",
            direction=WhatsAppMessage.Direction.INBOUND,
            message_type=message_type,
            body=body,
            raw_payload=payload,
        )

    @patch("apps.integrations.whatsapp.operator_arrival_confirm.property_local_now")
    @patch("apps.integrations.whatsapp.operator_arrival_confirm._notify_arrival_confirm_push")
    @patch("apps.integrations.whatsapp.operator_arrival_confirm._operator_session_open", return_value=True)
    @patch("apps.integrations.whatsapp.operator_arrival_confirm.send_interactive_button_message")
    def test_send_prompt_to_all_operators(self, mock_send, mock_session, mock_push, mock_now):
        mock_now.return_value = datetime(2026, 6, 7, 16, 0, tzinfo=ZAGREB)
        mock_send.return_value = {"messages": [{"id": "wamid.prompt.1"}]}
        mock_push.return_value = {"sent": 1}

        result = send_arrival_confirm_prompt(
            self.reservation,
            trigger=WhatsAppArrivalConfirmTrigger.GUEST_DEADLINE_PLUS_30,
            integration_row=self.integration,
            runtime=self.runtime,
        )

        self.assertEqual(result["status"], "prompted")
        mock_send.assert_called_once()
        call_kwargs = mock_send.call_args.kwargs
        self.assertIn(str(self.reservation.pk), call_kwargs["body"])
        buttons = call_kwargs["buttons"]
        self.assertEqual(buttons[0][0], operator_arrived_yes_button_id(self.reservation.pk))
        self.assertEqual(buttons[1][0], operator_arrived_no_button_id(self.reservation.pk))
        session = WhatsAppArrivalConfirmSession.objects.get(reservation=self.reservation)
        self.assertEqual(session.status, WhatsAppArrivalConfirmSessionStatus.AWAITING_ARRIVED)

    @patch("apps.integrations.whatsapp.operator_arrival_confirm.property_local_now")
    @patch("apps.integrations.whatsapp.operator_arrival_confirm._notify_arrival_confirm_push")
    @patch("apps.integrations.whatsapp.operator_arrival_confirm._operator_session_open", return_value=True)
    @patch("apps.integrations.whatsapp.operator_arrival_confirm.send_interactive_button_message")
    def test_session_defaults_stated_arrival_to_check_in_time(self, mock_send, mock_session, mock_push, mock_now):
        mock_now.return_value = datetime(2026, 6, 7, 16, 0, tzinfo=ZAGREB)
        mock_send.return_value = {"messages": [{"id": "wamid.prompt.1"}]}
        mock_push.return_value = {"sent": 1}
        reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booker_name="Marko Markić",
            booking_code="BCOM-182",
            check_in=date(2026, 6, 7),
            check_out=date(2026, 6, 9),
            status=Reservation.Status.EXPECTED,
        )
        ReservationUnit.objects.create(
            tenant=self.tenant,
            reservation=reservation,
            sort_order=0,
            room_name="B2",
        )

        send_arrival_confirm_prompt(
            reservation,
            trigger=WhatsAppArrivalConfirmTrigger.NIGHTLY_23H,
            integration_row=self.integration,
            runtime=self.runtime,
        )

        session = WhatsAppArrivalConfirmSession.objects.get(reservation=reservation)
        self.assertEqual(
            session.guest_stated_arrival_at,
            datetime(2026, 6, 7, 15, 0, tzinfo=ZAGREB),
        )
        self.assertIsNone(reservation.guest_stated_arrival_at)

    def test_default_confirmed_arrival_at_without_stated_uses_check_in_time(self):
        reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booker_name="Marko Markić",
            booking_code="BCOM-184",
            check_in=date(2026, 6, 7),
            check_out=date(2026, 6, 9),
            status=Reservation.Status.EXPECTED,
        )
        self.assertEqual(
            _default_confirmed_arrival_at(reservation),
            datetime(2026, 6, 7, 15, 0, tzinfo=ZAGREB),
        )

    @patch("apps.integrations.whatsapp.operator_arrival_confirm.property_local_now")
    @patch("apps.integrations.whatsapp.client.send_text_message")
    @patch("apps.integrations.whatsapp.operator_arrival_confirm._notify_arrival_confirm_push")
    @patch("apps.integrations.whatsapp.operator_arrival_confirm._finish_arrival_checkin")
    @patch("apps.integrations.whatsapp.operator_arrival_confirm._operator_session_open", return_value=True)
    @patch("apps.integrations.whatsapp.operator_arrival_confirm.send_interactive_button_message")
    def test_yes_triggers_checkin_immediately(self, mock_send, mock_session, mock_finish, mock_push, mock_text, mock_now):
        mock_now.return_value = datetime(2026, 6, 7, 23, 0, tzinfo=ZAGREB)
        mock_send.return_value = {"messages": [{"id": "wamid.prompt.1"}]}
        mock_finish.return_value = {"status": "completed"}
        mock_push.return_value = {"sent": 1}
        send_arrival_confirm_prompt(
            self.reservation,
            trigger=WhatsAppArrivalConfirmTrigger.NIGHTLY_23H,
            integration_row=self.integration,
            runtime=self.runtime,
        )

        yes_row = self._operator_row(
            button_id=operator_arrived_yes_button_id(self.reservation.pk),
            body="Da",
        )
        yes_result = handle_operator_arrival_confirm_inbound(
            row=yes_row,
            integration_row=self.integration,
            runtime=self.runtime,
            action_text="Da",
            button_id=operator_arrived_yes_button_id(self.reservation.pk),
        )
        self.assertEqual(yes_result["status"], "completed")
        mock_finish.assert_called_once()

    @patch("apps.integrations.whatsapp.operator_arrival_confirm.property_local_now")
    @patch("apps.integrations.whatsapp.client.send_text_message")
    @patch("apps.integrations.whatsapp.operator_arrival_confirm._notify_arrival_confirm_push")
    @patch("apps.integrations.whatsapp.operator_arrival_confirm._finish_arrival_checkin")
    @patch("apps.integrations.whatsapp.operator_arrival_confirm._operator_session_open", return_value=True)
    @patch("apps.integrations.whatsapp.operator_arrival_confirm.send_interactive_button_message")
    def test_legacy_awaiting_time_still_completes(self, mock_send, mock_session, mock_finish, mock_push, mock_text, mock_now):
        mock_now.return_value = datetime(2026, 6, 7, 23, 0, tzinfo=ZAGREB)
        mock_send.return_value = {"messages": [{"id": "wamid.prompt.1"}]}
        mock_finish.return_value = {"status": "completed"}
        mock_push.return_value = {"sent": 1}
        mock_text.return_value = {"messages": [{"id": "wamid.op.text.1"}]}
        send_arrival_confirm_prompt(
            self.reservation,
            trigger=WhatsAppArrivalConfirmTrigger.NIGHTLY_23H,
            integration_row=self.integration,
            runtime=self.runtime,
        )
        session = WhatsAppArrivalConfirmSession.objects.get(reservation=self.reservation)
        session.status = WhatsAppArrivalConfirmSessionStatus.AWAITING_TIME
        session.responded_operator_wa_id = self.operator_wa_id
        session.save(update_fields=["status", "responded_operator_wa_id", "updated_at"])

        time_row = self._operator_row(body="19:30")
        time_result = handle_operator_arrival_confirm_inbound(
            row=time_row,
            integration_row=self.integration,
            runtime=self.runtime,
            action_text="19:30",
            button_id="",
        )
        self.assertEqual(time_result["status"], "completed")
        mock_finish.assert_called_once()

    @patch("apps.integrations.whatsapp.operator_arrival_confirm.property_local_now")
    @patch("apps.integrations.whatsapp.client.send_text_message")
    @patch("apps.integrations.whatsapp.operator_arrival_confirm._notify_arrival_confirm_push")
    @patch("apps.integrations.whatsapp.operator_arrival_confirm._operator_session_open", return_value=True)
    @patch("apps.integrations.whatsapp.operator_arrival_confirm.send_interactive_button_message")
    def test_no_keeps_expected(self, mock_send, mock_session, mock_push, mock_text, mock_now):
        mock_now.return_value = datetime(2026, 6, 7, 16, 0, tzinfo=ZAGREB)
        mock_send.return_value = {"messages": [{"id": "wamid.prompt.1"}]}
        mock_push.return_value = {"sent": 1}
        mock_text.return_value = {"messages": [{"id": "wamid.op.text.2"}]}
        send_arrival_confirm_prompt(
            self.reservation,
            trigger=WhatsAppArrivalConfirmTrigger.GUEST_DEADLINE_PLUS_30,
            integration_row=self.integration,
            runtime=self.runtime,
        )

        no_row = self._operator_row(
            button_id=operator_arrived_no_button_id(self.reservation.pk),
            body="Ne",
        )
        result = handle_operator_arrival_confirm_inbound(
            row=no_row,
            integration_row=self.integration,
            runtime=self.runtime,
            action_text="Ne",
            button_id=operator_arrived_no_button_id(self.reservation.pk),
        )
        self.assertEqual(result["status"], "declined")
        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.status, Reservation.Status.EXPECTED)
        session = WhatsAppArrivalConfirmSession.objects.get(reservation=self.reservation)
        self.assertEqual(session.status, WhatsAppArrivalConfirmSessionStatus.DECLINED)

    @patch("apps.integrations.whatsapp.operator_arrival_confirm.arrival_confirm_guest_deadline_elapsed.apply_async")
    @patch("apps.integrations.whatsapp.operator_arrival_confirm.property_local_now")
    def test_schedule_timer_at_guest_deadline_plus_30(self, mock_now, mock_apply):
        mock_now.return_value = datetime(2026, 6, 7, 17, 0, tzinfo=ZAGREB)
        run_at = datetime(2026, 6, 7, 19, 30, tzinfo=ZAGREB)
        schedule_arrival_confirm_prompt(
            self.reservation,
            trigger=WhatsAppArrivalConfirmTrigger.GUEST_DEADLINE_PLUS_30,
            run_at=run_at,
        )
        mock_apply.assert_called_once()
        self.assertEqual(mock_apply.call_args.kwargs["countdown"], 9000)

    def test_nightly_skips_checked_out_reservations(self):
        from apps.integrations.whatsapp.operator_arrival_confirm import send_nightly_arrival_confirm_prompts

        self.reservation.check_in = date(2026, 6, 12)
        self.reservation.save(update_fields=["check_in", "updated_at"])
        checked_out = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booker_name="Old Guest",
            booking_code="OLD-1",
            check_in=date(2026, 6, 12),
            check_out=date(2026, 6, 13),
            status=Reservation.Status.CHECKED_OUT,
        )
        WhatsAppArrivalConfirmSession.objects.create(
            tenant_id=self.tenant.pk,
            reservation=checked_out,
            status=WhatsAppArrivalConfirmSessionStatus.AWAITING_ARRIVED,
            trigger=WhatsAppArrivalConfirmTrigger.GUEST_DEADLINE_PLUS_30,
        )
        with patch(
            "apps.integrations.whatsapp.operator_arrival_confirm.property_local_now",
            return_value=datetime(2026, 6, 12, 23, 5, tzinfo=ZAGREB),
        ), patch(
            "apps.integrations.whatsapp.apply_reply.is_document_checkin_complete",
            return_value=True,
        ), patch(
            "apps.integrations.whatsapp.operator_arrival_confirm.send_arrival_confirm_prompt",
            return_value={"status": "prompted"},
        ) as mock_prompt:
            result = send_nightly_arrival_confirm_prompts()
        self.assertEqual(result["prompted"], 1)
        mock_prompt.assert_called_once()
        self.assertEqual(mock_prompt.call_args.args[0].pk, self.reservation.pk)
        stale = WhatsAppArrivalConfirmSession.objects.get(reservation=checked_out)
        self.assertEqual(stale.status, WhatsAppArrivalConfirmSessionStatus.DONE)

    @patch("apps.integrations.whatsapp.apply_reply.is_document_checkin_complete", return_value=True)
    @patch("apps.integrations.whatsapp.operator_arrival_confirm.property_local_now")
    def test_nightly_prompt_runs_at_23h_property_local(self, mock_now, mock_docs_complete):
        from apps.integrations.whatsapp.operator_arrival_confirm import send_nightly_arrival_confirm_prompts

        mock_now.return_value = datetime(2026, 6, 7, 23, 5, tzinfo=ZAGREB)
        with patch(
            "apps.integrations.whatsapp.operator_arrival_confirm.send_arrival_confirm_prompt",
            return_value={"status": "prompted"},
        ) as mock_prompt:
            result = send_nightly_arrival_confirm_prompts()
        self.assertEqual(result["prompted"], 1)
        mock_prompt.assert_called_once()

    @patch("apps.integrations.whatsapp.operator_arrival_confirm.property_local_now")
    @patch("apps.integrations.whatsapp.client.send_template_message")
    @patch("apps.integrations.whatsapp.operator_arrival_confirm._notify_arrival_confirm_push")
    @patch("apps.integrations.whatsapp.operator_arrival_confirm._operator_session_open", return_value=False)
    def test_send_prompt_uses_template_when_session_closed(self, mock_session, mock_push, mock_template, mock_now):
        mock_now.return_value = datetime(2026, 6, 7, 16, 0, tzinfo=ZAGREB)
        mock_push.return_value = {"sent": 1}
        mock_template.return_value = {"messages": [{"id": "wamid.template.1"}]}

        result = send_arrival_confirm_prompt(
            self.reservation,
            trigger=WhatsAppArrivalConfirmTrigger.GUEST_DEADLINE_PLUS_30,
            integration_row=self.integration,
            runtime=self.runtime,
        )

        self.assertEqual(result["status"], "prompted")
        mock_template.assert_called_once()
        self.assertEqual(result["operators"][0]["channel"], "template")

    @patch("apps.integrations.whatsapp.operator_arrival_confirm.property_local_now")
    @patch("apps.integrations.whatsapp.client.send_text_message")
    @patch("apps.integrations.whatsapp.operator_arrival_confirm._notify_arrival_confirm_push")
    @patch("apps.integrations.whatsapp.operator_arrival_confirm._operator_session_open", return_value=True)
    @patch("apps.integrations.whatsapp.operator_arrival_confirm.send_interactive_button_message")
    def test_double_operator_gets_already_handled(self, mock_send, mock_session, mock_push, mock_text, mock_now):
        mock_now.return_value = datetime(2026, 6, 7, 23, 0, tzinfo=ZAGREB)
        mock_send.return_value = {"messages": [{"id": "wamid.prompt.1"}]}
        mock_push.return_value = {"sent": 1}
        mock_text.return_value = {"messages": [{"id": "wamid.op.text.3"}]}
        send_arrival_confirm_prompt(
            self.reservation,
            trigger=WhatsAppArrivalConfirmTrigger.NIGHTLY_23H,
            integration_row=self.integration,
            runtime=self.runtime,
        )
        session = WhatsAppArrivalConfirmSession.objects.get(reservation=self.reservation)
        session.status = WhatsAppArrivalConfirmSessionStatus.DONE
        session.save(update_fields=["status", "updated_at"])

        row = self._operator_row(
            button_id=operator_arrived_yes_button_id(self.reservation.pk),
            body="Da",
        )
        result = handle_operator_arrival_confirm_inbound(
            row=row,
            integration_row=self.integration,
            runtime=self.runtime,
            action_text="Da",
            button_id=operator_arrived_yes_button_id(self.reservation.pk),
        )
        self.assertEqual(result["status"], "already_handled")

    @patch("apps.integrations.whatsapp.operator_arrival_confirm.property_local_now")
    @patch("apps.integrations.whatsapp.operator_arrival_confirm._notify_arrival_confirm_push")
    @patch("apps.integrations.whatsapp.operator_arrival_confirm._operator_session_open", return_value=True)
    @patch("apps.integrations.whatsapp.operator_arrival_confirm.send_interactive_button_message")
    def test_send_prompt_skipped_when_not_checkin_day(self, mock_send, mock_session, mock_push, mock_now):
        mock_now.return_value = datetime(2026, 6, 14, 23, 30, tzinfo=ZAGREB)
        mock_push.return_value = {"sent": 0}

        result = send_arrival_confirm_prompt(
            self.reservation,
            trigger=WhatsAppArrivalConfirmTrigger.GUEST_DEADLINE_PLUS_30,
            integration_row=self.integration,
            runtime=self.runtime,
        )

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "not_checkin_day")
        mock_send.assert_not_called()
        self.assertFalse(
            WhatsAppArrivalConfirmSession.objects.filter(reservation=self.reservation).exists()
        )

    @patch("apps.integrations.whatsapp.operator_arrival_confirm.property_local_now")
    @patch("apps.integrations.whatsapp.operator_arrival_confirm._notify_arrival_confirm_push")
    @patch("apps.integrations.whatsapp.operator_arrival_confirm._operator_session_open", return_value=True)
    @patch("apps.integrations.whatsapp.operator_arrival_confirm.send_interactive_button_message")
    def test_prompt_uses_planned_checkin_when_guest_did_not_report(self, mock_send, mock_session, mock_push, mock_now):
        mock_now.return_value = datetime(2026, 6, 7, 16, 0, tzinfo=ZAGREB)
        mock_send.return_value = {"messages": [{"id": "wamid.prompt.1"}]}
        mock_push.return_value = {"sent": 1}
        reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booker_name="Paweł Sobolewski",
            booking_code="6245110966",
            check_in=date(2026, 6, 7),
            check_out=date(2026, 6, 8),
            status=Reservation.Status.EXPECTED,
        )
        ReservationUnit.objects.create(
            tenant=self.tenant,
            reservation=reservation,
            sort_order=0,
            room_name="R3",
        )

        send_arrival_confirm_prompt(
            reservation,
            trigger=WhatsAppArrivalConfirmTrigger.NIGHTLY_23H,
            integration_row=self.integration,
            runtime=self.runtime,
        )

        body = mock_send.call_args.kwargs["body"]
        self.assertIn("Planirani check-in: 15:00", body)
        self.assertNotIn("Gost javio:", body)

    @patch("apps.integrations.whatsapp.operator_arrival_confirm.property_local_now")
    def test_close_obsolete_sessions_for_future_checkin(self, mock_now):
        from apps.integrations.whatsapp.operator_arrival_confirm import _close_obsolete_arrival_sessions

        mock_now.return_value = datetime(2026, 6, 14, 23, 30, tzinfo=ZAGREB)
        WhatsAppArrivalConfirmSession.objects.create(
            tenant_id=self.tenant.pk,
            reservation=self.reservation,
            status=WhatsAppArrivalConfirmSessionStatus.AWAITING_ARRIVED,
            trigger=WhatsAppArrivalConfirmTrigger.GUEST_DEADLINE_PLUS_30,
        )

        closed = _close_obsolete_arrival_sessions(reservation_id=self.reservation.pk)

        self.assertEqual(closed, 1)
        session = WhatsAppArrivalConfirmSession.objects.get(reservation=self.reservation)
        self.assertEqual(session.status, WhatsAppArrivalConfirmSessionStatus.DONE)

    def test_send_prompt_disabled_by_default(self):
        with patch(
            "apps.integrations.whatsapp.operator_arrival_confirm.WHATSAPP_ARRIVAL_CONFIRM_PROMPTS_ENABLED",
            False,
        ):
            result = send_arrival_confirm_prompt(
                self.reservation,
                trigger=WhatsAppArrivalConfirmTrigger.GUEST_DEADLINE_PLUS_30,
            )
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "disabled")
