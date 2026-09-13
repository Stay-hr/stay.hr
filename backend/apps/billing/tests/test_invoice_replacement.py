from decimal import Decimal

from django.test import SimpleTestCase

from apps.billing.exceptions import ReplacementInProgress
from apps.billing.services.fiscal_routing import BuyerStatusConfidence
from apps.billing.services.invoice_replacement import (
    ALLOWED_TRANSITIONS,
    IDENTITY_FIELDS,
    InvoiceDocumentRole,
    InvoiceDocumentSnapshot,
    InvoiceLineSnapshot,
    ReplacementCaseStatus,
    ReplacementRecipientDraft,
    ReplacementRejectReason,
    can_cancel,
    can_complete,
    can_edit_recipient,
    can_issue_storno,
    can_open_case,
    can_transition,
    derive_document_role,
    expected_issuer_oib,
    identity_changed,
    is_structurally_ready,
    mirror_invoice_snapshot,
    negate_invoice_snapshot,
    ready_missing_fields,
)


def _hr_ready(**overrides) -> ReplacementRecipientDraft:
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
    )
    fields.update(overrides)
    return ReplacementRecipientDraft(**fields)


def _snapshot() -> InvoiceDocumentSnapshot:
    return InvoiceDocumentSnapshot(
        subtotal=Decimal("88.50"),
        vat_amount=Decimal("11.50"),
        total=Decimal("295.36"),
        currency="EUR",
        payment_method="booking",
        payment_note="Booking.com",
        buyer_name="DARIO PREZEC",
        buyer_document_number="119907920",
        buyer_address="ZAGREB, GRAD ZAGREB, OPOROVEČKI VINOGRADI 66 A",
        buyer_country="Hrvatska",
        lines=(
            InvoiceLineSnapshot(
                sort_order=1,
                line_kind="accommodation",
                description="Noćenje",
                quantity=Decimal("2"),
                unit_price=Decimal("130.00"),
                vat_rate=Decimal("13.00"),
                vat_amount=Decimal("29.91"),
                line_total=Decimal("260.00"),
            ),
        ),
    )


class ReplacementRecipientCompletenessTests(SimpleTestCase):
    def test_hr_1159_company_is_structurally_ready(self):
        draft = _hr_ready()
        self.assertTrue(is_structurally_ready(draft))
        self.assertEqual(draft.identity_confidence, BuyerStatusConfidence.UNVERIFIED)

    def test_hr_tax_id_must_be_eleven_digits(self):
        draft = _hr_ready(tax_id="8735764422")
        self.assertIn("tax_id", ready_missing_fields(draft))

    def test_identity_fields_are_ready_fields(self):
        self.assertEqual(IDENTITY_FIELDS, (
            "company_name",
            "tax_id",
            "tax_id_country",
            "country",
            "address",
            "postal_code",
            "city",
            "email",
        ))

    def test_identity_edit_is_detected(self):
        self.assertTrue(identity_changed(_hr_ready(), _hr_ready(tax_id="11111111111")))
        self.assertFalse(identity_changed(_hr_ready(), _hr_ready(phone="091")))


class ReplacementTransitionTests(SimpleTestCase):
    def test_only_open_to_completed_or_cancelled(self):
        self.assertTrue(
            can_transition(ReplacementCaseStatus.OPEN, ReplacementCaseStatus.COMPLETED)
        )
        self.assertTrue(
            can_transition(ReplacementCaseStatus.OPEN, ReplacementCaseStatus.CANCELLED)
        )
        self.assertFalse(
            can_transition(ReplacementCaseStatus.COMPLETED, ReplacementCaseStatus.CANCELLED)
        )
        self.assertFalse(
            can_transition(ReplacementCaseStatus.CANCELLED, ReplacementCaseStatus.OPEN)
        )
        self.assertEqual(len(ALLOWED_TRANSITIONS), 2)

    def test_replacement_in_progress_code(self):
        self.assertEqual(ReplacementInProgress.code, "replacement_in_progress")


class ReplacementSnapshotTests(SimpleTestCase):
    def test_storno_is_exact_negative_with_same_buyer_and_payment(self):
        original = _snapshot()
        storno = negate_invoice_snapshot(original)
        self.assertEqual(storno.total, Decimal("-295.36"))
        self.assertEqual(storno.subtotal, Decimal("-88.50"))
        self.assertEqual(storno.vat_amount, Decimal("-11.50"))
        self.assertEqual(storno.buyer_name, original.buyer_name)
        self.assertEqual(storno.buyer_document_number, original.buyer_document_number)
        self.assertEqual(storno.payment_method, original.payment_method)
        self.assertEqual(storno.payment_note, original.payment_note)
        self.assertEqual(storno.currency, original.currency)
        self.assertEqual(storno.lines[0].quantity, Decimal("2"))
        self.assertEqual(storno.lines[0].unit_price, Decimal("-130.00"))
        self.assertEqual(storno.lines[0].line_total, Decimal("-260.00"))

    def test_replacement_mirrors_positive_economics(self):
        original = _snapshot()
        replacement = mirror_invoice_snapshot(original)
        self.assertEqual(replacement, original)


