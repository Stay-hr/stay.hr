# ADR 0020: Fiscal channel routing (F1 / eRačun / standard)

## Status

Accepted (2026-09) · **module decision, not a platform standard**

Do not add this ADR to the “Stable platform standards” table in [README.md](README.md). The rule is locked before the first eRačun is sent; it is not yet proven in production.

## Summary

**Why:** From 2026 a Croatian guest invoice is not automatically an F1 fiscalization just because it is an invoice, and not automatically an eRačun just because the buyer is a company. **How:** Stay decides the exclusive channel from three axes together — recipient status, domestic vs foreign transaction, and legal payment method — with an explicit `UNRESOLVED` state instead of a silent default. **This PR** only records the rule and a pure decision function. Nobody calls it at runtime yet.

## Context

Stay today issues one `Invoice` at checkout and sends it down the F1 path (`ZKI → F73 RacunZahtjev → CIS → JIR`). The production fiscal gate is still closed (`FISKAL_EXECUTION_ENABLED=false`). fiskal.hr already has F2 / eRačun infrastructure (`invoice-v1` → `EvidentirajERacunZahtjev`, plus UBL providers), but Stay never calls it.

Two product facts collide with the 2026 Act:

1. A Booking.com / card reservation where the guest asks for a company invoice looks like “R1 / eRačun” in everyday speech. It is not. Recipient type alone does not choose the channel.
2. The current F1 builder in Stay does not send a buyer block. That is **not** a reason to claim “a company only changes the PDF”. Article 15(6) requires the recipient OIB on an F1 invoice that uses the Article 39 exception.

