from itertools import product

from django.test import SimpleTestCase

from apps.billing.services.fiscal_routing import (
    BuyerKind,
    BuyerStatusConfidence,
    FiscalChannel,
    LegalPaymentMethod,
    PaymentSignalConfidence,
    RoutingConfidence,
    RoutingReason,
    route_invoice,
)

HR_TAX_ID = "91381354893"
DE_TAX_ID = "DE123456789"


def _route(**overrides):
    defaults = {
        "buyer_kind": BuyerKind.CONSUMER,
        "buyer_country": "HR",
        "payment_method": LegalPaymentMethod.CASH,
        "payment_signal": PaymentSignalConfidence.EXPLICIT,
        "buyer_tax_id": "",
        "buyer_status": BuyerStatusConfidence.UNVERIFIED,
    }
    defaults.update(overrides)
    return route_invoice(**defaults)


class FiscalRoutingTableTests(SimpleTestCase):
    def test_consumer_transfer_is_f1(self):
        result = _route(
            buyer_kind=BuyerKind.CONSUMER,
            payment_method=LegalPaymentMethod.TRANSFER,
            payment_signal=PaymentSignalConfidence.UNKNOWN,
        )
        self.assertEqual(result.channel, FiscalChannel.F1)
        self.assertEqual(result.confidence, RoutingConfidence.CONFIRMED)
        self.assertEqual(result.reason, RoutingReason.CONSUMER_FINAL_CONSUMPTION)
        self.assertFalse(result.requires_recipient_tax_id)
        self.assertTrue(result.is_resolved)

    def test_consumer_is_f1_for_every_payment_and_signal(self):
        for method, signal in product(LegalPaymentMethod, PaymentSignalConfidence):
            with self.subTest(method=method, signal=signal):
                result = _route(
                    buyer_kind=BuyerKind.CONSUMER,
                    payment_method=method,
                    payment_signal=signal,
                    buyer_tax_id="ignored",
                    buyer_status=BuyerStatusConfidence.UNVERIFIED,
                )
                self.assertEqual(result.channel, FiscalChannel.F1)
                self.assertEqual(result.confidence, RoutingConfidence.CONFIRMED)
                self.assertFalse(result.requires_recipient_tax_id)

    def test_hr_business_explicit_card_is_f1_and_requires_recipient_oib(self):
        result = _route(
            buyer_kind=BuyerKind.BUSINESS,
            buyer_country="HR",
            buyer_tax_id=HR_TAX_ID,
            buyer_status=BuyerStatusConfidence.VERIFIED,
            payment_method=LegalPaymentMethod.CARD,
            payment_signal=PaymentSignalConfidence.EXPLICIT,
        )
        self.assertEqual(result.channel, FiscalChannel.F1)
        self.assertEqual(result.confidence, RoutingConfidence.CONFIRMED)
        self.assertEqual(result.reason, RoutingReason.DOMESTIC_B2B_ARTICLE_39_EXCEPTION)
        self.assertTrue(result.requires_recipient_tax_id)

    def test_hr_business_explicit_cash_is_f1(self):
        result = _route(
            buyer_kind=BuyerKind.BUSINESS,
            buyer_country="hr",
            buyer_tax_id=HR_TAX_ID,
            buyer_status=BuyerStatusConfidence.VERIFIED,
            payment_method=LegalPaymentMethod.CASH,
            payment_signal=PaymentSignalConfidence.EXPLICIT,
        )
        self.assertEqual(result.channel, FiscalChannel.F1)
        self.assertTrue(result.requires_recipient_tax_id)

    def test_hr_business_explicit_transfer_is_eracun(self):
        result = _route(
            buyer_kind=BuyerKind.BUSINESS,
            buyer_country="HR",
            buyer_tax_id=HR_TAX_ID,
            buyer_status=BuyerStatusConfidence.VERIFIED,
            payment_method=LegalPaymentMethod.TRANSFER,
            payment_signal=PaymentSignalConfidence.EXPLICIT,
        )
        self.assertEqual(result.channel, FiscalChannel.ERACUN)
        self.assertEqual(result.confidence, RoutingConfidence.CONFIRMED)
        self.assertEqual(result.reason, RoutingReason.DOMESTIC_B2B_ERACUN_MANDATE)
        self.assertFalse(result.requires_recipient_tax_id)

    def test_hr_business_inferred_payment_never_silently_f1(self):
        for method in LegalPaymentMethod:
            with self.subTest(method=method):
                result = _route(
                    buyer_kind=BuyerKind.BUSINESS,
                    buyer_country="HR",
                    buyer_tax_id=HR_TAX_ID,
                    buyer_status=BuyerStatusConfidence.VERIFIED,
                    payment_method=method,
                    payment_signal=PaymentSignalConfidence.INFERRED,
                )
                self.assertIsNone(result.channel)
                self.assertEqual(result.confidence, RoutingConfidence.REVIEW)
                self.assertEqual(
                    result.reason, RoutingReason.DOMESTIC_B2B_PAYMENT_UNCONFIRMED
                )
                self.assertFalse(result.requires_recipient_tax_id)

    def test_hr_business_unknown_signal_is_unresolved(self):
        result = _route(
            buyer_kind=BuyerKind.BUSINESS,
            buyer_country="HR",
            buyer_tax_id=HR_TAX_ID,
            buyer_status=BuyerStatusConfidence.VERIFIED,
            payment_method=LegalPaymentMethod.CARD,
            payment_signal=PaymentSignalConfidence.UNKNOWN,
        )
        self.assertIsNone(result.channel)
        self.assertEqual(result.confidence, RoutingConfidence.REVIEW)

    def test_hr_business_explicit_unknown_method_is_unresolved(self):
        result = _route(
            buyer_kind=BuyerKind.BUSINESS,
            buyer_country="HR",
            buyer_tax_id=HR_TAX_ID,
            buyer_status=BuyerStatusConfidence.VERIFIED,
            payment_method=LegalPaymentMethod.UNKNOWN,
            payment_signal=PaymentSignalConfidence.EXPLICIT,
        )
        self.assertIsNone(result.channel)
        self.assertEqual(result.confidence, RoutingConfidence.REVIEW)

    def test_foreign_verified_business_is_standard_for_every_payment(self):
        for method, signal in product(LegalPaymentMethod, PaymentSignalConfidence):
            with self.subTest(method=method, signal=signal):
                result = _route(
                    buyer_kind=BuyerKind.BUSINESS,
                    buyer_country="DE",
                    buyer_tax_id=DE_TAX_ID,
                    buyer_status=BuyerStatusConfidence.VERIFIED,
                    payment_method=method,
                    payment_signal=signal,
                )
                self.assertEqual(result.channel, FiscalChannel.STANDARD)
                self.assertEqual(result.confidence, RoutingConfidence.CONFIRMED)
                self.assertEqual(
                    result.reason, RoutingReason.FOREIGN_B2B_OUTSIDE_MANDATE
                )
                self.assertNotEqual(result.channel, FiscalChannel.F1)
                self.assertNotEqual(result.channel, FiscalChannel.ERACUN)
                self.assertFalse(result.requires_recipient_tax_id)

    def test_foreign_country_alone_is_not_standard(self):
        result = _route(
            buyer_kind=BuyerKind.BUSINESS,
            buyer_country="DE",
            buyer_tax_id=DE_TAX_ID,
            buyer_status=BuyerStatusConfidence.UNVERIFIED,
            payment_method=LegalPaymentMethod.TRANSFER,
            payment_signal=PaymentSignalConfidence.EXPLICIT,
        )
        self.assertIsNone(result.channel)
        self.assertEqual(result.confidence, RoutingConfidence.REVIEW)
        self.assertEqual(result.reason, RoutingReason.BUSINESS_IDENTITY_UNVERIFIED)

    def test_empty_tax_id_is_not_treated_as_consumer(self):
        result = _route(
            buyer_kind=BuyerKind.BUSINESS,
            buyer_country="HR",
            buyer_tax_id="",
            buyer_status=BuyerStatusConfidence.VERIFIED,
            payment_method=LegalPaymentMethod.TRANSFER,
            payment_signal=PaymentSignalConfidence.EXPLICIT,
        )
        self.assertIsNone(result.channel)
        self.assertEqual(result.reason, RoutingReason.BUSINESS_TAX_ID_MISSING)
        self.assertNotEqual(result.reason, RoutingReason.CONSUMER_FINAL_CONSUMPTION)

    def test_blank_business_country_is_unresolved(self):
        result = _route(
            buyer_kind=BuyerKind.BUSINESS,
            buyer_country="  ",
            buyer_tax_id=HR_TAX_ID,
            buyer_status=BuyerStatusConfidence.VERIFIED,
        )
        self.assertIsNone(result.channel)
        self.assertEqual(result.reason, RoutingReason.BUSINESS_COUNTRY_UNKNOWN)

    def test_display_country_name_is_not_a_known_country(self):
        result = _route(
            buyer_kind=BuyerKind.BUSINESS,
            buyer_country="Hrvatska",
            buyer_tax_id=HR_TAX_ID,
            buyer_status=BuyerStatusConfidence.VERIFIED,
            payment_method=LegalPaymentMethod.TRANSFER,
            payment_signal=PaymentSignalConfidence.EXPLICIT,
        )
        self.assertIsNone(result.channel)
        self.assertEqual(result.reason, RoutingReason.BUSINESS_COUNTRY_UNKNOWN)

    def test_unknown_buyer_kind_is_always_unresolved(self):
        for method, signal, status in product(
            LegalPaymentMethod,
            PaymentSignalConfidence,
            BuyerStatusConfidence,
        ):
            with self.subTest(method=method, signal=signal, status=status):
                result = _route(
                    buyer_kind=BuyerKind.UNKNOWN,
                    buyer_country="HR",
                    buyer_tax_id=HR_TAX_ID,
                    buyer_status=status,
                    payment_method=method,
                    payment_signal=signal,
                )
                self.assertIsNone(result.channel)
                self.assertEqual(result.confidence, RoutingConfidence.REVIEW)
                self.assertEqual(result.reason, RoutingReason.BUYER_STATUS_UNKNOWN)


