# Architecture Review — Reservation Versioning Infrastructure

**Date:** 2026-07-05  
**Status:** ✅ Initiative complete  
**Scope:** Post-PR5 review before v2 feature work (payments panel, SSE, …)

Related: [reservation-versioning.md](reservation-versioning.md) · [ADR 0001](adr/0001-reservation-event-versioning.md) · [AGENTS.md](../../AGENTS.md#reservation-versioning)

---

## Verdict

All **four required artifacts** are present, aligned with the implementation, and cross-linked. All **three platform checklist** items pass. **Reservation Versioning Infrastructure** is accepted as platform infrastructure; future modules follow the same pattern (new `scope` + writer hooks + `useTimelineVersionPoll`).

---

## Artifact review

| Artefakt | Review question | Result |
|----------|-----------------|--------|
| **1. Kod** | PR1–PR4 DoD fulfilled? | ✅ Pass — see [Code verification](#code-verification) |
| **2. Dokumentacija** | `reservation-versioning.md` reflects implementation and four levels? | ✅ Pass — intro, Overview, Scopes, When to touch, touch vs publish, sync-versions API, API Compatibility, Performance Goals, Future: SSE, Adding a new module, Governance |
| **3. ADR** | Explains decision and alternatives? Aligned with docs? | ✅ Pass — ADR 0001 Accepted; Summary (four levels), Context, Decision, Consequences, Alternatives; links to docs and AGENTS.md |
| **4. Governance** | `AGENTS.md` rule present and links to docs? | ✅ Pass — [AGENTS.md § Reservation versioning](../../AGENTS.md#reservation-versioning) |

### Cross-artifact alignment

| Topic | Code | Docs | ADR | AGENTS.md |
|-------|------|------|-----|-----------|
| Stable scope names (`messages`, …) | `ReservationVersionScope` | Scopes table | Decision § | — |
| `{ "versions": { … } }` contract | `fetch_reservation_versions`, view | sync-versions API | Consequences | — |
| UI-visible touch only | Writer hooks + tests | When to touch | Consequences | — |
| No parallel versioning | Single `touch_*` API | Governance | Consequences (negative) | Governance rule |
| `publish_*` stub for v2 transport | `reservation_version.py` | touch vs publish, Future: SSE | Decision | — |

---

## Platform checklist

| Question | Expected | Result |
|----------|----------|--------|
| **Performance goals met?** | touch &lt; 5 ms; sync-versions 1 SELECT; 304 on unchanged poll | ✅ `test_touch_reservation_version_latency` (median/p95 &lt; 5 ms); `test_fetch_reservation_versions_single_scope_query_count` (`assertNumQueries(1)`); `test_sync_versions_scope_messages_etag_304` (HTTP 304) |
| **API compatible?** | Legacy full `sync-versions` unchanged; scoped `{ versions }` stable | ✅ `test_sync_versions` (legacy); `test_sync_versions_scope_messages_returns_zero_without_row`; invalid scope → 400; `guest_messages` rejected |
| **Pattern extensible?** | payments, documents, housekeeping, checkin — scope + hook + poll only | ✅ `ReservationVersionScope` pre-defines all five scopes; docs *Adding a new module* example; `useTimelineVersionPoll` parameterized by `scope`; no panel-specific versioning |

---

## Code verification

### PR1 — Model + touch + publish stub

| Criterion | Evidence |
|-----------|----------|
| `ReservationVersion` model + migration | `models.py`, `0024_reservationversion.py` |
| Optimistic `F()` bump + race retry | `reservation_version.py` `_bump_version` |
| Concurrent exact increments | `test_concurrent_touch_increments_exactly`, `test_concurrent_first_bump_race` |
| Latency &lt; 5 ms | `test_touch_reservation_version_latency` |
| `publish_*` stub | `publish_reservation_version_changed` (debug log only) |
| No HTTP API change | PR1 isolated to reservations app |

### PR2 — sync-versions scoped poll

| Criterion | Evidence |
|-----------|----------|
| `scope=messages` → `{ "versions": { "messages": N } }` incl. `0` | `test_fetch_reservation_versions_single_scope_zero_when_missing`, `test_sync_versions_scope_messages_returns_zero_without_row` |
| ETag / 304 | `test_sync_versions_etag_304_semantics`, `test_sync_versions_scope_messages_etag_304` |
| 1 SELECT per scoped poll | `test_fetch_reservation_versions_single_scope_query_count` |
| Legacy clients unaffected | `test_sync_versions`, `test_sync_versions_with_reservation_id` |

### PR3 — Writer hooks (MESSAGES, UI-visible only)

| Criterion | Evidence |
|-----------|----------|
| Inbound/outbound touch | `test_whatsapp_inbound_touches_when_reservation_linked`, email/Channex/handoff tests |
| No touch on delivered/read | `test_delivered_status_webhook_does_not_touch`, `test_read_status_webhook_does_not_touch` |
| No touch on pending_send retry | `test_pending_send_retry_metadata_does_not_touch` |
| No touch on Channex duplicate | `test_channex_message_does_not_touch_on_duplicate` |

### PR4 — Frontend consumer

| Criterion | Evidence |
|-----------|----------|
| `useTimelineVersionPoll` + `onVersionChange` | `useTimelineVersionPoll.ts` — panel uses callback, not transport |
| Messages panel GET | `GuestMessagesPanel` always `sync=0` (ADR 0019 Phase C). `shouldRunFullSync` removed. |
| Poll pauses when tab hidden | `document.hidden` guard in hook |
| 5 s interval, ETag reuse | Default `intervalMs = 5_000`; `If-None-Match` on poll |

### Automated test run (2026-07-05)

```
apps.reservations.tests.test_reservation_version          — 9 tests OK
apps.reservations.tests.test_sync_versions                — 16 tests OK
apps.reservations.tests.test_reservation_version_writer_hooks — 11 tests OK
apps.api.tests.test_reception_api (scoped sync-versions)  — OK
```

PostGIS test DB (`config.settings.test_postgis`).

---

## Manual verification (`/reservations/973`)

Procedure documented in [reservation-versioning.md § Manual verification checklist](reservation-versioning.md#manual-verification-checklist):

1. Mount → one `sync=0` timeline fetch (DB-only; [ADR 0019](adr/0019-messaging-conversation-store.md))
2. WhatsApp inbound → new message within ~5 s without F5  
3. `delivered` webhook → no refresh  
4. Tab hidden → no polls  
5. Tab visible, no changes → poll **304**

Automated regression covers API contract and writer-hook semantics; production smoke on reservation 973 is the operator checklist above.

---

## Gaps found and resolved in this review

| Gap | Resolution |
|-----|------------|
| Docs missing mandatory 2-sentence platform intro | Added to `reservation-versioning.md` (PR5 DoD) |
| ADR missing explicit four-level summary | Added **Summary** section to ADR 0001 |
| No formal review record | This document |

No v1 code changes required.

---

## Next steps (out of scope for v1)

- v2 feature PRs: payments/documents/check-in panels — same infrastructure  
- Phase D: SSE/WebSocket inside `publish_reservation_version_changed` only  
- New scopes or contract changes require ADR revision
