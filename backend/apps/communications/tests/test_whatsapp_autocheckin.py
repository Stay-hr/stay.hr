from datetime import date, datetime, time, timedelta
import html
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.communications.guest_compose import render_autocheckin_whatsapp_intro_email_html
from apps.communications.whatsapp_autocheckin_tasks import (
    is_immediate_autocheckin_eligible,
    iter_due_autocheckin_intro_emails,
    iter_due_autocheckin_reservations,
    maybe_send_immediate_autocheckin_welcome,
    run_whatsapp_autocheckin_welcome,
    send_autocheckin_intro_email,
    send_welcome_template_for_reservation,
)
from apps.communications.models import GuestOutboundMessage, GuestOutboundMessageStatus
from apps.integrations.models import IntegrationConfig
from apps.integrations.tests.test_whatsapp_webhook import TEST_FERNET_KEY
from apps.integrations.whatsapp.welcome_template import build_welcome_template_parameters
from apps.properties.models import Property
from apps.reservations.booking_lifecycle import confirm_web_booking
from apps.reservations.models import (
    GuestCheckInSession,
    GuestCheckInSessionCreatedFrom,
    GuestCheckInSessionStatus,
    Reservation,
)
from apps.tenants.models import Tenant

TEST_D360_KEY = "test-d360-key"
ZAGREB = ZoneInfo("Europe/Zagreb")


