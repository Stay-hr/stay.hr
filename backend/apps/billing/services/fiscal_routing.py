"""Decide the exclusive fiscal channel for an issued guest invoice.

Pure function: no Django, no I/O. Callers do not exist yet — see ADR 0020.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class FiscalChannel(StrEnum):
    F1 = "f1"
    ERACUN = "eracun"
    STANDARD = "standard"


class RoutingConfidence(StrEnum):
    CONFIRMED = "confirmed"
    REVIEW = "review"


class BuyerKind(StrEnum):
    CONSUMER = "consumer"
    BUSINESS = "business"
    UNKNOWN = "unknown"


class BuyerStatusConfidence(StrEnum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"


class LegalPaymentMethod(StrEnum):
    CASH = "cash"
    CARD = "card"
    TRANSFER = "transfer"
    UNKNOWN = "unknown"


class PaymentSignalConfidence(StrEnum):
    EXPLICIT = "explicit"
    INFERRED = "inferred"
    UNKNOWN = "unknown"


class RoutingReason(StrEnum):
    CONSUMER_FINAL_CONSUMPTION = "consumer_final_consumption"
    DOMESTIC_B2B_ARTICLE_39_EXCEPTION = "domestic_b2b_article_39_exception"
    DOMESTIC_B2B_ERACUN_MANDATE = "domestic_b2b_eracun_mandate"
    DOMESTIC_B2B_PAYMENT_UNCONFIRMED = "domestic_b2b_payment_unconfirmed"
    FOREIGN_B2B_OUTSIDE_MANDATE = "foreign_b2b_outside_mandate"
    BUSINESS_IDENTITY_UNVERIFIED = "business_identity_unverified"
    BUSINESS_TAX_ID_MISSING = "business_tax_id_missing"
    BUSINESS_COUNTRY_UNKNOWN = "business_country_unknown"
    BUYER_STATUS_UNKNOWN = "buyer_status_unknown"


@dataclass(frozen=True)
class FiscalRouting:
    channel: FiscalChannel | None
    confidence: RoutingConfidence
    reason: str
    requires_recipient_tax_id: bool = False

    @property
    def is_resolved(self) -> bool:
        return self.channel is not None


def _unresolved(reason: RoutingReason) -> FiscalRouting:
    return FiscalRouting(
        channel=None,
        confidence=RoutingConfidence.REVIEW,
        reason=reason.value,
    )


def _normalize_country(buyer_country: str) -> str | None:
    code = (buyer_country or "").strip().upper()
    if not code:
        return None
    if len(code) == 2 and code.isalpha():
        return code
    return None


def route_invoice(
    *,
    buyer_kind: BuyerKind,
    buyer_country: str,
    payment_method: LegalPaymentMethod,
    payment_signal: PaymentSignalConfidence,
    buyer_tax_id: str = "",
    buyer_status: BuyerStatusConfidence = BuyerStatusConfidence.UNVERIFIED,
) -> FiscalRouting:
    if buyer_kind is BuyerKind.UNKNOWN:
        return _unresolved(RoutingReason.BUYER_STATUS_UNKNOWN)

    if buyer_kind is BuyerKind.CONSUMER:
        return FiscalRouting(
            channel=FiscalChannel.F1,
            confidence=RoutingConfidence.CONFIRMED,
            reason=RoutingReason.CONSUMER_FINAL_CONSUMPTION.value,
        )

    if buyer_status is not BuyerStatusConfidence.VERIFIED:
        return _unresolved(RoutingReason.BUSINESS_IDENTITY_UNVERIFIED)

    if not (buyer_tax_id or "").strip():
        return _unresolved(RoutingReason.BUSINESS_TAX_ID_MISSING)

    country = _normalize_country(buyer_country)
    if country is None:
        return _unresolved(RoutingReason.BUSINESS_COUNTRY_UNKNOWN)

    if country != "HR":
        return FiscalRouting(
            channel=FiscalChannel.STANDARD,
            confidence=RoutingConfidence.CONFIRMED,
            reason=RoutingReason.FOREIGN_B2B_OUTSIDE_MANDATE.value,
        )

    if payment_signal is not PaymentSignalConfidence.EXPLICIT:
        return _unresolved(RoutingReason.DOMESTIC_B2B_PAYMENT_UNCONFIRMED)

    if payment_method in (LegalPaymentMethod.CASH, LegalPaymentMethod.CARD):
        return FiscalRouting(
            channel=FiscalChannel.F1,
            confidence=RoutingConfidence.CONFIRMED,
            reason=RoutingReason.DOMESTIC_B2B_ARTICLE_39_EXCEPTION.value,
            requires_recipient_tax_id=True,
        )

    if payment_method is LegalPaymentMethod.TRANSFER:
        return FiscalRouting(
            channel=FiscalChannel.ERACUN,
            confidence=RoutingConfidence.CONFIRMED,
            reason=RoutingReason.DOMESTIC_B2B_ERACUN_MANDATE.value,
        )

    return _unresolved(RoutingReason.DOMESTIC_B2B_PAYMENT_UNCONFIRMED)
