from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from apps.billing.models import Invoice
from apps.communications.invoice_link_distribute import (
    deliver_invoice_link,
    render_invoice_link_message,
    resolve_invoice_delivery_channels,
)
from apps.communications.models import GuestMessageChannel
from apps.properties.models import Property
from apps.reservations.models import Guest, Reservation
from apps.tenants.models import Tenant


class ResolveInvoiceDeliveryChannelsTests(SimpleTestCase):
    def test_whatsapp_and_usable_email(self):
        self.assertEqual(
            resolve_invoice_delivery_channels(
                last_channel=GuestMessageChannel.WHATSAPP,
                usable_email=True,
                booking_available=True,
                whatsapp_session_open=True,
            ),
            [GuestMessageChannel.WHATSAPP, GuestMessageChannel.EMAIL],
        )

    def test_booking_only_without_email(self):
        self.assertEqual(
            resolve_invoice_delivery_channels(
                last_channel=GuestMessageChannel.BOOKING,
                usable_email=False,
                booking_available=True,
                whatsapp_session_open=False,
            ),
            [GuestMessageChannel.BOOKING],
        )

    def test_email_last_sends_once(self):
        self.assertEqual(
            resolve_invoice_delivery_channels(
                last_channel=GuestMessageChannel.EMAIL,
                usable_email=True,
                booking_available=True,
                whatsapp_session_open=True,
            ),
            [GuestMessageChannel.EMAIL],
        )

    def test_relay_email_last_falls_back_to_booking(self):
        self.assertEqual(
            resolve_invoice_delivery_channels(
                last_channel=GuestMessageChannel.EMAIL,
                usable_email=False,
                booking_available=True,
                whatsapp_session_open=True,
            ),
            [GuestMessageChannel.BOOKING],
        )

    def test_whatsapp_closed_falls_back_to_email(self):
        self.assertEqual(
            resolve_invoice_delivery_channels(
                last_channel=GuestMessageChannel.WHATSAPP,
                usable_email=True,
                booking_available=True,
                whatsapp_session_open=False,
            ),
            [GuestMessageChannel.EMAIL],
        )

    def test_no_channel_without_email(self):
        self.assertEqual(
            resolve_invoice_delivery_channels(
                last_channel="",
                usable_email=False,
                booking_available=False,
                whatsapp_session_open=False,
            ),
            [],
        )

    def test_no_last_channel_uses_usable_email(self):
        self.assertEqual(
            resolve_invoice_delivery_channels(
                last_channel="",
                usable_email=True,
                booking_available=True,
                whatsapp_session_open=False,
            ),
            [GuestMessageChannel.EMAIL],
        )


class DeliverInvoiceLinkTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Deliver Tenant", slug="invoice-deliver")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Uzorita B&B",
            slug="uzorita-bb",
            language="hr",
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            check_in=date(2026, 9, 12),
            check_out=date(2026, 9, 13),
            status=Reservation.Status.CHECKED_OUT,
            booker_name="Marcel Droste",
            booker_email="guest@example.com",
            booking_code="6895655754",
            amount=Decimal("142.80"),
        )
        self.invoice = Invoice.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            invoice_number="265-ROOMS-1",
            sequence_number=265,
            issued_at=datetime(2026, 9, 13, 9, 11, 0),
            buyer_name="Marcel Droste",
            payment_method=Invoice.PaymentMethod.BOOKING,
            subtotal=Decimal("126.37"),
            vat_amount=Decimal("16.43"),
            total=Decimal("142.80"),
            jir="test-jir",
            public_access_token="11111111-1111-1111-1111-111111111111",
        )

    def test_skips_without_jir(self):
        self.invoice.jir = ""
        self.invoice.save(update_fields=["jir"])
        result = deliver_invoice_link(self.invoice)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "no_jir")

    def test_skips_relay_email_without_other_channel(self):
        self.reservation.booker_email = "x@guest.booking.com"
        self.reservation.save(update_fields=["booker_email"])
        with (
            patch(
                "apps.communications.invoice_link_distribute.build_message_channels",
                return_value={
                    "reply_channel": GuestMessageChannel.EMAIL,
                    "booking": {"available": False},
                    "whatsapp": {"session_open": False},
                },
            ),
            patch(
                "apps.communications.invoice_link_distribute.send_invoice_email"
            ) as mock_email,
        ):
            result = deliver_invoice_link(self.invoice)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "no_channel")
        mock_email.assert_not_called()

    def test_booking_and_email(self):
        with (
            patch(
                "apps.communications.invoice_link_distribute.build_message_channels",
                return_value={
                    "reply_channel": GuestMessageChannel.BOOKING,
                    "booking": {"available": True},
                    "whatsapp": {"session_open": False},
                },
            ),
            patch(
                "apps.communications.invoice_link_distribute.send_invoice_email",
                return_value={"status": "sent", "invoice_id": self.invoice.pk},
            ) as mock_email,
            patch(
                "apps.communications.invoice_link_distribute.send_guest_message",
            ) as mock_send,
        ):
            mock_send.return_value.status = "sent"
            result = deliver_invoice_link(self.invoice)
        self.assertEqual(result["status"], "sent")
        channels = [row["channel"] for row in result["channels"]]
        self.assertEqual(channels, [GuestMessageChannel.BOOKING, GuestMessageChannel.EMAIL])
        mock_send.assert_called_once()
        mock_email.assert_called_once_with(self.invoice.pk)
        body = mock_send.call_args.kwargs["body_text"]
        self.assertIn("11111111-1111-1111-1111-111111111111", body)
        self.assertNotIn("/pdf/", body)

    def test_skips_when_primary_identity_invented(self):
        Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="Marcel",
            last_name="Droste",
            is_primary=True,
            evisitor_identity_invented_at=datetime(2026, 9, 13, 10, 0, 0),
        )
        with (
            patch(
                "apps.communications.invoice_link_distribute.build_message_channels"
            ) as mock_channels,
            patch(
                "apps.communications.invoice_link_distribute.send_invoice_email"
            ) as mock_email,
        ):
            result = deliver_invoice_link(self.invoice)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "invented_identity")
        mock_channels.assert_not_called()
        mock_email.assert_not_called()

    def test_sends_when_only_secondary_identity_invented(self):
        Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="Marcel",
            last_name="Droste",
            is_primary=True,
        )
        Guest.objects.create(
            tenant=self.tenant,
            reservation=self.reservation,
            first_name="Other",
            last_name="Guest",
            is_primary=False,
            evisitor_identity_invented_at=datetime(2026, 9, 13, 10, 0, 0),
        )
        with (
            patch(
                "apps.communications.invoice_link_distribute.build_message_channels",
                return_value={
                    "reply_channel": GuestMessageChannel.BOOKING,
                    "booking": {"available": True},
                    "whatsapp": {"session_open": False},
                },
            ),
            patch(
                "apps.communications.invoice_link_distribute.send_invoice_email",
                return_value={"status": "sent", "invoice_id": self.invoice.pk},
            ) as mock_email,
            patch(
                "apps.communications.invoice_link_distribute.send_guest_message",
            ) as mock_send,
        ):
            mock_send.return_value.status = "sent"
            result = deliver_invoice_link(self.invoice)
        self.assertEqual(result["status"], "sent")
        mock_email.assert_called_once_with(self.invoice.pk)

    def test_message_uses_portal_url(self):
        text = render_invoice_link_message(self.invoice)
        self.assertIn("/api/v1/public/invoices/11111111-1111-1111-1111-111111111111/", text)
        self.assertNotIn("/pdf/", text)