Legal basis — [Zakon o fiskalizaciji, NN 89/2025](https://narodne-novine.nn.hr/clanci/sluzbeni/2025_06_89_1233.html), [Porezna — eRačun](https://porezna.gov.hr/fiskalizacija/bezgotovinski-racuni/eracun):

- **Art. 38** — mandatory domestic B2B eRačun. A “domestic transaction” requires that **both** issuer and recipient have a seat / residence in Croatia. A foreign company is therefore **not** in that regime.
- **Art. 39** — exception from the domestic eRačun duty for a cash or card payment, if F1 is performed. If the exception is used, the same invoice **must not** also be issued and fiscalized as an eRačun. That is the legal basis for XOR. The exception is tied to Art. 38 and **does not extend** to a recipient who is not under Art. 38.
- **Art. 15(6)** — when applying Art. 39, the Tax Administration is also given the **recipient OIB**.
- **Final consumption** — F1 is primarily the fiscalization of invoices issued to citizens / consumers, **regardless of payment method**.

Existing code that must not be treated as the legal model:

- `resolve_payment_method()` in [backend/apps/billing/services/payment.py](../../../backend/apps/billing/services/payment.py) infers `TRANSFER` from any unrecognized `payment_status` text, and treats `booking` in `source` / `payment_provider` as a payment method.
- `fisk1_payment_code` maps both `BOOKING` and `TRANSFER` to CIS `NacinPlac=T`. The CIS payment code is **not** a channel discriminator.
- Channex `payment_collect` is not persisted. `Invoice.PaymentMethod.BOOKING` is a collection channel, not a legal payment method.

## Decision

The channel of an issued invoice is exclusive: `F1` | `ERACUN` | `STANDARD`. Before a final channel exists, the function returns `channel=None` with `confidence=REVIEW` (`UNRESOLVED`). There is **no** “F1 as a safe default”.

The three axes are:

| Axis | Values the function accepts |
|------|-----------------------------|
| Recipient status | `CONSUMER` / `BUSINESS` / `UNKNOWN`, plus `BuyerStatusConfidence` (`VERIFIED` / `UNVERIFIED`) |
| Territory | ISO 3166-1 alpha-2 `buyer_country` (`HR` vs a known foreign country vs unknown) |
| Legal payment | `CASH` / `CARD` / `TRANSFER` / `UNKNOWN`, plus `PaymentSignalConfidence` (`EXPLICIT` / `INFERRED` / `UNKNOWN`) |

`booking` is **not** a legal payment method. Translation (`Booking + proven guest card → CARD + EXPLICIT`) stays outside this function and is blocked until a structured `payment_collect` is stored.

### Decision table

| Buyer | Payment | Signal | Result |
|-------|---------|--------|--------|
| consumer | any | any | `F1` / `CONFIRMED` |
| HR business | cash / card | `EXPLICIT` | `F1` / `CONFIRMED` |
| HR business | transfer | `EXPLICIT` | `ERACUN` / `CONFIRMED` |
| HR business | any | `INFERRED` / `UNKNOWN` | `None` / `REVIEW` |
| HR business | unknown method | `EXPLICIT` | `None` / `REVIEW` |
| foreign business, verified identity | any | any | `STANDARD` / `CONFIRMED` |
| business, country or identity unclear | any | any | `None` / `REVIEW` |
| buyer kind unknown | any | any | `None` / `REVIEW` |

Two axes are payment-agnostic on purpose:

- **consumer → F1** regardless of payment (final consumption).
- **verified foreign business → STANDARD** regardless of payment, including `UNKNOWN`. Art. 38 does not apply; Art. 39 is not extended.

Payment-signal confidence therefore changes the outcome **only** for domestic B2B. That is the only place where the existing payment heuristic can choose the wrong 2026 regime.

### Two guards, different status

**Guard 1 — legal condition: `STANDARD` requires a verified foreign business identity.**

`buyer_country != "HR"` is not enough. The function also requires `buyer_kind=BUSINESS`, `buyer_status=VERIFIED`, a non-empty `tax_id`, and a known non-HR ISO country. A company name alone, or an unverified VAT ID, stays `UNRESOLVED`. The same `VERIFIED` + tax-id + known-country gate applies before any domestic B2B route: an eRačun mandate cannot be asserted on an unconfirmed OIB.

**Guard 2 — Stay.hr safety policy, not a statutory requirement: domestic B2B requires `payment_signal=EXPLICIT`.**

Art. 39 allows a domestic B2B cash/card payment to go through F1 instead of eRačun; otherwise the domestic B2B transaction is in the eRačun regime. Because Stay’s current heuristic can mis-label a transfer as a card (or the reverse), `INFERRED` / `UNKNOWN` → `UNRESOLVED` is a **Stay policy**. Do not cite this guard as Article 39.

### Rejected assumptions

1. **Not** “the discriminator is payment method, not buyer type”.
2. **Not** “a company only changes the PDF, not the F73 message”. Art. 15(6) requires `OibPrimateljaRacuna` on a domestic B2B F1 invoice. A company does not change the ZKI/JIR algorithm, but **does** change the F1 payload.
3. **Not** “F1 as a safe default”. On an uncertain domestic B2B invoice, F1 can silently bypass a mandatory eRačun.
4. **Not** “foreign B2B → F1”. A foreign company is not a consumer merely because it is outside the Croatian eRačun mandate.
5. **Not** “a non-empty `tax_id` means the buyer is a business”. A missing OIB may mean an incomplete B2B capture, not B2C.

`buyer_kind` is an input. The function never infers it from `tax_id`.

### Downstream contract (not implemented here)

When the result is domestic B2B → `F1`, `requires_recipient_tax_id=True`. The future F1 payload must send `OibPrimateljaRacuna`. This ADR does not change `payload.py`.

## Consequences

- Stay will not send a Booking “R1” into Fiskalizacija 2.0 / Lakol merely because the buyer is a company.
- The first wired caller must map `Invoice` / reservation fields onto `buyer_kind`, `buyer_country`, `buyer_status`, and `payment_signal`. That adapter does not exist yet: `Reservation.buyer_oib` is 11 characters, there is no `tax_id_country`, and Channex `payment_collect` is not stored.
- `tasks.py` and `submit.py` stay on the current F1-only path until a later PR explicitly calls `route_invoice`.
- XOR is a legal constraint for the Art. 39 exception (one invoice, one channel). It is also the Stay model for every resolved invoice.

## Follow-ups (out of this slice)

- `OibPrimateljaRacuna` on the F1 payload for domestic B2B (Art. 15(6)).
- Freeze the chosen channel on `Invoice` at issue time so an issued document cannot be re-routed.
- Wider `billing_recipient` (`tax_id`, `tax_id_country`, structured address) and a source for `BuyerStatusConfidence`.
- Persist Channex `payment_collect` before any `Booking → CARD + EXPLICIT` translation.

## Implementation

Pure function: [backend/apps/billing/services/fiscal_routing.py](../../../backend/apps/billing/services/fiscal_routing.py). Tests: [backend/apps/billing/tests/test_fiscal_routing.py](../../../backend/apps/billing/tests/test_fiscal_routing.py). No Django imports, no model migrations, no runtime callers.
