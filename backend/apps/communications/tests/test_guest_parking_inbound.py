from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from apps.communications.guest_parking_inbound import maybe_handle_guest_parking_inbound
from apps.communications.guest_parking_patterns import classify_parking_only
from apps.communications.models import GuestMessageDraft
from apps.properties.guest_info import (
    merge_parking_into_guest_info,
    normalize_guest_info,
    render_parking_reply_text,
)
from apps.properties.models import Property
from apps.properties.uzorita_guest_info import UZORITA_GUEST_INFO
from apps.reservations.models import Reservation
from apps.tenants.models import Tenant


class GuestParkingPatternTests(TestCase):
    def test_parking_only(self):
        self.assertTrue(classify_parking_only("Gdje je parking?"))
        self.assertFalse(classify_parking_only("We arrive at 8 PM"))

    def test_mixed_deferred_to_arrival(self):
        self.assertFalse(
            classify_parking_only("We need parking and arrive at 8 PM"),
        )


class GuestParkingInboundTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="parking", name="Parking")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Test",
            slug="test",
            guest_info=merge_parking_into_guest_info(
                {},
                has_private=True,
                zone_label="Zone B",
                price_per_day=Decimal("0"),
                custom_hr="Ispred objekta.",
                custom_en="In front of the property.",
            ),
            guest_parking_auto_reply_enabled=True,
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            check_in=date(2026, 8, 4),
            check_out=date(2026, 8, 5),
            status=Reservation.Status.EXPECTED,
            booker_name="Olga Test",
        )

    @patch("apps.communications.guest_parking_inbound.llm_configured", return_value=False)
    @patch("apps.communications.guest_parking_inbound.send_guest_message")
    def test_parking_auto_reply_booking_channel(self, mock_send, _mock_llm):
        result = maybe_handle_guest_parking_inbound(
            self.reservation,
            "Do you have free parking?",
            channel="booking",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "guest_parking_handled")
        mock_send.assert_called_once()
        draft = GuestMessageDraft.objects.filter(reservation=self.reservation).first()
        self.assertIsNotNone(draft)
        self.assertIn("parking", draft.final_body_text.lower())

    @patch("apps.communications.guest_parking_inbound.llm_configured", return_value=False)
    @patch("apps.communications.guest_parking_inbound.send_guest_message")
    def test_toggle_off_skips(self, mock_send, _mock_llm):
        self.property.guest_parking_auto_reply_enabled = False
        self.property.save(update_fields=["guest_parking_auto_reply_enabled", "updated_at"])
        result = maybe_handle_guest_parking_inbound(
            self.reservation,
            "Parking?",
            channel="email",
        )
        self.assertIsNone(result)
        mock_send.assert_not_called()

    @patch("apps.communications.guest_parking_inbound.llm_configured", return_value=False)
    @patch("apps.communications.guest_parking_inbound.send_guest_message")
    def test_dedup_same_day(self, mock_send, _mock_llm):
        maybe_handle_guest_parking_inbound(
            self.reservation,
            "Where is parking?",
            channel="email",
        )
        result = maybe_handle_guest_parking_inbound(
            self.reservation,
            "Parking again?",
            channel="email",
        )
        self.assertEqual(result["reply"]["status"], "dedup_skipped")
        self.assertEqual(mock_send.call_count, 1)


