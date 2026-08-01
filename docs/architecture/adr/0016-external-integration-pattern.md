# ADR 0016: External Integration Pattern

## Status

Accepted (2026-08) · **stable standard**

Do not widen this ADR with per-integration special cases. If a future integration cannot follow the pattern, document a **justified exception** (in that module’s docs / PR) or write a **new ADR** for a different pattern. Keep 0016 simple.

## Summary

**Why:** External systems (government APIs, channel managers, fiscal/payment providers) must not own or block stay.hr business outcomes. **How:** Local business process is the source of truth; external sync is best-effort with explicit sync state, idempotent retry, and built-in observability. **Reference:** eVisitor v1 is the first complete implementation. New integrations should cite this ADR rather than inventing a parallel model.

## Context

Integrations fail, time out, and change APIs. If a local action (check-in, checkout, booking ingest, fiscal seal) is rolled back or blocked solely because an external call failed, operators lose trust and the product becomes fragile.

eVisitor v1 crystallized a repeatable split:

```text
Business Process (Source of Truth)
        │
        ▼
Local Transaction (committed)
        │
        ▼
Best-Effort Integration
        │
        ▼
Explicit Sync State
        │
        ▼
Idempotent Retry (explicit)
        │
        ▼
Observability (audit, metrics, correlation_id)
```

Without a platform rule, each new integration risks re-coupling local success to remote success, hiding retry behind side effects, or shipping without audit/metrics.

## Decision

All **outbound external integrations** that project stay.hr business state into a third party SHOULD follow this pattern.

### Applicability

This ADR applies to outbound integrations that **project stay.hr business state** to external systems (sync / registration / push after a local decision).

It does **not** automatically apply to:

- synchronous payment authorization where the external provider determines whether the business action may proceed;
- identity / authentication providers during login (e.g. OAuth);
- inbound webhooks;
- read-only integrations (lookups, reference data fetch).

Those integrations may follow a different ADR or document a justified deviation. They must still not corrupt local source of truth where they write local state.

### Pattern

1. **Source of truth** — The local domain model and local transaction decide whether the business action succeeded.
2. **Commit before remote I/O** — Persist the local outcome, then attempt external sync. Remote timeout/exception must not roll back the local commit.
3. **Best-effort synchronization** — Failure to sync leaves the local process valid; sync state is updated accordingly.
4. **Explicit sync state** — Durable statuses distinct from local business status (e.g. not conflating “checked in” with “registered at eVisitor”).
5. **Idempotent retry** — Retry is an explicit API/operation, safe to repeat; never a hidden side effect of re-doing the local business action.
6. **Correlation ID** — One ID spans orchestration → remote attempt → audit/logs for a single sync attempt.
7. **Audit** — Persist request/response (or masked payload) for each attempt before/after the remote call where practical.
8. **Metrics** — Process-local or exported counters for sync outcomes (success / partial / failure / not required); duration histograms when production volume justifies them.
9. **Feature freeze after stabilization** — When a module reaches Production Ready, new features require a measurable production signal (API change, sustained failure rate, regulatory need, or user demand that cannot be solved operationally). Bugfixes and compatibility remain allowed.

### Non-goals

- This ADR does not mandate a shared framework, base class, or shared job runner for all integrations.
- Strict two-phase commit with external systems is out of scope (see Applicability for cases where the remote system must authorize the action).

### PR review checklist

For any PR that adds or changes an outbound external integration, start here:

1. Is the **local business process** the source of truth?
2. Is the **local commit separated** from remote I/O?
3. Is synchronization **best-effort** (remote failure does not undo local success)?
4. Are there **explicit sync states** distinct from local business status?
5. Is **retry** explicit and **idempotent**?
6. Are **audit**, **metrics**, and **`correlation_id`** present for sync attempts?
7. Is a **feature freeze after stabilization** planned (or already documented)?

If all seven are **yes**, the change likely follows this ADR.  
If any answer is **no**: is that a **documented justified deviation**, or **architectural debt** to fix before merge?

## Consequences

### Positive

- Operators can complete reception and booking workflows when a third party is down.
- Sync failures are visible, retryable, and attributable via `correlation_id`.
- New modules can cite this ADR instead of re-litigating architecture per PR.
- Feature freeze after v1 reduces feature creep on stable integrations.

### Negative

- Temporary divergence between local state and external system is expected and must be designed for (UI, ops runbooks).
- Writers must maintain sync state and retry paths explicitly — more code than fire-and-forget.
- “Best-effort” must not become “best-effort and forgotten”: observability is mandatory.

## Alternatives considered

| Alternative | Why not chosen |
|-------------|----------------|
| Remote call inside the same DB transaction as the business action | Timeout/exception rolls back local truth; operators blocked |
| Local success only after remote ACK | Couples uptime of stay.hr to third-party SLA |
| Silent re-sync on every subsequent PATCH/refresh | Hidden retries; hard to reason about; duplicates |
| Shared generic “IntegrationJob” framework first | Premature abstraction; eVisitor proved the rules without a framework |

## Reference implementation

| Module | Notes |
|--------|-------|
| [eVisitor v1](../../development/evisitor.md) | Production Ready (2026-08): reception auto check-in, `checkout_failed`, per-guest retry, audit, metrics, correlation ID, feature policy |

Future candidates (apply the same rules, do not copy eVisitor domain types): Channex ARI/outbound, Booking.com connectivity, WhatsApp delivery, fiscalization, payment providers, ERP sync, other national tourist registers.

## References

- [eVisitor integracija](../../development/evisitor.md) — milestone, feature policy, reception check-in / checkout semantics
- [ADR 0014: Channex outbound guard](0014-channex-outbound-guard.md) — related outbound safety constraints
- [ADR 0001: Reservation event versioning](0001-reservation-event-versioning.md) — local UI sync pattern (orthogonal; do not invent parallel versioning for integration state)
