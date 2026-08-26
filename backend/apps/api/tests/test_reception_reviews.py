from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.integrations.channex.booking_service import channex_external_id
from apps.integrations.models import ChannexReview, IntegrationConfig
from apps.properties.models import Property, Unit
from apps.reservations.models import Reservation
from apps.tenants.models import ChannelManager, Tenant, TenantMembership, TenantReceptionSettings

User = get_user_model()


class ReceptionReviewsTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="demo", name="Demo")
        TenantReceptionSettings.objects.create(
            tenant=self.tenant,
            channel_manager=ChannelManager.CHANNEX,
        )
        self.property = Property.objects.create(
            tenant=self.tenant,
            slug="channex-demo",
            name="Demo",
            timezone="Europe/Zagreb",
        )
        self.unit = Unit.objects.create(
            tenant=self.tenant,
            property=self.property,
            code="BCOM-STUDIO",
            name="Studio",
        )
        self.integration = IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.CHANNEX,
            is_active=True,
        )
        self.integration.set_config_dict(
            {
                "property_id": "prop-id",
                "sync_property_slug": "channex-demo",
            }
        )
        self.integration.save()
        self.booking_id = "channex-booking-1"
        self.reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            external_id=channex_external_id(self.booking_id),
            import_source="channex",
            booking_code="5307026805",
            check_in=date(2026, 6, 20),
            check_out=date(2026, 6, 21),
            booker_name="Guest Test",
            status=Reservation.Status.EXPECTED,
        )
        self.review = ChannexReview.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            reservation=self.reservation,
            channex_review_id="review-uuid-1",
            channex_booking_id=self.booking_id,
            ota="BookingCom",
            content="Excellent stay",
            overall_score=Decimal("9.5"),
            is_replied=False,
            received_at=timezone.now(),
            expired_at=timezone.now() + timedelta(days=30),
        )

        self.staff = User.objects.create_user(
            username="evan",
            password="secret-pass",
            is_staff=True,
        )
        TenantMembership.objects.create(user=self.staff, tenant=self.tenant)
        self.client = APIClient()

    def _login(self):
        response = self.client.post(
            "/api/v1/auth/reception-login/",
            {"username": "evan", "password": "secret-pass"},
            format="json",
            HTTP_HOST="app.stay.hr",
        )
        self.assertEqual(response.status_code, 200)

    def test_list_reviews(self):
        self._login()
        response = self.client.get(
            "/api/v1/reception/reviews/?sync=0",
            HTTP_HOST="app.stay.hr",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(len(data["reviews"]), 1)
        self.assertEqual(data["reviews"][0]["content"], "Excellent stay")
        self.assertEqual(data["reviews"][0]["reservation_ref"], "5307026805")
        self.assertTrue(data["reviews"][0]["reservation_linkable"])

    def test_list_reviews_relinks_unlinked_review(self):
        unlinked = ChannexReview.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            reservation=None,
            channex_review_id="review-uuid-unlinked",
            ota="BookingCom",
            ota_reservation_id="5307026805",
            content="Late-linked review",
            overall_score=Decimal("8.0"),
            is_replied=False,
            received_at=timezone.now(),
        )
        self.assertIsNone(unlinked.reservation_id)

        self._login()
        response = self.client.get(
            "/api/v1/reception/reviews/?sync=0",
            HTTP_HOST="app.stay.hr",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        unlinked_payload = next(
            item for item in data["reviews"] if item["id"] == unlinked.pk
        )
        self.assertEqual(unlinked_payload["reservation_id"], self.reservation.pk)
        self.assertTrue(unlinked_payload["reservation_linkable"])
        unlinked.refresh_from_db()
        self.assertEqual(unlinked.reservation_id, self.reservation.pk)

    def test_reply_dict_upsert_normalization(self):
        from apps.integrations.channex.review_service import upsert_channex_review_from_payload

        row, created, _ = upsert_channex_review_from_payload(
            tenant=self.tenant,
            integration=self.integration,
            payload={
                "id": "review-uuid-dict-reply",
                "ota": "BookingCom",
                "reply": {"reply": "Hvala na boravku"},
                "is_replied": False,
            },
        )
        self.assertTrue(created)
        self.assertEqual(row.reply, "Hvala na boravku")
        self.assertTrue(row.is_replied)

        self._login()
        response = self.client.get(
            f"/api/v1/reception/reviews/{row.pk}/?sync=0",
            HTTP_HOST="app.stay.hr",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["reply"], "Hvala na boravku")
        self.assertTrue(data["is_replied"])

    def test_reply_legacy_string_repair(self):
        from apps.integrations.channex.review_service import repair_channex_review_replies

        row = ChannexReview.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            reservation=self.reservation,
            channex_review_id="review-uuid-legacy-reply",
            ota="BookingCom",
            reply="{'reply': 'Stari format odgovora'}",
            is_replied=False,
            received_at=timezone.now(),
        )

        repaired = repair_channex_review_replies(self.tenant)
        self.assertEqual(repaired, 1)
        row.refresh_from_db()
        self.assertEqual(row.reply, "Stari format odgovora")
        self.assertTrue(row.is_replied)
        self.assertIsNone(row.reply_sent_at)

    def test_review_links_by_ota_reservation_id(self):
        from apps.integrations.channex.review_service import upsert_channex_review_from_payload

        row, created, _ = upsert_channex_review_from_payload(
            tenant=self.tenant,
            integration=self.integration,
            payload={
                "id": "review-uuid-2",
                "ota": "BookingCom",
                "ota_reservation_id": "5307026805",
                "content": "Great stay",
            },
        )
        self.assertTrue(created)
        self.assertEqual(row.reservation_id, self.reservation.pk)
        self.assertEqual(row.ota_reservation_id, "5307026805")

    @patch("apps.integrations.channex.review_service.translate_text")
    def test_review_translation_cached(self, mock_translate):
        mock_translate.return_value = "Odličan boravak"
        self._login()
        url = f"/api/v1/reception/reviews/{self.review.pk}/?lang=hr&translate=1"
        response = self.client.get(url, HTTP_HOST="app.stay.hr")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["content_localized"], "Odličan boravak")
        self.assertTrue(data["content_is_translated"])
        mock_translate.assert_called_once()

        mock_translate.reset_mock()
        response2 = self.client.get(url, HTTP_HOST="app.stay.hr")
        self.assertEqual(response2.status_code, 200)
        self.assertEqual(response2.json()["content_localized"], "Odličan boravak")
        mock_translate.assert_not_called()

    @patch("apps.integrations.channex.review_service.complete_chat")
    @patch("apps.integrations.channex.review_service.llm_configured", return_value=True)
    def test_compose_reply(self, _mock_llm, mock_chat):
        mock_chat.return_value = "Thank you for staying with us!"
        self._login()
        response = self.client.post(
            f"/api/v1/reception/reviews/{self.review.pk}/compose-reply/",
            {"hint": "mention breakfast"},
            format="json",
            HTTP_HOST="app.stay.hr",
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["body_text"], "Thank you for staying with us!")
        self.assertTrue(data["llm_used"])

    @patch("apps.integrations.channex.review_service.complete_chat")
    @patch("apps.integrations.channex.review_service.llm_configured", return_value=True)
    def test_compose_reply_uses_detected_slovak(self, _mock_llm, mock_chat):
        mock_chat.return_value = "Ďakujeme za recenziu."
        self.review.content = "izba veľká a priestranná, ale záchod bol špinavý"
        self.review.save(update_fields=["content", "updated_at"])
        self._login()
        response = self.client.post(
            f"/api/v1/reception/reviews/{self.review.pk}/compose-reply/",
            {"hint": "be brief"},
            format="json",
            HTTP_HOST="app.stay.hr",
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["language"], "sk")
        user_prompt = mock_chat.call_args[0][1]
        self.assertIn("language code: sk", user_prompt)

    def test_reservation_reviews(self):
        self._login()
        response = self.client.get(
            f"/api/v1/reception/reservations/{self.reservation.pk}/reviews/?sync=0",
            HTTP_HOST="app.stay.hr",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["reservation_id"], self.reservation.pk)
        self.assertEqual(len(data["reviews"]), 1)

    @patch("apps.integrations.channex.review_service.sync_reviews_from_channex")
    def test_reservation_reviews_recent_checkout_defaults_to_auto_sync(self, mock_sync):
        checked_out = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            external_id=channex_external_id("channex-booking-checkout"),
            import_source="channex",
            booking_code="5856279283",
            check_in=date(2026, 6, 7),
            check_out=timezone.localdate(),
            booker_name="Markus Zöhrer",
            status=Reservation.Status.CHECKED_OUT,
        )
        self._login()
        response = self.client.get(
            f"/api/v1/reception/reservations/{checked_out.pk}/reviews/",
            HTTP_HOST="app.stay.hr",
        )
        self.assertEqual(response.status_code, 200)
        mock_sync.assert_called_once()

    @patch("apps.integrations.channex.review_service.sync_reviews_from_channex")
    def test_reservation_reviews_default_skips_channex_sync(self, mock_sync):
        empty_reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            external_id=channex_external_id("channex-booking-empty"),
            import_source="channex",
            booking_code="9999999999",
            check_in=date(2026, 7, 1),
            check_out=date(2026, 7, 2),
            booker_name="No Reviews Guest",
            status=Reservation.Status.EXPECTED,
        )
        self._login()
        response = self.client.get(
            f"/api/v1/reception/reservations/{empty_reservation.pk}/reviews/",
            HTTP_HOST="app.stay.hr",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["reservation_id"], empty_reservation.pk)
        self.assertEqual(data["reviews"], [])
        mock_sync.assert_not_called()

    @patch("apps.integrations.channex.review_service.sync_reviews_from_channex")
    def test_reservation_reviews_sync_one_triggers_channex(self, mock_sync):
        empty_reservation = Reservation.objects.create(
            tenant=self.tenant,
            property=self.property,
            external_id=channex_external_id("channex-booking-sync"),
            import_source="channex",
            booking_code="8888888888",
            check_in=date(2026, 7, 3),
            check_out=date(2026, 7, 4),
            booker_name="Sync Guest",
            status=Reservation.Status.EXPECTED,
        )
        self._login()
        response = self.client.get(
            f"/api/v1/reception/reservations/{empty_reservation.pk}/reviews/?sync=1",
            HTTP_HOST="app.stay.hr",
        )
        self.assertEqual(response.status_code, 200)
        mock_sync.assert_called_once()

    def test_reservation_reviews_relinks_unlinked(self):
        unlinked = ChannexReview.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            reservation=None,
            channex_review_id="review-uuid-res-unlinked",
            ota="BookingCom",
            ota_reservation_id="5307026805",
            content="Reservation endpoint relink",
            overall_score=Decimal("8.0"),
            is_replied=False,
            received_at=timezone.now(),
        )
        self.assertIsNone(unlinked.reservation_id)

        self._login()
        response = self.client.get(
            f"/api/v1/reception/reservations/{self.reservation.pk}/reviews/?sync=0",
            HTTP_HOST="app.stay.hr",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        review_ids = [item["id"] for item in data["reviews"]]
        self.assertIn(unlinked.pk, review_ids)
        unlinked.refresh_from_db()
        self.assertEqual(unlinked.reservation_id, self.reservation.pk)

    def test_reply_rejects_guest_name(self):
        self.review.guest_name = "Guest Test"
        self.review.save(update_fields=["guest_name", "updated_at"])
        self._login()
        response = self.client.post(
            f"/api/v1/reception/reviews/{self.review.pk}/reply/",
            {"reply": "Thank you Guest Test for your review."},
            format="json",
            HTTP_HOST="app.stay.hr",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("reply", response.json())

    def test_reply_rejects_url(self):
        self._login()
        response = self.client.post(
            f"/api/v1/reception/reviews/{self.review.pk}/reply/",
            {"reply": "See https://example.com for more info."},
            format="json",
            HTTP_HOST="app.stay.hr",
        )
        self.assertEqual(response.status_code, 400)

    @patch("apps.integrations.channex.review_service.complete_chat")
    @patch("apps.integrations.channex.review_service.llm_configured", return_value=True)
    def test_compose_bookingcom_omits_guest_name_from_llm_prompt(self, _mock_llm, mock_chat):
        from apps.integrations.channex.review_reply_policy import booking_compliant_fallback

        mock_chat.return_value = booking_compliant_fallback("en")
        self.review.guest_name = "Secret Guest"
        self.review.save(update_fields=["guest_name", "updated_at"])
        self._login()
        response = self.client.post(
            f"/api/v1/reception/reviews/{self.review.pk}/compose-reply/",
            {},
            format="json",
            HTTP_HOST="app.stay.hr",
        )
        self.assertEqual(response.status_code, 201)
        user_prompt = mock_chat.call_args[0][1]
        self.assertNotIn("Secret Guest", user_prompt)
        self.assertNotIn("Guest name:", user_prompt)

    @patch("apps.integrations.channex.review_service.ChannexClient")
    def test_reply_to_review(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.reply_to_review.return_value = {
            "data": {
                "id": "review-uuid-1",
                "attributes": {
                    "id": "review-uuid-1",
                    "reply": "Thank you!",
                    "is_replied": True,
                    "booking_id": self.booking_id,
                    "ota": "BookingCom",
                },
            }
        }
        mock_client_cls.return_value = mock_client

        self._login()
        response = self.client.post(
            f"/api/v1/reception/reviews/{self.review.pk}/reply/",
            {"reply": "Thank you!"},
            format="json",
            HTTP_HOST="app.stay.hr",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["is_replied"])
        self.assertEqual(data["reply"], "Thank you!")

    @patch("apps.integrations.channex.review_service.ChannexClient")
    def test_reply_to_review_channex_dict_reply(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.reply_to_review.return_value = {
            "data": {
                "id": "review-uuid-1",
                "attributes": {
                    "id": "review-uuid-1",
                    "reply": {"reply": "Hvala vam na ocjeni"},
                    "is_replied": False,
                    "booking_id": self.booking_id,
                    "ota": "BookingCom",
                },
            }
        }
        mock_client_cls.return_value = mock_client

        self._login()
        response = self.client.post(
            f"/api/v1/reception/reviews/{self.review.pk}/reply/",
            {"reply": "Hvala vam na ocjeni"},
            format="json",
            HTTP_HOST="app.stay.hr",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["is_replied"])
        self.assertEqual(data["reply"], "Hvala vam na ocjeni")
        self.review.refresh_from_db()
        self.assertEqual(self.review.reply, "Hvala vam na ocjeni")
        self.assertTrue(self.review.is_replied)
        self.assertIsNone(self.review.reply_sent_at)
        self.assertTrue(data["reply_pending_moderation"])
        self.assertFalse(data["reply_published"])
        self.assertTrue(data["can_reply"])
        self.assertIsNone(data.get("reply_blocked_reason"))

    def test_review_detail_exposes_reply_blocked_reason(self):
        self._login()
        response = self.client.get(
            f"/api/v1/reception/reviews/{self.review.pk}/?sync=0",
            HTTP_HOST="app.stay.hr",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["can_reply"])
        self.assertIsNone(data["reply_blocked_reason"])

    def test_rating_only_stays_in_default_list_and_leaves_unreplied_queue(self):
        rating_only = ChannexReview.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            reservation=self.reservation,
            channex_review_id="review-uuid-rating-only",
            channex_booking_id=self.booking_id,
            ota="BookingCom",
            content="",
            overall_score=Decimal("10.0"),
            is_replied=False,
            received_at=timezone.now(),
            expired_at=timezone.now() + timedelta(days=30),
        )
        expired = ChannexReview.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            reservation=self.reservation,
            channex_review_id="review-uuid-expired",
            ota="BookingCom",
            content="Late review",
            overall_score=Decimal("8.0"),
            is_replied=False,
            received_at=timezone.now() - timedelta(days=100),
            expired_at=timezone.now() - timedelta(hours=2),
        )
        hidden = ChannexReview.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            reservation=self.reservation,
            channex_review_id="review-uuid-airbnb-hidden",
            ota="AirBNB",
            content="Hidden until host rates guest",
            is_hidden=True,
            is_replied=False,
            received_at=timezone.now(),
        )
        pending = ChannexReview.objects.create(
            tenant=self.tenant,
            integration=self.integration,
            reservation=self.reservation,
            channex_review_id="review-uuid-pending",
            ota="BookingCom",
            content="Nice stay",
            reply="Thank you for staying with us.",
            overall_score=Decimal("8.0"),
            is_replied=True,
            received_at=timezone.now(),
            expired_at=timezone.now() + timedelta(days=30),
        )
        self._login()

        default_list = self.client.get(
            "/api/v1/reception/reviews/?sync=0",
            HTTP_HOST="app.stay.hr",
        )
        self.assertEqual(default_list.status_code, 200)
        default_ids = {item["id"] for item in default_list.json()["reviews"]}
        self.assertIn(self.review.pk, default_ids)
        self.assertIn(rating_only.pk, default_ids)
        self.assertIn(pending.pk, default_ids)
        rating_payload = next(
            item for item in default_list.json()["reviews"] if item["id"] == rating_only.pk
        )
        self.assertFalse(rating_payload["can_reply"])
        self.assertEqual(rating_payload["reply_blocked_reason"], "rating_only")
        self.assertEqual(rating_payload["overall_score"], 10.0)

        unreplied = self.client.get(
            "/api/v1/reception/reviews/?unreplied=1&sync=0",
            HTTP_HOST="app.stay.hr",
        )
        self.assertEqual(unreplied.status_code, 200)
        unreplied_ids = {item["id"] for item in unreplied.json()["reviews"]}
        self.assertEqual(unreplied_ids, {self.review.pk})
        self.assertNotIn(rating_only.pk, unreplied_ids)
        self.assertNotIn(expired.pk, unreplied_ids)
        self.assertNotIn(hidden.pk, unreplied_ids)
        self.assertNotIn(pending.pk, unreplied_ids)
        pending_detail = self.client.get(
            f"/api/v1/reception/reviews/{pending.pk}/?sync=0",
            HTTP_HOST="app.stay.hr",
        )
        self.assertEqual(pending_detail.status_code, 200)
        self.assertTrue(pending_detail.json()["can_reply"])
        self.assertTrue(pending_detail.json()["reply_pending_moderation"])

    @patch("apps.integrations.channex.review_service.sync_reviews_from_channex")
    def test_empty_action_queue_does_not_force_sync_when_reviews_exist(self, mock_sync):
        from apps.integrations.channex.review_service import mark_reviews_synced

        self.review.content = ""
        self.review.save(update_fields=["content", "updated_at"])
        mark_reviews_synced(self.tenant.pk)
        self._login()
        response = self.client.get(
            "/api/v1/reception/reviews/?unreplied=1&sync=auto",
            HTTP_HOST="app.stay.hr",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total"], 0)
        mock_sync.assert_not_called()

    def test_reply_rejects_rating_only(self):
        self.review.content = ""
        self.review.save(update_fields=["content", "updated_at"])
        self._login()
        response = self.client.post(
            f"/api/v1/reception/reviews/{self.review.pk}/reply/",
            {"reply": "Thank you for staying with us."},
            format="json",
            HTTP_HOST="app.stay.hr",
        )
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertIn("reply", body)
        self.assertIn("rating-only", body["reply"][0])

    @patch("apps.api.reception_reviews_views.reply_to_review")
    def test_reply_channex_api_error_uses_reply_key(self, mock_reply):
        from apps.integrations.channex.exceptions import ChannexApiError

        mock_reply.side_effect = ChannexApiError(
            "Channex POST /reviews/x/reply failed (422): cannot reply without comments"
        )
        self._login()
        response = self.client.post(
            f"/api/v1/reception/reviews/{self.review.pk}/reply/",
            {"reply": "Thank you for staying with us."},
            format="json",
            HTTP_HOST="app.stay.hr",
        )
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertIn("reply", body)
        self.assertIn("cannot reply without comments", body["reply"][0])

    @patch("apps.api.reception_reviews_views.reply_to_review")
    def test_reply_write_disabled_returns_503(self, mock_reply):
        from apps.integrations.channex.exceptions import ChannexWriteDisabled

        mock_reply.side_effect = ChannexWriteDisabled()
        self._login()
        response = self.client.post(
            f"/api/v1/reception/reviews/{self.review.pk}/reply/",
            {"reply": "Thank you for staying with us."},
            format="json",
            HTTP_HOST="app.stay.hr",
        )
        self.assertEqual(response.status_code, 503)
        self.assertIn("detail", response.json())