@override_settings(STAY_INTEGRATION_FERNET_KEY=TEST_FERNET_KEY)
class WhatsAppAutocheckinWelcomeTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="uzorita", name="Uzorita", default_language="hr")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Uzorita",
            slug="uzorita",
            whatsapp_autocheckin_enabled=True,
            whatsapp_autocheckin_time=time(8, 0),
        )
        self.integration = IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.WHATSAPP,
            routing_key="1068791909660300",
            is_active=True,
        )
        self.integration.set_config_dict(
            {
                "provider": "360dialog",
                "phone_number_id": "1068791909660300",
                "access_token": TEST_D360_KEY,
                "api_base_url": "https://waba-v2.360dialog.io",
                "display_phone_number": "+385911111111",
                "auto_reply": False,
                "whatsapp_templates": {
                    "header_image_url": "https://stay.hr/static/whatsapp-header.png",
                    "welcome": {"hr": "stay_welcome_hr", "en": "stay_welcome_en"},
                },
            }
        )
        self.integration.save()
        self.today = date(2026, 6, 7)
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booker_name="Ana Anić",
            booker_phone="+385911111111",
            booker_email="ana@example.com",
            booking_code="BCOM-100",
            check_in=self.today,
            check_out=self.today + timedelta(days=2),
            status=Reservation.Status.EXPECTED,
        )

    def test_welcome_template_parameters(self):
        lang, params = build_welcome_template_parameters(self.reservation)
        self.assertEqual(lang, "hr")
        self.assertEqual(params[0], "Ana")
        self.assertEqual(params[1], "BCOM-100")
        self.assertEqual(params[2], "Uzorita")
        self.assertIn("2026-06-07", params[3])
        self.assertIn("2026-06-09", params[4])

    @patch("apps.communications.whatsapp_autocheckin_tasks.property_local_now")
    def test_due_after_autocheckin_time(self, mock_now):
        mock_now.return_value = datetime(2026, 6, 7, 8, 15, tzinfo=ZAGREB)
        due = iter_due_autocheckin_reservations()
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0].pk, self.reservation.pk)

    @patch("apps.communications.whatsapp_autocheckin_tasks.property_local_now")
    def test_not_due_before_autocheckin_time(self, mock_now):
        mock_now.return_value = datetime(2026, 6, 7, 7, 45, tzinfo=ZAGREB)
        due = iter_due_autocheckin_reservations()
        self.assertEqual(due, [])

    @patch.dict("os.environ", {"D360_API_KEY": TEST_D360_KEY})
    @patch("apps.communications.whatsapp_autocheckin_tasks.send_template_message")
    @patch("apps.communications.whatsapp_autocheckin_tasks.property_local_now")
    def test_send_welcome_marks_sent(self, mock_now, mock_send):
        mock_now.return_value = datetime(2026, 6, 7, 9, 0, tzinfo=ZAGREB)
        mock_send.return_value = {"messages": [{"id": "wamid.welcome.1"}]}

        result = send_welcome_template_for_reservation(self.reservation)
        self.assertEqual(result["status"], "sent")
        self.reservation.refresh_from_db()
        self.assertIsNotNone(self.reservation.whatsapp_welcome_sent_at)
        mock_send.assert_called_once()
        kwargs = mock_send.call_args.kwargs
        self.assertEqual(kwargs["template_name"], "stay_welcome_hr")
        self.assertEqual(kwargs["body_parameters"][0], "Ana")

    @patch.dict("os.environ", {"D360_API_KEY": TEST_D360_KEY})
    @patch("apps.communications.whatsapp_autocheckin_tasks.send_template_message")
    def test_send_welcome_pl_payload_with_incomplete_config(self, mock_send):
        """Phase 5 E2E: incomplete map still sends stay_welcome_pl + language pl (#1020)."""
        # Config omits pl — old bug chose stay_welcome_en while keeping meta language pl.
        self.integration.set_config_dict(
            {
                "provider": "360dialog",
                "phone_number_id": "1068791909660300",
                "access_token": TEST_D360_KEY,
                "api_base_url": "https://waba-v2.360dialog.io",
                "display_phone_number": "+385911111111",
                "auto_reply": False,
                "whatsapp_templates": {
                    "header_image_url": "https://stay.hr/static/whatsapp-header.png",
                    "welcome": {
                        "hr": "stay_welcome_hr",
                        "en": "stay_welcome_en",
                        "de": "stay_welcome_de",
                        "es": "stay_welcome_es",
                        "fr": "stay_welcome_fr",
                        "it": "stay_welcome_it",
                    },
                },
            }
        )
        self.integration.save()
        self.reservation.booker_country = "PL"
        self.reservation.booker_name = "Jan Kowalski"
        self.reservation.save(
            update_fields=["booker_country", "booker_name", "updated_at"]
        )
        mock_send.return_value = {"messages": [{"id": "wamid.welcome.pl.1"}]}

        result = send_welcome_template_for_reservation(self.reservation)

        self.assertEqual(result["status"], "sent")
        mock_send.assert_called_once()
        kwargs = mock_send.call_args.kwargs
        self.assertEqual(kwargs["template_name"], "stay_welcome_pl")
        self.assertEqual(kwargs["language_code"], "pl")
        self.assertEqual(kwargs["body_parameters"][0], "Jan")

    @patch.dict("os.environ", {"D360_API_KEY": TEST_D360_KEY})
    @patch("apps.communications.whatsapp_autocheckin_tasks.send_template_message")
    def test_dry_run_welcome_pl_payload_with_incomplete_config(self, mock_send):
        """Dry-run also resolves pl via registry when config map omits pl."""
        self.integration.set_config_dict(
            {
                "provider": "360dialog",
                "phone_number_id": "1068791909660300",
                "access_token": TEST_D360_KEY,
                "api_base_url": "https://waba-v2.360dialog.io",
                "auto_reply": False,
                "whatsapp_templates": {
                    "welcome": {
                        "hr": "stay_welcome_hr",
                        "en": "stay_welcome_en",
                        "de": "stay_welcome_de",
                        "es": "stay_welcome_es",
                        "fr": "stay_welcome_fr",
                        "it": "stay_welcome_it",
                    },
                },
            }
        )
        self.integration.save()
        self.reservation.booker_country = "PL"
        self.reservation.save(update_fields=["booker_country", "updated_at"])

        result = send_welcome_template_for_reservation(self.reservation, dry_run=True)

        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["template_name"], "stay_welcome_pl")
        self.assertEqual(result["meta_language"], "pl")
        self.assertEqual(result["language"], "pl")
        mock_send.assert_not_called()
        self.reservation.refresh_from_db()
        self.assertIsNone(self.reservation.whatsapp_welcome_sent_at)
    @patch.dict("os.environ", {"D360_API_KEY": TEST_D360_KEY})
    @patch("apps.communications.whatsapp_autocheckin_tasks.send_template_message")
    @patch("apps.communications.whatsapp_autocheckin_tasks.property_local_now")
    def test_idempotent_second_run(self, mock_now, mock_send):
        mock_now.return_value = datetime(2026, 6, 7, 9, 0, tzinfo=ZAGREB)
        mock_send.return_value = {"messages": [{"id": "wamid.welcome.1"}]}
        send_welcome_template_for_reservation(self.reservation)

        result = run_whatsapp_autocheckin_welcome()
        mock_send.assert_called_once()
        self.assertEqual(result["sent"], 0)

    def test_dry_run_does_not_send(self):
        result = send_welcome_template_for_reservation(self.reservation, dry_run=True)
        self.assertEqual(result["status"], "dry_run")
        self.reservation.refresh_from_db()
        self.assertIsNone(self.reservation.whatsapp_welcome_sent_at)

    @patch("apps.communications.whatsapp_autocheckin_tasks.property_local_now")
    def test_intro_email_due_thirty_minutes_before_welcome(self, mock_now):
        mock_now.return_value = datetime(2026, 6, 7, 7, 30, tzinfo=ZAGREB)
        due = iter_due_autocheckin_intro_emails()
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0].pk, self.reservation.pk)

    @patch("apps.communications.whatsapp_autocheckin_tasks.property_local_now")
    def test_intro_email_not_due_before_window(self, mock_now):
        mock_now.return_value = datetime(2026, 6, 7, 7, 15, tzinfo=ZAGREB)
        due = iter_due_autocheckin_intro_emails()
        self.assertEqual(due, [])

    @patch("apps.communications.whatsapp_autocheckin_tasks.property_local_now")
    def test_intro_email_not_due_at_welcome_time(self, mock_now):
        mock_now.return_value = datetime(2026, 6, 7, 8, 0, tzinfo=ZAGREB)
        due = iter_due_autocheckin_intro_emails()
        self.assertEqual(due, [])

    @patch("apps.communications.guest_message_send.send_guest_text_email")
    @patch("apps.communications.whatsapp_autocheckin_tasks.property_local_now")
    def test_send_intro_email_marks_sent(self, mock_now, mock_email):
        mock_now.return_value = datetime(2026, 6, 7, 7, 45, tzinfo=ZAGREB)
        mock_email.return_value = {"sent": True, "to": "ana@example.com"}

        result = send_autocheckin_intro_email(self.reservation)

        self.assertEqual(result["status"], "sent")
        self.reservation.refresh_from_db()
        self.assertIsNotNone(self.reservation.whatsapp_autocheckin_intro_email_sent_at)
        mock_email.assert_called_once()
        call_kwargs = mock_email.call_args.kwargs
        self.assertIn("body_html", call_kwargs)
        self.assertIn("wa.me", call_kwargs["body_html"])
        body = mock_email.call_args.args[1]
        self.assertIn("wa.me", body)
        self.assertIn("Auto check-in:", body)
        outbound = GuestOutboundMessage.objects.filter(
            reservation=self.reservation,
            status=GuestOutboundMessageStatus.SENT,
        ).first()
        self.assertIsNotNone(outbound)
        self.assertIn("wa.me", outbound.body_text)

    def test_intro_email_html_renders_whatsapp_button(self):
        wa_link = "https://wa.me/385976789626?text=Auto%20check-in"
        body_html = render_autocheckin_whatsapp_intro_email_html(
            self.reservation,
            wa_link=wa_link,
            display_phone="+385976789626",
        )
        self.assertIn(f'href="{wa_link}"', body_html)
        self.assertIn("Auto check-in", body_html)
        self.assertIn("#25D366", body_html)
        self.assertNotIn("<script", body_html.lower())
        self.assertIn(html.escape(self.reservation.booking_code), body_html)

    @patch.dict("os.environ", {"D360_API_KEY": TEST_D360_KEY})
    @patch("apps.communications.whatsapp_autocheckin_tasks.send_template_message")
    def test_skip_welcome_when_guest_engaged(self, mock_send):
        self.reservation.whatsapp_autocheckin_engaged_at = datetime(2026, 6, 7, 7, 0, tzinfo=ZAGREB)
        self.reservation.save(update_fields=["whatsapp_autocheckin_engaged_at", "updated_at"])

        result = send_welcome_template_for_reservation(self.reservation)

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "guest_engaged")
        mock_send.assert_not_called()

    @patch("apps.communications.whatsapp_autocheckin_tasks.property_local_now")
    def test_engaged_excluded_from_due_welcome(self, mock_now):
        mock_now.return_value = datetime(2026, 6, 7, 8, 15, tzinfo=ZAGREB)
        self.reservation.whatsapp_autocheckin_engaged_at = datetime(2026, 6, 7, 7, 0, tzinfo=ZAGREB)
        self.reservation.save(update_fields=["whatsapp_autocheckin_engaged_at", "updated_at"])

        due = iter_due_autocheckin_reservations()
        self.assertEqual(due, [])

    def _create_completed_web_checkin(self, *, created_from: str) -> GuestCheckInSession:
        now = timezone.now()
        return GuestCheckInSession.objects.create(
            tenant_id=self.tenant.pk,
            reservation=self.reservation,
            status=GuestCheckInSessionStatus.COMPLETED,
            opens_at=now - timedelta(days=1),
            expires_at=now + timedelta(days=2),
            completed_at=now,
            created_from=created_from,
        )

    @patch.dict("os.environ", {"D360_API_KEY": TEST_D360_KEY})
    @patch("apps.communications.whatsapp_autocheckin_tasks.send_template_message")
    def test_skip_welcome_when_web_checkin_completed_channex(self, mock_send):
        self._create_completed_web_checkin(created_from=GuestCheckInSessionCreatedFrom.CHANNEX)

        result = send_welcome_template_for_reservation(self.reservation)

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "web_checkin_completed")
        mock_send.assert_not_called()
        self.reservation.refresh_from_db()
        self.assertIsNone(self.reservation.whatsapp_welcome_sent_at)

    @patch.dict("os.environ", {"D360_API_KEY": TEST_D360_KEY})
    @patch("apps.communications.whatsapp_autocheckin_tasks.send_template_message")
    def test_skip_welcome_when_web_checkin_completed_email(self, mock_send):
        self._create_completed_web_checkin(created_from=GuestCheckInSessionCreatedFrom.EMAIL)

        result = send_welcome_template_for_reservation(self.reservation)

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "web_checkin_completed")
        mock_send.assert_not_called()

    @patch("apps.communications.whatsapp_autocheckin_tasks.property_local_now")
    def test_completed_web_checkin_excluded_from_due_welcome(self, mock_now):
        mock_now.return_value = datetime(2026, 6, 7, 8, 15, tzinfo=ZAGREB)
        self._create_completed_web_checkin(created_from=GuestCheckInSessionCreatedFrom.CHANNEX)

        due = iter_due_autocheckin_reservations()
        self.assertEqual(due, [])

    @patch("apps.communications.whatsapp_autocheckin_tasks.property_local_now")
    def test_completed_web_checkin_excluded_from_due_intro(self, mock_now):
        mock_now.return_value = datetime(2026, 6, 7, 7, 30, tzinfo=ZAGREB)
        self._create_completed_web_checkin(created_from=GuestCheckInSessionCreatedFrom.EMAIL)

        due = iter_due_autocheckin_intro_emails()
        self.assertEqual(due, [])

    @patch("apps.communications.guest_message_send.send_guest_text_email")
    def test_skip_intro_email_when_web_checkin_completed(self, mock_email):
        self._create_completed_web_checkin(created_from=GuestCheckInSessionCreatedFrom.CHANNEX)

        result = send_autocheckin_intro_email(self.reservation)

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "web_checkin_completed")
        mock_email.assert_not_called()

    @patch("apps.communications.guest_message_send.send_guest_text_email")
    @patch.dict("os.environ", {"D360_API_KEY": TEST_D360_KEY})
    @patch("apps.communications.whatsapp_autocheckin_tasks.send_template_message")
    @patch("apps.communications.whatsapp_autocheckin_tasks.property_local_now")
    def test_run_sends_intro_then_welcome(self, mock_now, mock_welcome, mock_email):
        mock_now.return_value = datetime(2026, 6, 7, 8, 15, tzinfo=ZAGREB)
        mock_email.return_value = {"sent": True, "to": "ana@example.com"}
        mock_welcome.return_value = {"messages": [{"id": "wamid.welcome.1"}]}

        result = run_whatsapp_autocheckin_welcome()

        self.assertEqual(result["intro_sent"], 0)
        self.assertEqual(result["sent"], 1)
        mock_welcome.assert_called_once()
        mock_email.assert_not_called()


