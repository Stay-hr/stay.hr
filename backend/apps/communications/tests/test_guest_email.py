from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet
from django.test import TestCase, override_settings

from apps.communications.guest_email import (
    _email_context,
    send_booking_canceled_email,
    send_booking_confirmed_email,
    should_send_guest_canceled_email,
)
from apps.properties.models import Property
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant, TenantReceptionSettings
from apps.tenants.smtp import smtp_host_for_email

TEST_FERNET_KEY = Fernet.generate_key().decode()


@override_settings(STAY_INTEGRATION_FERNET_KEY=TEST_FERNET_KEY)
class GuestEmailStayContextTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Uzorita", slug="uzorita-ctx", default_language="hr")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Uzorita",
            slug="uzorita",
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="CTX01",
            check_in=date(2026, 6, 6),
            check_out=date(2026, 6, 8),
            status=Reservation.Status.EXPECTED,
            booker_name="Guest",
            booker_email="guest@example.com",
            amount=Decimal("100.00"),
        )

    def test_email_context_includes_property_times(self):
        ctx = _email_context(self.reservation)
        self.assertEqual(ctx["check_in_display"], "2026-06-06 od 15:00")
        self.assertEqual(ctx["check_out_display"], "2026-06-08 do 11:00")
        self.assertEqual(ctx["check_in_time"], "15:00")
        self.assertEqual(ctx["check_out_time"], "11:00")


@override_settings(STAY_INTEGRATION_FERNET_KEY=TEST_FERNET_KEY)
class SmtpHostForEmailTests(TestCase):
    def test_uses_central_stay_smtp_host(self):
        self.assertEqual(smtp_host_for_email("room_reservations@uzorita.hr"), "mail.stay.hr")

    def test_empty_for_invalid(self):
        self.assertEqual(smtp_host_for_email(""), "")
        self.assertEqual(smtp_host_for_email("invalid"), "")


@override_settings(STAY_INTEGRATION_FERNET_KEY=TEST_FERNET_KEY)
class TenantReceptionSettingsSmtpPasswordTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Uzorita", slug="uzorita")
        self.settings_row = TenantReceptionSettings.objects.create(tenant=self.tenant)

    def test_password_roundtrip(self):
        self.settings_row.set_guest_smtp_password("Uzorita.2026")
        self.settings_row.save()
        self.settings_row.refresh_from_db()
        self.assertTrue(self.settings_row.has_guest_smtp_password)
        self.assertEqual(self.settings_row.get_guest_smtp_password(), "Uzorita.2026")

    def test_clear_password(self):
        self.settings_row.set_guest_smtp_password("secret")
        self.settings_row.set_guest_smtp_password("")
        self.assertFalse(self.settings_row.has_guest_smtp_password)


@override_settings(
    STAY_INTEGRATION_FERNET_KEY=TEST_FERNET_KEY,
    EMAIL_PORT=587,
    EMAIL_USE_TLS=True,
    EMAIL_USE_SSL=False,
)
class GuestEmailSmtpConnectionTests(TestCase):
    def setUp(self):
        notify_patcher = patch("apps.core.tasks.notify_new_reservation.delay")
        block_patcher = patch(
            "apps.integrations.channel_manager.tasks.sync_reservation_outbound_task.delay"
        )
        self.addCleanup(notify_patcher.stop)
        self.addCleanup(block_patcher.stop)
        notify_patcher.start()
        block_patcher.start()
        self.tenant = Tenant.objects.create(name="Uzorita", slug="uzorita")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Uzorita",
            slug="uzorita",
        )
        self.settings_row = TenantReceptionSettings.objects.create(
            tenant=self.tenant,
            guest_contact_email="room_reservations@uzorita.hr",
            guest_contact_name="Uzorita Luxury b&b",
        )
        self.settings_row.set_guest_smtp_password("Uzorita.2026")
        self.settings_row.save()
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="SMTPTEST1",
            check_in=date(2026, 10, 4),
            check_out=date(2026, 10, 6),
            status=Reservation.Status.EXPECTED,
            booker_name="Guest Test",
            booker_email="guest@example.com",
            amount=Decimal("100.00"),
        )

    @patch("apps.communications.guest_email.get_connection")
    def test_uses_tenant_smtp_credentials(self, mock_get_connection):
        mock_connection = MagicMock()
        mock_get_connection.return_value = mock_connection

        result = send_booking_confirmed_email(self.reservation.pk)

        self.assertTrue(result["sent"])
        mock_get_connection.assert_called_once_with(
            host="mail.stay.hr",
            port=587,
            username="room_reservations@uzorita.hr",
            password="Uzorita.2026",
            use_tls=True,
            use_ssl=False,
        )
        mock_connection.send_messages.assert_called_once()


@override_settings(STAY_INTEGRATION_FERNET_KEY=TEST_FERNET_KEY)
class GuestCanceledEmailPolicyTests(TestCase):
    def test_skips_repeat_cancel_and_refused(self):
        self.assertFalse(should_send_guest_canceled_email(Reservation.Status.CANCELED))
        self.assertFalse(should_send_guest_canceled_email(Reservation.Status.REFUSED))

    def test_allows_from_expected(self):
        self.assertTrue(should_send_guest_canceled_email(Reservation.Status.EXPECTED))


@override_settings(
    STAY_INTEGRATION_FERNET_KEY=TEST_FERNET_KEY,
    EMAIL_PORT=587,
    EMAIL_USE_TLS=True,
    EMAIL_USE_SSL=False,
)
class GuestEmailCanceledTemplateTests(TestCase):
    def setUp(self):
        notify_patcher = patch("apps.core.tasks.notify_new_reservation.delay")
        block_patcher = patch(
            "apps.integrations.channel_manager.tasks.sync_reservation_outbound_task.delay"
        )
        self.addCleanup(notify_patcher.stop)
        self.addCleanup(block_patcher.stop)
        notify_patcher.start()
        block_patcher.start()

        self.tenant = Tenant.objects.create(name="Uzorita", slug="uzorita-cancel", default_language="hr")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Uzorita",
            slug="uzorita",
        )
        self.settings_row = TenantReceptionSettings.objects.create(
            tenant=self.tenant,
            guest_contact_email="room_reservations@uzorita.hr",
            guest_contact_name="Uzorita Luxury b&b",
        )
        self.settings_row.set_guest_smtp_password("Uzorita.2026")
        self.settings_row.save()
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booking_code="CANCEL01",
            check_in=date(2026, 10, 4),
            check_out=date(2026, 10, 6),
            status=Reservation.Status.CANCELED,
            booker_name="Guest Test",
            booker_email="guest@example.com",
            amount=Decimal("100.00"),
        )

    @patch("apps.communications.guest_email.get_connection")
    def test_uses_canceled_template(self, mock_get_connection):
        mock_connection = MagicMock()
        mock_get_connection.return_value = mock_connection

        result = send_booking_canceled_email(self.reservation.pk)

        self.assertTrue(result["sent"])
        sent_message = mock_connection.send_messages.call_args[0][0][0]
        self.assertIn("otkazana", sent_message.body.lower())
