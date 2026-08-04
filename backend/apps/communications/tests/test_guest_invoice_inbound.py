"""Invoice email capture: quality helpers, inbound ask/capture, recipient resolution."""

from datetime import date, timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.communications.guest_email_quality import (
    extract_usable_invoice_emails,
    is_ota_relay_email,
    is_usable_invoice_email,
)
from apps.communications.guest_invoice_inbound import maybe_handle_guest_invoice_inbound
from apps.communications.guest_invoice_patterns import guest_message_requests_invoice
from apps.communications.invoice_email import resolve_invoice_recipient
from apps.communications.invoice_email_capture import (
    InvoiceEmailCaptureService,
    start_waiting_for_invoice_email,
)
from apps.communications.models import GuestMessageDraft
from apps.properties.models import Property
from apps.reservations.models import Guest, Reservation
from apps.tenants.models import Tenant


class GuestEmailQualityTests(TestCase):
    def test_relay_and_usable(self):
        self.assertTrue(is_ota_relay_email("bfouqu.690243@guest.booking.com"))
        self.assertFalse(is_usable_invoice_email("bfouqu.690243@guest.booking.com"))
        self.assertTrue(is_usable_invoice_email("reissitua@gmail.com"))
        self.assertFalse(is_usable_invoice_email(""))
        self.assertFalse(is_usable_invoice_email(None))

    def test_extract_usable(self):
        emails = extract_usable_invoice_emails(
            "bonjour,\nreissitua@gmail.com\nand a@guest.booking.com"
        )
        self.assertEqual(emails, ["reissitua@gmail.com"])


class GuestInvoicePatternTests(TestCase):
    def test_invoice_phrases(self):
        self.assertTrue(
            guest_message_requests_invoice(
                "Bonjour, je souhaiterais qu’une facture soit envoyée à mon adresse e-mail"
            )
        )
        self.assertTrue(guest_message_requests_invoice("Please send invoice"))
        self.assertTrue(guest_message_requests_invoice("Račun molim"))
        self.assertFalse(guest_message_requests_invoice("What time is check-in?"))


class GuestInvoiceInboundTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="inv", name="Invoice")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Uzorita",
            slug="uzorita",
            guest_invoice_auto_reply_enabled=True,
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            booker_name="Benjamin Fouqueau",
            booker_email="bfouqu.690243@guest.booking.com",
            check_in=date(2026, 8, 21),
            check_out=date(2026, 8, 22),
            status=Reservation.Status.EXPECTED,
        )
        self.guest = Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            name="Benjamin Fouqueau",
            first_name="Benjamin",
            last_name="Fouqueau",
            email="bfouqu.690243@guest.booking.com",
            is_primary=True,
        )

    @patch("apps.communications.guest_invoice_inbound.send_guest_message")
    def test_1077_relay_ask_then_gmail_update(self, mock_send):
        ask = maybe_handle_guest_invoice_inbound(
            self.reservation,
            "Bonjour, je souhaiterais qu’une facture soit envoyée à mon adresse e-mail au moment du départ. Merci!",
            channel="booking",
        )
        self.assertIsNotNone(ask)
        self.reservation.refresh_from_db()
        self.assertIsNotNone(self.reservation.invoice_email_waiting_at)
        self.assertTrue(
            GuestMessageDraft.objects.filter(
                reservation=self.reservation,
                hint="invoice auto reply:ask_email",
            ).exists()
        )

        capture = maybe_handle_guest_invoice_inbound(
            self.reservation,
            "bonjour,\nreissitua@gmail.com",
            channel="booking",
        )
        self.assertEqual(capture["capture"]["status"], "updated")
        self.reservation.refresh_from_db()
        self.guest.refresh_from_db()
        self.assertIsNone(self.reservation.invoice_email_waiting_at)
        self.assertEqual(self.reservation.booker_email, "reissitua@gmail.com")
        self.assertEqual(self.guest.email, "reissitua@gmail.com")
        self.assertEqual(
            resolve_invoice_recipient(self.reservation),
            "reissitua@gmail.com",
        )

    def test_resolve_skips_relay(self):
        self.assertIsNone(resolve_invoice_recipient(self.reservation))

    @patch("apps.communications.guest_invoice_inbound.send_guest_message")
    def test_no_overwrite_when_not_waiting(self, mock_send):
        self.reservation.booker_email = "ivan@gmail.com"
        self.reservation.invoice_email_waiting_at = None
        self.reservation.save(update_fields=["booker_email", "invoice_email_waiting_at", "updated_at"])
        self.guest.email = "ivan@gmail.com"
        self.guest.save(update_fields=["email", "updated_at"])

        result = maybe_handle_guest_invoice_inbound(
            self.reservation,
            "See you soon ana@other.com",
            channel="booking",
        )
        self.assertIsNone(result)
        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.booker_email, "ivan@gmail.com")

    @patch("apps.communications.guest_invoice_inbound.send_guest_message")
    def test_ambiguous_emails_while_waiting(self, mock_send):
        start_waiting_for_invoice_email(self.reservation)
        result = maybe_handle_guest_invoice_inbound(
            self.reservation,
            "contact me:\nana@gmail.com\naccounting@firma.hr",
            channel="booking",
        )
        self.assertEqual(result["capture"]["status"], "ambiguous")
        self.reservation.refresh_from_db()
        self.assertIsNotNone(self.reservation.invoice_email_waiting_at)
        self.assertEqual(
            self.reservation.booker_email,
            "bfouqu.690243@guest.booking.com",
        )

    @override_settings(INVOICE_EMAIL_WAITING_TIMEOUT_DAYS=14)
    def test_timeout_clears_waiting(self):
        self.reservation.invoice_email_waiting_at = timezone.now() - timedelta(days=15)
        self.reservation.save(update_fields=["invoice_email_waiting_at", "updated_at"])
        result = maybe_handle_guest_invoice_inbound(
            self.reservation,
            "hello",
            channel="booking",
        )
        self.assertIsNone(result)
        self.reservation.refresh_from_db()
        self.assertIsNone(self.reservation.invoice_email_waiting_at)

    @patch("apps.communications.guest_invoice_inbound.send_guest_message")
    def test_flag_off(self, mock_send):
        self.property.guest_invoice_auto_reply_enabled = False
        self.property.save(update_fields=["guest_invoice_auto_reply_enabled", "updated_at"])
        result = maybe_handle_guest_invoice_inbound(
            self.reservation,
            "Please send invoice",
            channel="booking",
        )
        self.assertIsNone(result)
        mock_send.assert_not_called()

    def test_received_only_for_single_email(self):
        start_waiting_for_invoice_email(self.reservation)
        with self.assertLogs(
            "apps.communications.invoice_email_capture", level="INFO"
        ) as logs:
            InvoiceEmailCaptureService.try_capture_while_waiting(
                self.reservation,
                "a@gmail.com b@gmail.com",
            )
        joined = "\n".join(logs.output)
        self.assertIn("invoice_email_ambiguous", joined)
        self.assertNotIn("invoice_email_received", joined)
