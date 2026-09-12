from django.test import SimpleTestCase

from apps.billing.services.billing_recipient import (
    ALLOWED_TRANSITIONS,
    BillingRecipientDraft,
    RecipientRejectReason,
    RecipientStatus,
    can_mark_applied,
    can_transition,
    has_request_anchor,
    is_structurally_ready,
    next_status_for_fields,
    normalize_country,
    ready_missing_fields,
)
from apps.billing.services.fiscal_routing import BuyerStatusConfidence


def _hr_ready(**overrides) -> BillingRecipientDraft:
    fields = dict(
        company_name="PRO AUTOMATIKA",
        tax_id="87357644223",
        tax_id_country="HR",
        country="HR",
        address="Novo naselje 19E",
        postal_code="22214",
        city="Bilice",
        email="domagoj.perjanec@pro-automatika.hr",
        identity_confidence=BuyerStatusConfidence.UNVERIFIED,
        source_excerpt="Molio bih R1 račun.",
    )
    fields.update(overrides)
    return BillingRecipientDraft(**fields)


class BillingRecipientCompletenessTests(SimpleTestCase):
    def test_empty_draft_has_no_request_anchor(self):
        self.assertFalse(has_request_anchor(BillingRecipientDraft()))

    def test_excerpt_alone_is_a_valid_requested_anchor(self):
        draft = BillingRecipientDraft(source_excerpt="Molio bih R1 račun.")
        self.assertTrue(has_request_anchor(draft))
        self.assertFalse(is_structurally_ready(draft))

    def test_hr_company_from_1159_message_is_structurally_ready(self):
        draft = _hr_ready()
        self.assertTrue(has_request_anchor(draft))
        self.assertTrue(is_structurally_ready(draft))
        self.assertEqual(draft.identity_confidence, BuyerStatusConfidence.UNVERIFIED)

    def test_unverified_identity_does_not_block_ready(self):
        draft = _hr_ready(identity_confidence=BuyerStatusConfidence.UNVERIFIED)
        self.assertTrue(is_structurally_ready(draft))

    def test_display_country_name_is_not_ready(self):
        draft = _hr_ready(country="Hrvatska", tax_id_country="Hrvatska")
        self.assertFalse(is_structurally_ready(draft))
        self.assertIn("country", ready_missing_fields(draft))
        self.assertIn("tax_id_country", ready_missing_fields(draft))

    def test_hr_tax_id_must_be_eleven_digits(self):
        draft = _hr_ready(tax_id="8735764422")
        self.assertFalse(is_structurally_ready(draft))
        self.assertIn("tax_id", ready_missing_fields(draft))

    def test_foreign_vat_id_is_not_capped_at_eleven(self):
        draft = _hr_ready(
            company_name="Example GmbH",
            tax_id="DE123456789",
            tax_id_country="DE",
            country="DE",
            city="Berlin",
            postal_code="10115",
            address="Unter den Linden 1",
        )
        self.assertTrue(is_structurally_ready(draft))
        self.assertEqual(normalize_country(draft.country), "DE")

    def test_missing_email_keeps_row_requested(self):
        draft = _hr_ready(email="")
        self.assertFalse(is_structurally_ready(draft))
        self.assertEqual(
            next_status_for_fields(RecipientStatus.REQUESTED, draft),
            RecipientStatus.REQUESTED,
        )


class BillingRecipientTransitionTests(SimpleTestCase):
    def test_allowed_edges(self):
        self.assertTrue(
            can_transition(RecipientStatus.REQUESTED, RecipientStatus.READY)
        )
        self.assertTrue(
            can_transition(RecipientStatus.READY, RecipientStatus.REQUESTED)
        )
        self.assertTrue(can_transition(RecipientStatus.READY, RecipientStatus.APPLIED))
        self.assertTrue(
            can_transition(RecipientStatus.REQUESTED, RecipientStatus.REQUESTED)
        )

    def test_skip_requested_to_applied_is_forbidden(self):
        self.assertFalse(
            can_transition(RecipientStatus.REQUESTED, RecipientStatus.APPLIED)
        )
        self.assertNotIn(
            (RecipientStatus.REQUESTED, RecipientStatus.APPLIED),
            ALLOWED_TRANSITIONS,
        )

    def test_applied_is_terminal(self):
        self.assertFalse(
            can_transition(RecipientStatus.APPLIED, RecipientStatus.READY)
        )
        self.assertFalse(
            can_transition(RecipientStatus.APPLIED, RecipientStatus.REQUESTED)
        )
        self.assertIsNone(
            next_status_for_fields(RecipientStatus.APPLIED, _hr_ready())
        )

    def test_complete_edit_promotes_requested_to_ready(self):
        self.assertEqual(
            next_status_for_fields(RecipientStatus.REQUESTED, _hr_ready()),
            RecipientStatus.READY,
        )

    def test_incomplete_edit_demotes_ready_to_requested(self):
        self.assertEqual(
            next_status_for_fields(
                RecipientStatus.READY, _hr_ready(tax_id="")
            ),
            RecipientStatus.REQUESTED,
        )


class BillingRecipientApplyGuardTests(SimpleTestCase):
    def test_ready_may_apply_only_to_a_new_invoice(self):
        self.assertIsNone(
            can_mark_applied(
                current=RecipientStatus.READY,
                invoice_already_persisted=False,
            )
        )

    def test_ready_cannot_apply_to_already_persisted_invoice(self):
        """#1159: request existed before 259, but 259 must never become APPLIED."""
        self.assertEqual(
            can_mark_applied(
                current=RecipientStatus.READY,
                invoice_already_persisted=True,
            ),
            RecipientRejectReason.INVOICE_ALREADY_PERSISTED,
        )

    def test_requested_cannot_skip_to_applied(self):
        self.assertEqual(
            can_mark_applied(
                current=RecipientStatus.REQUESTED,
                invoice_already_persisted=False,
            ),
            RecipientRejectReason.SKIP_TO_APPLIED,
        )

    def test_applied_cannot_apply_again(self):
        self.assertEqual(
            can_mark_applied(
                current=RecipientStatus.APPLIED,
                invoice_already_persisted=False,
            ),
            RecipientRejectReason.ALREADY_APPLIED,
        )
