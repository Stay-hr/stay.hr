# ADR 0001: Reservation event versioning for panel auto-refresh

## Status

Accepted (2026-07) · amended 2026-08 ([ADR 0019](0019-messaging-conversation-store.md) supersedes messages-panel `sync=1` on mount)

## Summary

**Why:** Reception panels need efficient, consistent reservation state sync without manual refresh. **How:** Writers call `touch_reservation_version()`; the frontend polls `sync-versions` per scope and fetches full data only on version change (ETag/304 when unchanged). **Extension:** New modules add a stable `scope`, writer hooks, and `useTimelineVersionPoll` — no parallel versioning. **Evolution:** Transport (poll → SSE/WebSocket) changes only `publish_reservation_version_changed` and the watch hook; panels keep `onVersionChange`.

## Context

Reception panels on a reservation detail page (guest messages, payments, documents, check-in, housekeeping) need to stay up to date without manual refresh. Naive approaches fail at scale:

- **Full timeline poll** — expensive serialization and DB work on every interval.
- **Aggregate fingerprints** (`COUNT`, `MAX`, `GROUP BY`) on each poll — cost grows with message volume.
- **WebSocket-only** — real-time push is desirable later, but adds infrastructure before the core pattern is proven.

Multiple panels on one reservation also need independent refresh signals: a new payment should not force a full messages refetch unless the user is viewing that panel.

## Decision

Use an **event-driven monotonic counter** per `(reservation_id, scope)`:

1. **Writers** call `touch_reservation_version()` when UI-visible state changes.
2. **Frontend** polls `GET /api/v1/reception/sync-versions/?reservation_id=X&scope=messages` every ~5 s (tab visible).
3. Poll uses **ETag / 304** — unchanged version returns no body.
4. On version change, the panel runs a **background** full fetch (`sync=0`). For **messages**, provider fetch must not block opening the conversation — first paint and version refresh are DB-only (`sync=0`). Blocking `sync=1` on mount / tab-return / interval is **superseded** by [ADR 0019](0019-messaging-conversation-store.md). Other scopes may still use a heavy first fetch until they have their own ingest ADR.

Scopes use **stable domain names** (`messages`, `payments`, …), not module or package names.

`publish_reservation_version_changed()` is a v1 no-op stub; SSE/WebSocket/Redis can extend it later without changing `touch` or the poll hook.

## Consequences

### Positive

- Poll is **O(1)** — one `SELECT` on `(reservation_id, scope)`.
- **304** avoids JSON serialization when nothing changed.
- Same infrastructure applies to every future panel — new scope + writer hooks + `useTimelineVersionPoll`.
- API contract `{ "versions": { ... } }` is stable and backward compatible with existing full `sync-versions` clients.

### Negative

- Writers must **explicitly** call `touch`; forgetting a hook means stale UI until the next ingest or explicit reconcile (messages: not a blocking GET to the provider — [ADR 0019](0019-messaging-conversation-store.md)).
- Counter semantics must be documented (UI-visible only — no bump on `delivered`/`read` webhooks).
- Parallel versioning mechanisms (`PaymentVersion`, per-panel `MAX` polls) are forbidden without a new ADR.

## Alternatives considered

| Alternative | Why not chosen |
|-------------|----------------|
| Poll full `/messages` on interval | High cost; scales poorly with timeline size |
| Hash entire timeline payload | Still requires reading/computing timeline data each poll |
| `COUNT`/`MAX` on messages table | Aggregate work on every poll; N+1 risk across scopes |
| WebSocket-only (no poll) | Deferred to Phase D; poll + ETag works without extra infra |
| Redis cache invalidation only | Adds dependency; DB counter is sufficient for v1 |

## References

- [Reservation versioning](../reservation-versioning.md)
- [Architecture review (2026-07)](../reservation-versioning-architecture-review.md)
- [AGENTS.md — Reservation versioning](../../../AGENTS.md#reservation-versioning)
- [ADR 0019 — Conversation store](0019-messaging-conversation-store.md) — messages read path; GET must not call providers
- Implementation: `backend/apps/reservations/reservation_version.py`, `backend/apps/reservations/sync_versions.py`, `web/reception/lib/useTimelineVersionPoll.ts`