@override_settings(
    STAY_INTEGRATION_FERNET_KEY=TEST_FERNET_KEY,
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class WhatsAppImmediateAutocheckinTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="uzorita", name="Uzorita", default_language="hr")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Uzorita",
            slug="uzorita",
            whatsapp_autocheckin_enabled=True,
            whatsapp_autocheckin_time=time(8, 0),
        )
        self.integration = IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.WHATSAPP,
            routing_key="1068791909660300",
            is_active=True,
        )
        self.integration.set_config_dict(
            {
                "provider": "360dialog",
                "phone_number_id": "1068791909660300",
                "access_token": TEST_D360_KEY,
                "api_base_url": "https://waba-v2.360dialog.io",
                "auto_reply": False,
                "whatsapp_templates": {
                    "welcome": {"hr": "stay_welcome_hr"},
                },
            }
        )
        self.integration.save()
        self.today = date(2026, 6, 7)

    def _create_reservation(self, **overrides):
        defaults = {
            "tenant": self.tenant,
            "property": self.property,
            "booker_name": "Ana Anić",
            "booker_phone": "+385911111111",
            "booking_code": "BCOM-200",
            "check_in": self.today,
            "check_out": self.today + timedelta(days=2),
            "status": Reservation.Status.EXPECTED,
        }
        defaults.update(overrides)
        return Reservation.objects.create(**defaults)

    @patch("apps.communications.whatsapp_autocheckin_tasks.property_local_now")
    def test_eligible_after_autocheckin_time(self, mock_now):
        mock_now.return_value = datetime(2026, 6, 7, 10, 0, tzinfo=ZAGREB)
        reservation = self._create_reservation()
        self.assertTrue(is_immediate_autocheckin_eligible(reservation))

    @patch("apps.communications.whatsapp_autocheckin_tasks.property_local_now")
    def test_not_eligible_before_autocheckin_time(self, mock_now):
        mock_now.return_value = datetime(2026, 6, 7, 7, 0, tzinfo=ZAGREB)
        reservation = self._create_reservation()
        self.assertFalse(is_immediate_autocheckin_eligible(reservation))

    @patch("apps.communications.whatsapp_autocheckin_tasks.property_local_now")
    def test_not_eligible_future_check_in(self, mock_now):
        mock_now.return_value = datetime(2026, 6, 7, 10, 0, tzinfo=ZAGREB)
        reservation = self._create_reservation(check_in=self.today + timedelta(days=1))
        self.assertFalse(is_immediate_autocheckin_eligible(reservation))

    @patch("apps.communications.whatsapp_autocheckin_tasks.property_local_now")
    def test_not_eligible_when_disabled(self, mock_now):
        mock_now.return_value = datetime(2026, 6, 7, 10, 0, tzinfo=ZAGREB)
        self.property.whatsapp_autocheckin_enabled = False
        self.property.save(update_fields=["whatsapp_autocheckin_enabled", "updated_at"])
        reservation = self._create_reservation()
        self.assertFalse(is_immediate_autocheckin_eligible(reservation))

    @patch.dict("os.environ", {"D360_API_KEY": TEST_D360_KEY})
    @patch("apps.communications.whatsapp_autocheckin_tasks.send_template_message")
    @patch("apps.communications.whatsapp_autocheckin_tasks.property_local_now")
    def test_immediate_task_sends_when_eligible(self, mock_now, mock_send):
        mock_now.return_value = datetime(2026, 6, 7, 10, 0, tzinfo=ZAGREB)
        mock_send.return_value = {"messages": [{"id": "wamid.immediate.1"}]}
        reservation = self._create_reservation()

        result = maybe_send_immediate_autocheckin_welcome(reservation.pk)

        self.assertEqual(result["status"], "sent")
        mock_send.assert_called_once()

    @patch("apps.communications.whatsapp_autocheckin_tasks.send_autocheckin_intro_email")
    @patch("apps.communications.whatsapp_autocheckin_tasks.property_local_now")
    def test_immediate_task_does_not_send_intro_email(self, mock_now, mock_intro):
        mock_now.return_value = datetime(2026, 6, 7, 10, 0, tzinfo=ZAGREB)
        reservation = self._create_reservation(check_in=self.today + timedelta(days=30))

        result = maybe_send_immediate_autocheckin_welcome(reservation.pk)

        self.assertEqual(result["status"], "skipped")
        mock_intro.assert_not_called()

    @patch.dict("os.environ", {"D360_API_KEY": TEST_D360_KEY})
    @patch("apps.communications.whatsapp_autocheckin_tasks.send_template_message")
    @patch("apps.communications.whatsapp_autocheckin_tasks.property_local_now")
    def test_immediate_task_skips_before_time(self, mock_now, mock_send):
        mock_now.return_value = datetime(2026, 6, 7, 7, 0, tzinfo=ZAGREB)
        reservation = self._create_reservation()

        result = maybe_send_immediate_autocheckin_welcome(reservation.pk)

        self.assertEqual(result["status"], "skipped")
        mock_send.assert_not_called()

    @patch.dict("os.environ", {"D360_API_KEY": TEST_D360_KEY})
    @patch("apps.communications.whatsapp_autocheckin_tasks.send_template_message")
    @patch("apps.communications.whatsapp_autocheckin_tasks.property_local_now")
    def test_immediate_task_already_sent(self, mock_now, mock_send):
        mock_now.return_value = datetime(2026, 6, 7, 10, 0, tzinfo=ZAGREB)
        reservation = self._create_reservation(
            whatsapp_welcome_sent_at=datetime(2026, 6, 7, 8, 30, tzinfo=ZAGREB),
        )

        result = maybe_send_immediate_autocheckin_welcome(reservation.pk)

        self.assertEqual(result["status"], "already_sent")
        mock_send.assert_not_called()

    @patch("apps.communications.whatsapp_autocheckin_tasks.property_local_now")
    def test_deferred_guest_document_checkin_is_deprecated(self, mock_now):
        from apps.communications.whatsapp_autocheckin_tasks import run_deferred_guest_document_checkins

        mock_now.return_value = datetime(2026, 6, 7, 12, 30, tzinfo=ZAGREB)
        result = run_deferred_guest_document_checkins()
        self.assertEqual(result["status"], "deprecated")
        self.assertEqual(result["completed"], 0)

    @patch("apps.communications.whatsapp_autocheckin_tasks.maybe_send_immediate_autocheckin_welcome")
    @patch("apps.communications.whatsapp_autocheckin_tasks.property_local_now")
    def test_post_save_queues_immediate_task(self, mock_now, mock_task):
        mock_now.return_value = datetime(2026, 6, 7, 10, 0, tzinfo=ZAGREB)
        with self.captureOnCommitCallbacks(execute=True):
            reservation = self._create_reservation()
        mock_task.delay.assert_called_once_with(reservation.pk)

    @patch("apps.communications.whatsapp_autocheckin_tasks.maybe_send_immediate_autocheckin_welcome")
    @patch("apps.communications.whatsapp_autocheckin_tasks.property_local_now")
    def test_post_save_skips_web_pending(self, mock_now, mock_task):
        mock_now.return_value = datetime(2026, 6, 7, 10, 0, tzinfo=ZAGREB)
        with self.captureOnCommitCallbacks(execute=True):
            self._create_reservation(status=Reservation.Status.PENDING, source="api")
        mock_task.delay.assert_not_called()

    @patch("apps.communications.whatsapp_autocheckin_tasks.maybe_send_immediate_autocheckin_welcome")
    @patch("apps.core.tasks.notify_new_reservation")
    @patch("apps.communications.tasks.send_guest_booking_confirmed_email")
    @patch("apps.communications.whatsapp_autocheckin_tasks.property_local_now")
    def test_confirm_web_booking_queues_immediate_task(
        self,
        mock_now,
        mock_email,
        mock_notify,
        mock_task,
    ):
        mock_now.return_value = datetime(2026, 6, 7, 10, 0, tzinfo=ZAGREB)
        reservation = self._create_reservation(status=Reservation.Status.PENDING, source="api")

        self.assertTrue(confirm_web_booking(reservation.pk))
        mock_task.delay.assert_called_once_with(reservation.pk)