class GuestParkingLlmInboundTests(TestCase):
    """LLM path: the final language owns the body, the wrapper owns boilerplate."""

    def setUp(self):
        self.tenant = Tenant.objects.create(slug="parking-llm", name="Parking LLM")
        self.property = Property.objects.create(
            tenant=self.tenant,
            name="Uzorita B&B",
            slug="parking-llm",
            language="hr",
            guest_info=merge_parking_into_guest_info(
                {},
                has_private=True,
                zone_label="cijela zona (besplatno)",
                price_per_day=Decimal("0"),
                custom_hr="Možete parkirati ispred restorana.",
                custom_en="You may park in front of the restaurant.",
            ),
            guest_parking_auto_reply_enabled=True,
        )
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            check_in=date(2026, 8, 4),
            check_out=date(2026, 8, 5),
            status=Reservation.Status.EXPECTED,
            booker_name="Antoine Dupont",
        )

    @staticmethod
    def _sent_body(mock_send):
        return mock_send.call_args.kwargs["body_text"]

    @patch("apps.communications.guest_parking_inbound.llm_configured", return_value=True)
    @patch("apps.communications.guest_parking_inbound.send_guest_message")
    @patch("apps.communications.guest_parking_llm.complete_chat_json")
    def test_english_message_with_foreign_filler_word_is_answered_in_english(
        self,
        mock_chat,
        mock_send,
        _llm_on,
    ):
        """Regression for reservation 1137.

        Guest writes English with one Croatian filler word, the model claims `hr`
        and answers in Croatian. The Croatian body must not be sent.
        """
        mock_chat.return_value = {
            "is_parking_related": True,
            "reply_language": "hr",
            "reply_text": (
                "Pozdrav!\n\n"
                "Imamo privatni parking na licu mjesta.\n\n"
                "Lijep pozdrav,\n"
                "Uzorita B&B\n\n"
                "Managed by stay.hr — https://stay.hr/"
            ),
        }

        result = maybe_handle_guest_parking_inbound(
            self.reservation,
            "Hello, is it also possible to have a parking spot molimteh ? 😁",
            channel="booking",
        )

        self.assertEqual(result["status"], "guest_parking_handled")
        body = self._sent_body(mock_send)

        draft = GuestMessageDraft.objects.get(reservation=self.reservation)
        self.assertEqual(draft.language, "en")

        self.assertIn("Hi Antoine!", body)
        self.assertIn("Best regards,", body)
        self.assertIn("You may park in front of the restaurant.", body)
        self.assertIn("private on-site parking is available", body)

        self.assertNotIn("Imamo privatni parking", body)
        self.assertNotIn("Lijep pozdrav", body)
        self.assertNotIn("Bok Antoine", body)
        self.assertEqual(body.count("Managed by stay.hr — https://stay.hr/"), 1)

    @patch("apps.communications.guest_parking_inbound.llm_configured", return_value=True)
    @patch("apps.communications.guest_parking_inbound.send_guest_message")
    @patch("apps.communications.guest_parking_llm.complete_chat_json")
    def test_matching_language_uses_sanitized_llm_body(self, mock_chat, mock_send, _llm_on):
        mock_chat.return_value = {
            "is_parking_related": True,
            "reply_language": "en",
            "reply_text": (
                "Hi Antoine!\n\n"
                "Private parking is available on site, free of charge.\n\n"
                "Best regards,\n"
                "Uzorita B&B\n\n"
                "Managed by stay.hr — https://stay.hr/"
            ),
        }

        maybe_handle_guest_parking_inbound(
            self.reservation,
            "Hello, is it also possible to have a parking spot please?",
            channel="booking",
        )

        body = self._sent_body(mock_send)
        self.assertIn("Private parking is available on site, free of charge.", body)
        self.assertEqual(body.count("Hi Antoine!"), 1)
        self.assertEqual(body.count("Best regards,"), 1)
        self.assertEqual(body.count("Uzorita B&B"), 1)
        self.assertEqual(body.count("Managed by stay.hr — https://stay.hr/"), 1)

    @patch("apps.communications.guest_parking_inbound.llm_configured", return_value=True)
    @patch("apps.communications.guest_parking_inbound.send_guest_message")
    @patch("apps.communications.guest_parking_llm.complete_chat_json")
    def test_body_of_pure_boilerplate_falls_back_to_template(self, mock_chat, mock_send, _llm_on):
        mock_chat.return_value = {
            "is_parking_related": True,
            "reply_language": "en",
            "reply_text": "Hi Antoine!\n\nBest regards,\nUzorita B&B\n\nManaged by stay.hr",
        }

        result = maybe_handle_guest_parking_inbound(
            self.reservation,
            "Hello, is it also possible to have a parking spot please?",
            channel="booking",
        )

        self.assertEqual(result["status"], "guest_parking_handled")
        self.assertFalse(result["used_llm"])
        body = self._sent_body(mock_send)
        self.assertIn("private on-site parking is available", body)
        self.assertEqual(body.count("Managed by stay.hr — https://stay.hr/"), 1)


class GuestInfoParkingTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="pinfo", name="Pinfo")
        self.property = Property.objects.create(
            tenant=self.tenant,
            slug="pinfo",
            name="Pinfo",
        )

    def test_normalize_uzorita_parking_facts(self):
        normalized = normalize_guest_info(UZORITA_GUEST_INFO)
        parking = normalized["facts"]["parking"]
        self.assertTrue(parking.get("has_private"))
        self.assertEqual(parking.get("price_per_day"), "0")

    def test_render_free_parking_en(self):
        self.property.guest_info = merge_parking_into_guest_info(
            {},
            has_private=True,
            zone_label="City center",
            price_per_day=Decimal("0"),
            custom_en="Park in front of the restaurant.",
        )
        text = render_parking_reply_text(self.property, "en")
        self.assertIn("free", text.lower())
        self.assertIn("City center", text)

    def test_render_priced_parking_hr(self):
        self.property.guest_info = merge_parking_into_guest_info(
            {},
            zone_label="Zona A",
            price_per_day=Decimal("5.00"),
            currency="EUR",
        )
        text = render_parking_reply_text(self.property, "hr")
        self.assertIn("5.00", text)
        self.assertIn("EUR", text)

    def test_reservation_notes_prefix(self):
        self.property.guest_info = merge_parking_into_guest_info(
            {},
            price_per_day=Decimal("0"),
        )
        text = render_parking_reply_text(
            self.property,
            "en",
            reservation_notes="Guest would like free parking",
        )
        self.assertTrue(text.startswith("We see you requested parking"))
