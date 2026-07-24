"""Phase 7: live cutover — legacy suppression for CHECKIN_* / WELCOME (ADR 0010)."""

from __future__ import annotations

from datetime import date, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.communications.guest_reminder_service import GuestReminderService
from apps.communications.messaging.schedule_settings import (
    PLATFORM_SCHEDULE_DEFAULTS,
    PREFIX_PRE_ARRIVAL,
    PREFIX_WHATSAPP_WELCOME,
    resolve_schedule,
)
from apps.communications.whatsapp_autocheckin_tasks import (
    maybe_send_immediate_autocheckin_welcome,
    run_whatsapp_autocheckin_welcome,
    send_autocheckin_intro_email,
    send_welcome_template_for_reservation,
)
from apps.properties.models import Property
from apps.reservations.guest_checkin_session import ensure_active_session
from apps.reservations.models import Guest, GuestCheckInSessionCreatedFrom, Reservation
from apps.tenants.models import Tenant


LIVE_UZORITA = dict(
    MESSAGE_ORCHESTRATION_ENABLED=True,
    MESSAGE_ORCHESTRATION_SHADOW=False,
    MESSAGE_ORCHESTRATION_TENANTS=["uzorita"],
    MESSAGE_ORCHESTRATION_PROPERTIES=[],
)

SHADOW_UZORITA = dict(
    MESSAGE_ORCHESTRATION_ENABLED=True,
    MESSAGE_ORCHESTRATION_SHADOW=True,
    MESSAGE_ORCHESTRATION_TENANTS=["uzorita"],
    MESSAGE_ORCHESTRATION_PROPERTIES=[],
)


class MessagingCutoverSuppressionTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Uzorita", slug="uzorita")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Uzorita",
            slug="uzorita",
            whatsapp_autocheckin_enabled=True,
            whatsapp_autocheckin_time=time(11, 15),
            whatsapp_welcome_send_time=time(11, 15),
            guest_checkin_opens_days_before=7,
        )
        self.other_tenant = Tenant.objects.create(name="Other", slug="other")
        self.other_property = Property.objects.create(
            tenant=self.other_tenant,
            name="Other Prop",
            slug="other-prop",
            whatsapp_autocheckin_enabled=True,
            whatsapp_autocheckin_time=time(11, 15),
            guest_checkin_opens_days_before=7,
        )
        today = date(2026, 8, 1)
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="CUT-001",
            check_in=today,
            check_out=today + timedelta(days=2),
            adults_count=1,
            booker_name="Cutover Guest",
            booker_email="cut@example.com",
            booker_phone="+385911111111",
            amount=Decimal("100.00"),
            status=Reservation.Status.EXPECTED,
        )
        self.other_reservation = Reservation.objects.create(
            tenant=self.other_tenant,
            property=self.other_property,
            booking_code="CUT-OTHER",
            check_in=today,
            check_out=today + timedelta(days=2),
            adults_count=1,
            booker_name="Other Guest",
            booker_email="other@example.com",
            booker_phone="+385922222222",
            amount=Decimal("100.00"),
            status=Reservation.Status.EXPECTED,
        )
        Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="Cut",
            last_name="Guest",
            name="Cut Guest",
            is_primary=True,
        )
        Guest.objects.create(
            tenant=self.other_tenant,
            reservation=self.other_reservation,
            first_name="Other",
            last_name="Guest",
            name="Other Guest",
            is_primary=True,
        )
        ensure_active_session(
            self.reservation,
            created_from=GuestCheckInSessionCreatedFrom.EMAIL,
        )

    @override_settings(**LIVE_UZORITA)
    @patch("apps.communications.guest_reminder_service.send_guest_message")
    def test_live_suppresses_pre_arrival_and_d0_reminder(self, mock_send):
        for days_before in (7, 0):
            result = GuestReminderService.send_pre_arrival_reminder(
                self.reservation,
                days_before=days_before,
            )
            self.assertEqual(result["status"], "skipped")
            self.assertEqual(result["reason"], "orchestration_owns_outbound")
        mock_send.assert_not_called()

    @override_settings(**SHADOW_UZORITA)
    @patch("apps.communications.guest_reminder_service.send_guest_message")
    def test_shadow_does_not_suppress_legacy_reminder(self, mock_send):
        mock_send.return_value = None
        with patch(
            "apps.communications.guest_reminder_service.evaluate_session_access",
        ) as mock_access:
            mock_access.return_value = type(
                "Access",
                (),
                {"allowed": True, "gate_status": "open"},
            )()
            with patch(
                "apps.communications.guest_reminder_service.build_message_channels",
                return_value={
                    "email": {"available": True},
                    "whatsapp": {"available": False},
                    "booking": {"available": False},
                },
            ):
                result = GuestReminderService.send_pre_arrival_reminder(
                    self.reservation,
                    days_before=7,
                )
        self.assertNotEqual(result.get("reason"), "orchestration_owns_outbound")
        self.assertIn(result["status"], {"sent", "queued"}, result)
        mock_send.assert_called_once()

    @override_settings(**LIVE_UZORITA)
    @patch("apps.communications.guest_reminder_service.send_guest_message")
    def test_live_non_allowlisted_still_sends_reminder(self, mock_send):
        mock_send.return_value = None
        ensure_active_session(
            self.other_reservation,
            created_from=GuestCheckInSessionCreatedFrom.EMAIL,
        )
        with patch(
            "apps.communications.guest_reminder_service.evaluate_session_access",
        ) as mock_access:
            mock_access.return_value = type(
                "Access",
                (),
                {"allowed": True, "gate_status": "open"},
            )()
            with patch(
                "apps.communications.guest_reminder_service.build_message_channels",
                return_value={
                    "email": {"available": True},
                    "whatsapp": {"available": False},
                    "booking": {"available": False},
                },
            ):
                result = GuestReminderService.send_pre_arrival_reminder(
                    self.other_reservation,
                    days_before=0,
                )
        self.assertNotEqual(result.get("reason"), "orchestration_owns_outbound")
        self.assertIn(result["status"], {"sent", "queued"}, result)
        mock_send.assert_called_once()

    @override_settings(**LIVE_UZORITA)
    @patch(
        "apps.communications.whatsapp_autocheckin_tasks.send_welcome_template_for_reservation"
    )
    def test_live_suppresses_immediate_welcome(self, mock_send):
        result = maybe_send_immediate_autocheckin_welcome(self.reservation.pk)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "orchestration_owns_outbound")
        mock_send.assert_not_called()

    @override_settings(**LIVE_UZORITA)
    def test_live_suppresses_intro_email(self):
        result = send_autocheckin_intro_email(self.reservation)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "orchestration_owns_outbound")

    @override_settings(**LIVE_UZORITA)
    @patch(
        "apps.communications.whatsapp_autocheckin_tasks.send_welcome_template_for_reservation"
    )
    @patch(
        "apps.communications.whatsapp_autocheckin_tasks.send_autocheckin_intro_email"
    )
    @patch(
        "apps.communications.whatsapp_autocheckin_tasks.iter_due_autocheckin_reservations"
    )
    @patch(
        "apps.communications.whatsapp_autocheckin_tasks.iter_due_autocheckin_intro_emails"
    )
    @patch(
        "apps.integrations.whatsapp.autocheckin_docs_deadline."
        "mark_autocheckin_session_lost_for_due_reservations",
        return_value={"marked": 0},
    )
    def test_legacy_welcome_task_skips_allowlisted(
        self,
        _mock_session_lost,
        mock_intro_iter,
        mock_welcome_iter,
        mock_intro_send,
        mock_welcome_send,
    ):
        mock_intro_iter.return_value = [self.reservation]
        mock_welcome_iter.return_value = [self.reservation]
        result = run_whatsapp_autocheckin_welcome()
        self.assertEqual(result["suppressed"], 2)
        mock_intro_send.assert_not_called()
        mock_welcome_send.assert_not_called()

    @override_settings(**LIVE_UZORITA)
    @patch(
        "apps.communications.whatsapp_autocheckin_tasks.send_template_message"
    )
    def test_engine_welcome_primitive_still_callable(self, mock_template):
        """Provider adapter must still reach send_welcome_template_for_reservation."""
        mock_template.return_value = {"messages": [{"id": "wamid.test"}]}
        with patch(
            "apps.communications.whatsapp_autocheckin_tasks.resolve_whatsapp_integration"
        ) as mock_resolve:
            mock_resolve.return_value = (None, None)
            result = send_welcome_template_for_reservation(self.reservation)
        # Missing credentials → skipped, but not orchestration suppression.
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "no_credentials")


