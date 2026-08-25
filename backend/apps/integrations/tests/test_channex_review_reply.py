from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.integrations.channex.exceptions import ChannexBookingIngestError
from apps.integrations.channex.review_reply_policy import booking_compliant_fallback
from apps.integrations.channex.review_service import (
    REPLY_BLOCK_AIRBNB_HIDDEN,
    REPLY_BLOCK_EXPIRED,
    REPLY_BLOCK_RATING_ONLY,
    REPLY_BLOCK_REPLIED,
    compose_review_reply,
    detect_review_language,
    filter_reply_actionable,
    reply_pending_moderation,
    reply_published,
    reply_to_review,
    review_reply_allowed,
    review_reply_block_reason,
    serialize_channex_review,
    upsert_channex_review_from_payload,
)
from apps.integrations.models import ChannexReview, IntegrationConfig
from apps.properties.models import Property
from apps.tenants.models import Tenant


class ChannexReviewReplyTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="uzorita", name="Uzorita")
        self.property = Property.objects.create(tenant=self.tenant, name="Uzorita", slug="uzorita")
        self.integration = IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.CHANNEX,
            is_active=True,
        )

    def _review(self, **kwargs) -> ChannexReview:
        defaults = {
            "tenant": self.tenant,
            "integration": self.integration,
            "channex_review_id": "review-test-1",
            "ota": "BookingCom",
            "overall_score": Decimal("6.0"),
            "received_at": timezone.now(),
            "expired_at": timezone.now() + timedelta(days=90),
        }
        defaults.update(kwargs)
        return ChannexReview.objects.create(**defaults)

    def test_detect_slovak_review_language(self):
        content = "izba veľka priestranna, záchod bol špinavy"
        self.assertEqual(detect_review_language(content), "sk")

    def test_review_reply_allowed_when_reply_submitted_not_published(self):
        row = self._review(
            content="The apartment was fine.",
            reply="Draft reply waiting for Booking moderation",
            is_replied=True,
            reply_sent_at=None,
        )
        self.assertTrue(review_reply_allowed(row))
        self.assertIsNone(review_reply_block_reason(row))

    def test_review_reply_not_allowed_when_published(self):
        row = self._review(
            reply="Published reply",
            is_replied=True,
            reply_sent_at=timezone.now(),
        )
        self.assertFalse(review_reply_allowed(row))

    def test_reply_pending_moderation_bookingcom(self):
        row = self._review(reply="Waiting", is_replied=True, reply_sent_at=None)
        self.assertTrue(reply_pending_moderation(row))
        self.assertFalse(reply_published(row))

    def test_serialize_includes_moderation_fields(self):
        row = self._review(
            content="izba veľka",
            reply="Odpoveď",
            is_replied=True,
            reply_sent_at=None,
        )
        data = serialize_channex_review(row)
        self.assertEqual(data["suggested_reply_language"], "sk")
        self.assertTrue(data["reply_pending_moderation"])
        self.assertFalse(data["reply_published"])
        self.assertTrue(data["can_reply"])
        self.assertIsNone(data["reply_blocked_reason"])

    def test_block_reasons_are_distinct(self):
        replied = self._review(
            channex_review_id="r-replied",
            content="Nice stay",
            reply="Thanks",
            is_replied=True,
            reply_sent_at=timezone.now(),
        )
        expired = self._review(
            channex_review_id="r-expired",
            content="Nice stay",
            expired_at=timezone.now() - timedelta(hours=1),
        )
        rating_only = self._review(
            channex_review_id="r-rating",
            content="",
            overall_score=Decimal("10.0"),
        )
        whitespace_only = self._review(
            channex_review_id="r-whitespace",
            content="   ",
            overall_score=Decimal("8.0"),
        )
        airbnb_hidden = self._review(
            channex_review_id="r-airbnb",
            ota="AirBNB",
            content="Hidden until host rates guest",
            is_hidden=True,
        )
        rating_only_but_replied = self._review(
            channex_review_id="r-rating-replied",
            content="",
            overall_score=Decimal("9.0"),
            reply="Thanks",
            is_replied=True,
            reply_sent_at=timezone.now(),
        )

        self.assertEqual(review_reply_block_reason(replied), REPLY_BLOCK_REPLIED)
        self.assertEqual(review_reply_block_reason(expired), REPLY_BLOCK_EXPIRED)
        self.assertEqual(review_reply_block_reason(rating_only), REPLY_BLOCK_RATING_ONLY)
        self.assertEqual(review_reply_block_reason(whitespace_only), REPLY_BLOCK_RATING_ONLY)
        self.assertEqual(review_reply_block_reason(airbnb_hidden), REPLY_BLOCK_AIRBNB_HIDDEN)
        self.assertEqual(review_reply_block_reason(rating_only_but_replied), REPLY_BLOCK_REPLIED)
        self.assertFalse(review_reply_allowed(rating_only))
        self.assertFalse(review_reply_allowed(expired))

    def test_reply_to_review_rejects_rating_only(self):
        row = self._review(content="", overall_score=Decimal("10.0"))
        with self.assertRaises(ChannexBookingIngestError) as ctx:
            reply_to_review(self.integration, row, "Thank you for your review.")
        self.assertIn("rating-only", str(ctx.exception))

    def test_filter_reply_actionable_matches_block_reason(self):
        actionable = self._review(channex_review_id="r-ok", content="Great apartment")
        rows = [
            actionable,
            self._review(
                channex_review_id="r-replied",
                content="Nice stay",
                reply="Thanks",
                is_replied=True,
                reply_sent_at=timezone.now(),
            ),
            self._review(
                channex_review_id="r-expired",
                content="Nice stay",
                expired_at=timezone.now() - timedelta(hours=1),
            ),
            self._review(
                channex_review_id="r-rating",
                content="",
                overall_score=Decimal("10.0"),
            ),
            self._review(
                channex_review_id="r-airbnb",
                ota="AirBNB",
                content="Hidden until host rates guest",
                is_hidden=True,
            ),
        ]
        qs = ChannexReview.objects.filter(pk__in=[row.pk for row in rows])
        expected = {row.pk for row in rows if review_reply_block_reason(row) is None}
        actual = set(filter_reply_actionable(qs).values_list("pk", flat=True))
        self.assertEqual(actual, expected)
        self.assertEqual(expected, {actionable.pk})

    def test_upsert_preserves_guest_content_when_reply_payload_omits_it(self):
        row = self._review(content="Great apartment, quiet street.")
        updated, created, content_just_arrived = upsert_channex_review_from_payload(
            tenant=self.tenant,
            integration=self.integration,
            payload={
                "id": row.channex_review_id,
                "ota": "BookingCom",
                "reply": {"reply": "Thank you for staying with us."},
                "is_replied": True,
            },
        )
        self.assertFalse(created)
        self.assertFalse(content_just_arrived)
        self.assertEqual(updated.content, "Great apartment, quiet street.")
        self.assertTrue(review_reply_allowed(updated))


class ChannexReviewComposePolicyTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="uzorita", name="Uzorita")
        self.integration = IntegrationConfig.objects.create(
            tenant=self.tenant,
            provider=IntegrationConfig.Provider.CHANNEX,
            is_active=True,
        )

    def _review(self, **kwargs) -> ChannexReview:
        defaults = {
            "tenant": self.tenant,
            "integration": self.integration,
            "channex_review_id": "review-compose-policy",
            "ota": "BookingCom",
            "content": "The bathroom was dirty.",
            "guest_name": "Jane Doe",
            "overall_score": Decimal("4.0"),
            "received_at": timezone.now(),
            "expired_at": timezone.now() + timedelta(days=90),
        }
        defaults.update(kwargs)
        return ChannexReview.objects.create(**defaults)

    @patch("apps.integrations.channex.review_service.complete_chat")
    @patch("apps.integrations.channex.review_service.llm_configured", return_value=True)
    def test_compose_uses_fallback_when_llm_non_compliant(self, _mock_llm, mock_chat):
        mock_chat.return_value = "Jane, we apologize the bathroom was dirty. Call us at +385991234567."
        row = self._review()
        body, llm_used, lang = compose_review_reply(row)
        self.assertTrue(llm_used)
        self.assertEqual(body, booking_compliant_fallback(lang))
        self.assertNotIn("Jane", body)
