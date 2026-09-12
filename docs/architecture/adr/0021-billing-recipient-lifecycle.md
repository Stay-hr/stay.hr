# ADR 0021: Billing recipient lifecycle

## Status

Accepted (2026-09) · **module decision, not a platform standard**

Do not add this ADR to the “Stable platform standards” table in [README.md](README.md). This slice locks the contract only. There is **no** Django model, **no** migration, and **no** UI yet.

## Summary

**Why:** Flat `Reservation.buyer_*` cannot tell “the guest later asked for a company invoice” from “this company was the buyer on the issued invoice”. Reservation #1159 is the proof: an R1 request exists in messages, but `259-ROOMS-1` was issued and emailed to the guest. Writing `buyer_*` after the fact would falsify that document.

**How:** A dedicated `BillingRecipient` record on the reservation, with an explicit lifecycle `REQUESTED → READY → APPLIED`, structured fields for a domestic or foreign company, and a hard ban on applying a recipient to an already-persisted `Invoice`.

**This PR** records the contract as ADR + a pure module. Nobody persists a row. `#1159` is not written.

## Context

Today B2B data lives on `Reservation` as `buyer_company_name` / `buyer_oib` (max 11) / `buyer_address` / `invoice_email`, and is copied into `Invoice.buyer_*` at issue ([invoice_builder.py](../../../backend/apps/billing/services/invoice_builder.py)). After the first invoice, those reservation fields are treated as immutable for rebuilds — but they still have no status, no applied-invoice pointer, and no room for a foreign VAT ID.

ADR [0020](0020-fiscal-channel-routing.md) already needs `buyer_kind`, `buyer_country`, `buyer_tax_id`, and `BuyerStatusConfidence` as **inputs**. It must not infer “business” from a non-empty tax id.

#1159 (Uzorita, locked):

```text
R1 request in messages (2026-09-12 ~06:57 UTC):
PRO AUTOMATIKA / OIB 87357644223 / Novo naselje 19E, Bilice
email 	domagoj.perjanec@pro-automatika.hr

Reservation.buyer_* = empty (must stay empty)
Invoice 259-ROOMS-1 = DARIO PREZEC, emailed, old ZKI, no JIR
```

The request **preceded** issue. A “requested_at ≤ issued_at” check is therefore **not** enough to protect 259. Apply is only legal in the same transaction that **creates** the invoice.

## Decision

### Entity

`BillingRecipient` is a tenant-scoped billing record (`apps.billing`), FK to `Reservation`. It is not a CRM partner. It does not replace `Invoice.buyer_*` — the issued invoice remains the source of truth for that document.

One reservation may have many rows over time. At most **one open** row (`REQUESTED` or `READY`) per reservation. Many `APPLIED` rows are allowed later, one per invoice.

`Reservation.buyer_*` is legacy. This slice does not read or write it. Future issue may copy from a `READY` recipient onto a **new** `Invoice`; it must not back-fill `buyer_*` on a reservation that already has an issued invoice.

### Lifecycle

```text
REQUESTED → READY → APPLIED
```

| Status | Meaning |
|--------|---------|
| `REQUESTED` | A company invoice was asked for. Fields may be incomplete. |
| `READY` | Structurally complete for the next **new** issue. Does **not** mean the identity is verified. |
| `APPLIED` | This row was snapshotted onto an invoice **as that invoice was created**. Terminal for the row. |

Allowed transitions:

| From | To | When |
|------|----|------|
| `REQUESTED` | `READY` | Structural completeness passes |
| `READY` | `REQUESTED` | An edit makes the row incomplete |
| `READY` | `APPLIED` | Same transaction as `Invoice.objects.create` |

Forbidden:

- `REQUESTED` → `APPLIED` (no skip)
- any transition out of `APPLIED`
- linking or applying to an **already persisted** `Invoice`
- mutating `Invoice.buyer_*` / ZKI / JIR from this model

`APPLIED` ⇔ `applied_invoice_id` is set. `REQUESTED` / `READY` ⇔ `applied_invoice_id` is null.

### Fields (locked for the future model)

| Field | Rule |
|-------|------|
| `company_name` | Required for `READY` |
| `tax_id` | Required for `READY`. HR = 11 digits. Foreign = non-empty, not capped at 11 |
| `tax_id_country` | ISO 3166-1 alpha-2. Required for `READY` |
| `country` | Seat / address country, ISO 3166-1 alpha-2. Required for `READY` |
| `address` | Street line. Required for `READY` |
| `postal_code` | Required for `READY` |
| `city` | Required for `READY` |
| `email` | Required for `READY` |
| `phone` | Optional |
| `identity_confidence` | `unverified` / `verified` — same meaning as ADR 0020 `BuyerStatusConfidence`. Default `unverified`. **Independent of status.** `READY` + `unverified` is legal; routing stays `UNRESOLVED` until verified |
| `source` | `booking_message` / `whatsapp` / `guest_form` / `staff` |
| `source_ref` | Opaque id of the proof (e.g. `GuestMessage` pk). Optional |
| `source_excerpt` | Short stored quote of the request. Optional, no secrets |
| `requested_at` | Set on create. Immutable |
| `ready_at` | Set on enter `READY`. Cleared if it returns to `REQUESTED` |
| `applied_at` | Set on enter `APPLIED`. Immutable |
| `applied_invoice` | FK to `Invoice`, null except `APPLIED`. Unique. Immutable once set |

A `REQUESTED` row may be created with any subset. Empty rows are rejected: at least one of `company_name`, `tax_id`, `source_excerpt` must be present.

Display names (`Hrvatska`) are not countries. Completeness uses ISO2 only.

### Constraints (future table + this contract)

1. `tenant_id == reservation.tenant_id`
2. At most one row per reservation with `status ∈ {requested, ready}`
3. `APPLIED` iff `applied_invoice_id` is set
4. Unique `applied_invoice_id` (one recipient applied per invoice)
5. `can_mark_applied` is false when `invoice_already_persisted` is true — **#1159 guard**
6. `APPLIED` fields are frozen
7. This slice never writes `#1159` and never writes `Reservation.buyer_*`

### Mapping to ADR 0020 (not wired)

When a later issue reads a `READY` row:

| `route_invoice` input | From |
|-----------------------|------|
| `buyer_kind` | `BUSINESS` |
| `buyer_country` | `country` |
| `buyer_tax_id` | `tax_id` |
| `buyer_status` | `identity_confidence` |

Absence of a `READY` row does not imply `CONSUMER`. Missing recipient ≠ consumer (ADR 0020 rejected assumption 5).

## Consequences

- Messages remain a valid proof of an R1 request without falsifying an already issued invoice.
- `#1159` can later become a `REQUESTED` row **without** becoming `APPLIED` on 259.
- Storno / replacement invoice / CIS / NakDost stay out of this slice and out of 0020 callers.
- The Django model + migration exist. Apply on an already persisted invoice is still unreachable. Checkout is not wired. `#1159` is not written.

## Implementation

- Contract: [backend/apps/billing/services/billing_recipient.py](../../../backend/apps/billing/services/billing_recipient.py)
- ORM: [backend/apps/billing/models.py](../../../backend/apps/billing/models.py) (`BillingRecipient`)
- Tests: [backend/apps/billing/tests/test_billing_recipient.py](../../../backend/apps/billing/tests/test_billing_recipient.py), [backend/apps/billing/tests/test_billing_recipient_model.py](../../../backend/apps/billing/tests/test_billing_recipient_model.py)

`BillingRecipient.save(allow_apply=False)` rejects any transition into `APPLIED`. There is no checkout or issue caller. Ordinary edits cannot create an applied row.