class UzoritaScheduleConfirmationTests(TestCase):
    """Platform defaults + property resolve match cutover clocks (ADR Phase 7)."""

    def test_platform_defaults_match_cutover(self):
        pre = PLATFORM_SCHEDULE_DEFAULTS[PREFIX_PRE_ARRIVAL]
        wel = PLATFORM_SCHEDULE_DEFAULTS[PREFIX_WHATSAPP_WELCOME]
        self.assertEqual(pre.days_before, 7)
        self.assertEqual(pre.send_time, time(9, 0))
        self.assertEqual(pre.schedule_strategy, "FIXED_TIME")
        self.assertEqual(wel.days_before, 0)
        self.assertEqual(wel.send_time, time(11, 15))
        self.assertEqual(wel.schedule_strategy, "FIXED_TIME")

    def test_uzorita_with_aligned_welcome_time(self):
        tenant = Tenant.objects.create(name="Uzorita", slug="uzorita")
        prop = Property.objects.create(
            tenant=tenant,
            name="Uzorita",
            slug="uzorita",
            whatsapp_autocheckin_enabled=True,
            whatsapp_autocheckin_time=time(11, 15),
            whatsapp_welcome_send_time=time(11, 15),
        )
        pre = resolve_schedule(property=prop, prefixes=("pre_arrival",))
        wel = resolve_schedule(
            property=prop,
            prefixes=("whatsapp_welcome", "welcome"),
        )
        self.assertEqual(pre.days_before, 7)
        self.assertEqual(pre.send_time, time(9, 0))
        self.assertEqual(pre.schedule_strategy, "FIXED_TIME")
        self.assertEqual(wel.days_before, 0)
        self.assertEqual(wel.send_time, time(11, 15))
        self.assertEqual(wel.schedule_strategy, "FIXED_TIME")
