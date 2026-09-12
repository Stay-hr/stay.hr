# ADR 0022: Invoice replacement and storno

## Status

Accepted (2026-09) · **module decision, not a platform standard**

Do not add this ADR to the “Stable platform standards” table in [README.md](README.md).

## Summary

**Why:** An issued invoice is an immutable fiscal document. Reservation #1159 has `259-ROOMS-1` issued to the guest; the needed document is an R1 for PRO AUTOMATIKA. Rewriting `259` would falsify the issued document.

**How:** A privileged `InvoiceReplacement` case creates two new invoices in the normal tenant sequence: a negative storno, then a positive replacement. The original row is never mutated. `Invoice` has no type flag; document role is derived from case relationships.

**This PR** adds the models, DB constraints, and the pure contract. There are **no** command services, **no** admin API, **no** storno issuance, and **no** `#1159` write.

## Decision

### Documents

```text
original  →  next sequence = STORNO  →  next sequence = REPLACEMENT
```

`260` / `261` are expected only if nothing else consumes the sequence between the two actions. They are **not** a domain invariant. Numbers are never reserved in advance.

### Case lifecycle

```text
OPEN → COMPLETED
OPEN → CANCELLED
```

No `FAILED`. Cancel is allowed only before storno. Recipient freezes when `storno_invoice` is created, not only on `COMPLETED`.

### Effective invoice

`resolve_effective_invoice(reservation)` walks `InvoiceReplacement` links. Invoice cardinality is not an invariant. `>1` invoice is legal after storno/replacement. The resolver never uses `.last()` / newest / highest sequence. It never returns a storno. `OPEN + storno` is `None` (gap). Graph corruption is `InvoiceGraphError`.

Ordinary `issue_guest_invoice()` must not fill that gap (`ReplacementInProgress`).

### Recipient

Dedicated `InvoiceReplacementRecipient` (not `BillingRecipient`). Mutable only while the case is `OPEN` and `storno_invoice` is null. `VERIFIED` is an explicit privileged staff attestation; automation cannot set it. Identity edits clear verification. `source` / `source_ref` / `source_excerpt` are write-once.

Storno requires recipient structurally `READY` + `VERIFIED`.

### Snapshots

- Storno = exact negative money of the original; buyer and payment copied verbatim.
- Replacement = exact positive economic snapshot of the original; only buyer/document identity changes.
- `issued_at` is actual tenant-local time. Never backdated.
- New invoices own frozen issuer/document context. Legacy `259` is never backfilled; its PDF stays authoritative.
- Legal issuer is pinned by original issuer OIB. Legacy originals need write-once `original_issuer_oib` evidence before storno.

### Authorization and commands

`admin:read` for audit/read. `admin:write` for every mutation. `reception:*` is insufficient. Mutations are explicit commands. Lifecycle/link/audit fields are read-only to clients. Email delivery is a separate post-commit action.

### Integrity

No hard delete of issued `Invoice` / case / recipient. Audit actor FKs use `PROTECT`. Reservation is the outer write mutex. Open / storno / completion are idempotent per case.

## Implementation

- Contract: [backend/apps/billing/services/invoice_replacement.py](../../../backend/apps/billing/services/invoice_replacement.py)
- ORM: [backend/apps/billing/models.py](../../../backend/apps/billing/models.py)
- Commands: [backend/apps/billing/services/invoice_replacement_service.py](../../../backend/apps/billing/services/invoice_replacement_service.py) — open / update / verify / cancel; no Invoice writes
- Tests: [backend/apps/billing/tests/test_invoice_replacement.py](../../../backend/apps/billing/tests/test_invoice_replacement.py), [test_invoice_replacement_model.py](../../../backend/apps/billing/tests/test_invoice_replacement_model.py), [test_invoice_replacement_service.py](../../../backend/apps/billing/tests/test_invoice_replacement_service.py)

No storno issuance, admin API, or `#1159` write in this slice.