class DocumentRoleTests(SimpleTestCase):
    def test_roles_come_from_links_not_cardinality(self):
        self.assertEqual(
            derive_document_role(259, original_ids=frozenset({259}), storno_ids=frozenset(), replacement_ids=frozenset()),
            InvoiceDocumentRole.ORIGINAL,
        )
        self.assertEqual(
            derive_document_role(260, original_ids=frozenset({259}), storno_ids=frozenset({260}), replacement_ids=frozenset()),
            InvoiceDocumentRole.STORNO,
        )
        self.assertEqual(
            derive_document_role(
                261,
                original_ids=frozenset({259, 261}),
                storno_ids=frozenset({260}),
                replacement_ids=frozenset({261}),
            ),
            InvoiceDocumentRole.REPLACEMENT,
        )
        self.assertEqual(
            derive_document_role(10, original_ids=frozenset(), storno_ids=frozenset(), replacement_ids=frozenset()),
            InvoiceDocumentRole.STANDALONE,
        )

    def test_storno_cannot_also_be_original(self):
        with self.assertRaises(ValueError):
            derive_document_role(
                260,
                original_ids=frozenset({260}),
                storno_ids=frozenset({260}),
                replacement_ids=frozenset(),
            )


class ReplacementGuardTests(SimpleTestCase):
    def test_open_requires_current_effective_positive(self):
        self.assertEqual(
            can_open_case(
                candidate_is_effective=False,
                candidate_is_storno=False,
                same_scope=True,
                open_case_exists=False,
            ),
            ReplacementRejectReason.NOT_EFFECTIVE_ORIGINAL,
        )
        self.assertEqual(
            can_open_case(
                candidate_is_effective=True,
                candidate_is_storno=True,
                same_scope=True,
                open_case_exists=False,
            ),
            ReplacementRejectReason.CANDIDATE_IS_STORNO,
        )
        self.assertIsNone(
            can_open_case(
                candidate_is_effective=True,
                candidate_is_storno=False,
                same_scope=True,
                open_case_exists=False,
            )
        )

    def test_recipient_freezes_when_storno_exists(self):
        self.assertEqual(
            can_edit_recipient(status=ReplacementCaseStatus.OPEN, storno_invoice_id=260),
            ReplacementRejectReason.RECIPIENT_FROZEN,
        )
        self.assertIsNone(
            can_edit_recipient(status=ReplacementCaseStatus.OPEN, storno_invoice_id=None)
        )

    def test_expected_issuer_oib_prefers_frozen_original(self):
        self.assertEqual(
            expected_issuer_oib(
                original_issuer_oib="12345678901",
                recorded_issuer_oib="10987654321",
            ),
            "12345678901",
        )
        self.assertEqual(
            expected_issuer_oib(original_issuer_oib="", recorded_issuer_oib="12345678901"),
            "12345678901",
        )
        self.assertEqual(
            expected_issuer_oib(original_issuer_oib="", recorded_issuer_oib=""),
            "",
        )

    def test_storno_requires_ready_and_verified(self):
        self.assertEqual(
            can_issue_storno(
                status=ReplacementCaseStatus.OPEN,
                storno_invoice_id=None,
                recipient_ready=True,
                recipient_verified=False,
            ),
            ReplacementRejectReason.RECIPIENT_NOT_VERIFIED,
        )
        self.assertIsNone(
            can_issue_storno(
                status=ReplacementCaseStatus.OPEN,
                storno_invoice_id=None,
                recipient_ready=True,
                recipient_verified=True,
            )
        )

    def test_complete_requires_storno_and_no_replacement(self):
        self.assertEqual(
            can_complete(
                status=ReplacementCaseStatus.OPEN,
                storno_invoice_id=None,
                replacement_invoice_id=None,
            ),
            ReplacementRejectReason.STORNO_MISSING,
        )
        self.assertIsNone(
            can_complete(
                status=ReplacementCaseStatus.OPEN,
                storno_invoice_id=260,
                replacement_invoice_id=None,
            )
        )

    def test_cancel_only_before_storno(self):
        self.assertEqual(
            can_cancel(
                status=ReplacementCaseStatus.OPEN,
                storno_invoice_id=260,
                replacement_invoice_id=None,
            ),
            ReplacementRejectReason.CANCEL_AFTER_STORNO,
        )
        self.assertIsNone(
            can_cancel(
                status=ReplacementCaseStatus.OPEN,
                storno_invoice_id=None,
                replacement_invoice_id=None,
            )
        )