class FiscalRoutingInvariantTests(SimpleTestCase):
    def test_booking_is_not_a_legal_payment_method(self):
        self.assertNotIn("booking", {member.value for member in LegalPaymentMethod})
        self.assertFalse(hasattr(LegalPaymentMethod, "BOOKING"))

    def test_unresolved_always_has_review_confidence(self):
        for kind, status, country, tax_id, method, signal in product(
            BuyerKind,
            BuyerStatusConfidence,
            ("", "HR", "DE"),
            ("", HR_TAX_ID, DE_TAX_ID),
            LegalPaymentMethod,
            PaymentSignalConfidence,
        ):
            result = route_invoice(
                buyer_kind=kind,
                buyer_country=country,
                buyer_tax_id=tax_id,
                buyer_status=status,
                payment_method=method,
                payment_signal=signal,
            )
            if result.channel is None:
                self.assertEqual(result.confidence, RoutingConfidence.REVIEW)
                self.assertFalse(result.is_resolved)
            else:
                self.assertEqual(result.confidence, RoutingConfidence.CONFIRMED)
                self.assertTrue(result.is_resolved)

    def test_eracun_only_for_verified_explicit_hr_transfer(self):
        for kind, status, country, tax_id, method, signal in product(
            BuyerKind,
            BuyerStatusConfidence,
            ("", "HR", "DE"),
            ("", HR_TAX_ID),
            LegalPaymentMethod,
            PaymentSignalConfidence,
        ):
            result = route_invoice(
                buyer_kind=kind,
                buyer_country=country,
                buyer_tax_id=tax_id,
                buyer_status=status,
                payment_method=method,
                payment_signal=signal,
            )
            if result.channel is FiscalChannel.ERACUN:
                self.assertEqual(kind, BuyerKind.BUSINESS)
                self.assertEqual(status, BuyerStatusConfidence.VERIFIED)
                self.assertEqual(country, "HR")
                self.assertTrue(tax_id)
                self.assertEqual(method, LegalPaymentMethod.TRANSFER)
                self.assertEqual(signal, PaymentSignalConfidence.EXPLICIT)
                self.assertFalse(result.requires_recipient_tax_id)

    def test_standard_only_for_verified_foreign_business(self):
        for kind, status, country, tax_id, method, signal in product(
            BuyerKind,
            BuyerStatusConfidence,
            ("", "HR", "DE"),
            ("", HR_TAX_ID, DE_TAX_ID),
            LegalPaymentMethod,
            PaymentSignalConfidence,
        ):
            result = route_invoice(
                buyer_kind=kind,
                buyer_country=country,
                buyer_tax_id=tax_id,
                buyer_status=status,
                payment_method=method,
                payment_signal=signal,
            )
            if result.channel is FiscalChannel.STANDARD:
                self.assertEqual(kind, BuyerKind.BUSINESS)
                self.assertEqual(status, BuyerStatusConfidence.VERIFIED)
                self.assertEqual(country, "DE")
                self.assertTrue(tax_id)
                self.assertFalse(result.requires_recipient_tax_id)

    def test_recipient_oib_flag_only_on_domestic_b2b_f1(self):
        for kind, status, country, tax_id, method, signal in product(
            BuyerKind,
            BuyerStatusConfidence,
            ("", "HR", "DE"),
            ("", HR_TAX_ID),
            LegalPaymentMethod,
            PaymentSignalConfidence,
        ):
            result = route_invoice(
                buyer_kind=kind,
                buyer_country=country,
                buyer_tax_id=tax_id,
                buyer_status=status,
                payment_method=method,
                payment_signal=signal,
            )
            if result.requires_recipient_tax_id:
                self.assertEqual(result.channel, FiscalChannel.F1)
                self.assertEqual(kind, BuyerKind.BUSINESS)
                self.assertEqual(country, "HR")
                self.assertEqual(status, BuyerStatusConfidence.VERIFIED)
                self.assertIn(
                    method, (LegalPaymentMethod.CASH, LegalPaymentMethod.CARD)
                )
